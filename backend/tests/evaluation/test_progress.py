from types import SimpleNamespace

import pytest
from evaluation_technical_fixtures import TechnicalCatalogue
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.config import Settings
from aftercare.db import Base, User
from aftercare.evaluation import api
from aftercare.evaluation.service import EvaluationService, ExperimentInput
from aftercare.evaluation.tables import EvaluationProgress, Experiment

TESTER = UserContext(user_id="progress-tester", role="tester")
TESTER_TOKEN = "t" * 48
SUPPORT_TOKEN = "s" * 48
TESTER_HEADERS = {"Authorization": f"Bearer {TESTER_TOKEN}"}


@pytest.fixture
def system(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'progress.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db, db.begin():
        db.add_all(
            [
                User(
                    id=TESTER.user_id,
                    display_name="tester",
                    token_hash=token_hash(TESTER_TOKEN),
                    role="tester",
                    enabled=True,
                ),
                User(
                    id="progress-support",
                    display_name="support",
                    token_hash=token_hash(SUPPORT_TOKEN),
                    role="support",
                    enabled=True,
                ),
            ]
        )
    settings = Settings(
        profile="test",
        database_url="",
        redis_url="",
        object_endpoint="",
        object_access_key="",
        object_secret_key="",
    )
    calls = []
    service = EvaluationService(engine, settings, dispatch=calls.append, catalog=TechnicalCatalogue())
    app = FastAPI()
    app.state.evaluation, app.state.engine = service, engine
    app.include_router(api.router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            engine=engine, service=service, settings=settings, client=client, dispatches=calls
        )
    engine.dispose()


def create(system, repetitions=1, **options):
    defaults = {
        "name": "进度审计",
        "dataset_id": "v0.2",
        "split": "dev",
        "mode": "single",
        "strategy": "routed",
        "repetitions": repetitions,
        "concurrency": 1,
        "max_calls": 1,
        "budget_usd": 0,
        "source": "fixture",
        "case_ids": ["UNIT-DAMAGE"],
    }
    return system.service.create(TESTER, ExperimentInput(**{**defaults, **options}), "create")


def outcome(*_args, **_kwargs):
    return {
        "status": "NEEDS_REVIEW",
        "failure_reason": "HUMAN_REFERENCE_PENDING",
        "actual": {"private_model_prompt": "must never be in progress"},
        "trace": [],
        "checks": [],
    }


def process(system, experiment_id, run=outcome):
    runner = SimpleNamespace(
        settings=system.settings, model=None, run=run, recover_audit=lambda _result_id: (None, [])
    )
    system.service.process(experiment_id, runner)


def path(experiment_id):
    return f"/evaluation/experiments/{experiment_id}/progress"


def page(system, experiment_id, **kwargs):
    return system.client.get(path(experiment_id), headers=TESTER_HEADERS, params=kwargs)


def test_execution_journal_paginates_replays_and_settles(system):
    plan = create(system)
    assert create(system) == plan
    experiment_id = plan["id"]
    assert system.dispatches == [experiment_id]
    assert page(system, experiment_id).json()["latest_sequence"] == 2
    process(system, experiment_id)
    process(system, experiment_id, run=lambda *_a, **_kw: pytest.fail("duplicate task ran"))
    response = page(system, experiment_id, limit=3)
    assert response.headers["Cache-Control"] == "no-store"
    head = response.json()
    tail = page(system, experiment_id, after=3).json()
    assert head["history"] == "RECORDED"
    assert tail["terminal"] and tail["status"] == "COMPLETED" and tail["latest_sequence"] == 6
    events = head["events"] + tail["events"]
    assert [event["sequence"] for event in events] == list(range(1, 7))
    assert [(event["kind"], event["status"]) for event in events] == [
        ("EXPERIMENT_STATUS", "QUEUED"),
        ("RESULT_STATUS", "QUEUED"),
        ("EXPERIMENT_STATUS", "RUNNING"),
        ("RESULT_STATUS", "RUNNING"),
        ("RESULT_STATUS", "NEEDS_REVIEW"),
        ("EXPERIMENT_STATUS", "COMPLETED"),
    ]
    assert events[4]["result_id"] == plan["results"][0]["id"]
    assert events[4]["reason"] == "HUMAN_REFERENCE_PENDING"
    assert "private_model_prompt" not in str(events)
    stream = system.client.get(
        path(experiment_id) + "/stream", headers={**TESTER_HEADERS, "Last-Event-ID": "4"}
    )
    assert stream.status_code == 200
    assert [line for line in stream.text.splitlines() if line.startswith("id: ")] == [
        "id: 5",
        "id: 6",
    ]
    assert "event: evaluation-state" in stream.text and '"terminal": true' in stream.text
    assert page(system, experiment_id, after=6).json()["events"] == []
    assert page(system, experiment_id).json()["latest_sequence"] == 6


def test_cancellation_waits_for_active_result_to_settle(system):
    experiment_id = create(system, repetitions=2)["id"]

    def cancel_active(*_args, **_kwargs):
        snapshot = system.service.experiment(TESTER, experiment_id)
        cancelled = system.service.cancel(
            TESTER, experiment_id, expected_version=snapshot["version"], key="cancel"
        )
        assert cancelled["status"] == "CANCELLED"
        waiting = page(system, experiment_id).json()
        assert not waiting["terminal"]
        assert waiting["events"][-1]["status"] == "NOT_RUN"
        assert (
            system.service.cancel(
                TESTER, experiment_id, expected_version=snapshot["version"], key="cancel"
            )
            == cancelled
        )
        assert page(system, experiment_id).json() == waiting
        return {**outcome(), "status": "ERROR", "failure_reason": "EXPERIMENT_CANCELLED"}

    process(system, experiment_id, run=cancel_active)
    settled = page(system, experiment_id).json()
    assert settled["status"] == "CANCELLED" and settled["terminal"]
    assert [event["status"] for event in settled["events"]] == [
        "QUEUED",
        "QUEUED",
        "QUEUED",
        "RUNNING",
        "RUNNING",
        "CANCELLED",
        "NOT_RUN",
        "ERROR",
    ]
    process(system, experiment_id, run=lambda *_a, **_kw: pytest.fail("cancelled task ran"))
    assert page(system, experiment_id).json() == settled


def test_legacy_snapshot_follows_current_status_without_fabricated_history(system):
    experiment_id = create(system)["id"]
    with Session(system.engine) as db, db.begin():
        db.get_one(Experiment, experiment_id).progress_seq = None
        db.execute(
            delete(EvaluationProgress).where(EvaluationProgress.experiment_id == experiment_id)
        )
    initial = page(system, experiment_id).json()
    assert initial["history"] == "HISTORICAL_SNAPSHOT" and not initial["terminal"]
    assert initial["latest_sequence"] == 0 and initial["events"] == []
    stream = system.client.get(path(experiment_id) + "/stream", headers=TESTER_HEADERS)
    assert "event: evaluation-state" in stream.text and "id: " not in stream.text
    process(system, experiment_id)
    settled = page(system, experiment_id).json()
    assert settled == {**initial, "status": "COMPLETED", "terminal": True}


def test_progress_read_checks_cursor_and_revocation_during_buffered_page(system, monkeypatch):
    experiment_id = create(system)["id"]
    stream_path = path(experiment_id) + "/stream"
    assert system.client.get(path(experiment_id)).status_code == 401
    assert (
        system.client.get(
            path(experiment_id),
            headers={
                "Authorization": f"Bearer {SUPPORT_TOKEN}",
            },
        ).status_code
        == 403
    )
    assert page(system, "missing").status_code == 404
    assert page(system, experiment_id, after=-1).status_code == 422
    for header, code in (
        ("-1", "INVALID_PROGRESS_CURSOR"),
        ("no", "INVALID_PROGRESS_CURSOR"),
        ("3", "FUTURE_PROGRESS_CURSOR"),
    ):
        error = system.client.get(stream_path, headers={**TESTER_HEADERS, "Last-Event-ID": header})
        assert error.status_code == 400 and error.json()["detail"]["code"] == code

    authenticate = api.current_user
    checks = 0

    def revoke_before_second_frame(request, credentials):
        nonlocal checks
        checks += 1
        if checks == 3:
            with Session(system.engine) as db, db.begin():
                db.get_one(User, TESTER.user_id).enabled = False
        return authenticate(request, credentials)

    monkeypatch.setattr(api, "current_user", revoke_before_second_frame)
    stream = system.client.get(stream_path, headers=TESTER_HEADERS, params={"follow": False})
    assert [line for line in stream.text.splitlines() if line.startswith("id: ")] == ["id: 1"]
    assert "evaluation-state" not in stream.text
    assert system.client.get(stream_path, headers=TESTER_HEADERS).status_code == 401


def test_progress_writes_rollback_with_state(system, monkeypatch):
    from aftercare.evaluation import service as module

    append = module.append_progress

    def fail_on_result(db, experiment, result=None):
        append(db, experiment, result)
        if result is not None:
            raise RuntimeError("controlled transaction failure")

    monkeypatch.setattr(module, "append_progress", fail_on_result)
    with pytest.raises(RuntimeError, match="controlled transaction"):
        create(system)
    with Session(system.engine) as db:
        assert db.scalar(select(func.count()).select_from(Experiment)) == 0
        assert db.scalar(select(func.count()).select_from(EvaluationProgress)) == 0
    assert system.dispatches == []


def test_sse_crosses_page_boundary_from_last_event_id(system, monkeypatch):
    from aftercare.evaluation import api

    monkeypatch.setattr(api, "PAGE_SIZE", 2)
    experiment_id = create(system, repetitions=2, case_ids=None)["id"]
    response = system.client.get(
        path(experiment_id) + "/stream",
        headers={**TESTER_HEADERS, "Last-Event-ID": "2"},
        params={"follow": False, "after": 1},
    )
    assert response.status_code == 200
    assert [line for line in response.text.splitlines() if line.startswith("id: ")] == [
        f"id: {index}" for index in range(3, 8)
    ]
    assert '"latest_sequence": 7' in response.text
    assert '"terminal": false' in response.text


def test_recovered_worker_emits_interrupted_result_once(system, monkeypatch):
    experiment_id = create(system)["id"]

    def stop_after_claim(*_args, **_kwargs):
        raise RuntimeError("controlled worker exit")

    with monkeypatch.context() as scoped:
        scoped.setattr(system.service, "_run_one", stop_after_claim)
        with pytest.raises(RuntimeError, match="controlled worker exit"):
            process(system, experiment_id)
    before = page(system, experiment_id).json()
    assert before["status"] == "RUNNING" and before["latest_sequence"] == 4

    process(system, experiment_id, run=lambda *_a, **_kw: pytest.fail("interrupted case repeated"))
    tail = page(system, experiment_id, after=4).json()
    assert tail["terminal"]
    assert [(event["kind"], event["status"]) for event in tail["events"]] == [
        ("RESULT_STATUS", "ERROR"),
        ("EXPERIMENT_STATUS", "COMPLETED"),
    ]
    assert tail["events"][0]["reason"] == "WORKER_INTERRUPTED"


def test_missing_history_fails_explicitly_instead_of_hanging_a_stream(system):
    experiment_id = create(system)["id"]
    with Session(system.engine) as db, db.begin():
        db.execute(delete(EvaluationProgress).where(EvaluationProgress.sequence == 1))
    response = system.client.get(path(experiment_id) + "/stream", headers=TESTER_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROGRESS_HISTORY_INCOMPLETE"
