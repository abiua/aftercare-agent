import io
import json
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aftercare import queue_delivery
from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.config import ROOT, TARGETS
from aftercare.db import Base, User
from aftercare.dispatch import DeliveryJournal, DispatchJob
from aftercare.evaluation import service as evaluation_service
from aftercare.evaluation.api import router
from aftercare.evaluation.metrics import METRICS_VERSION
from aftercare.evaluation.progress import append_progress
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import (
    EvaluationImport,
    EvaluationResult,
    EvaluationReview,
    Experiment,
)

TESTER = UserContext(user_id="tester", role="tester")
TOKEN = "a" * 40
BASE_PATH = "/api/evaluation"
STAMP = datetime(2026, 11, 10, tzinfo=UTC)


def _experiment(*, formal=False, status="QUEUED", stamp=STAMP, **kwargs):
    fields = {
        "id": str(uuid4()),
        "name": "private formal body" if formal else "ordinary fixture",
        "status": status,
        "created_by": TESTER.user_id,
        "version": 1,
        "progress_seq": 0,
        "config": {
            "source": "fixture",
            "mode": "single",
            "strategy": "routed",
            "dataset_id": "v0.2",
            "metrics_version": METRICS_VERSION,
            "execution_driver": "queued",
        },
        "created_at": stamp,
        "updated_at": stamp,
    }
    if formal:
        fields.update(formal_freeze_sha256="a" * 64, formal_condition_key="single/routed")
    return Experiment(**{**fields, **kwargs})


@pytest.fixture
def isolated(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'formal-isolation.db'}")
    Base.metadata.create_all(engine)
    ordinary = _experiment()
    formal = _experiment(formal=True, stamp=STAMP + timedelta(seconds=1))
    result_id = str(uuid4())
    with Session(engine, expire_on_commit=False) as db, db.begin():
        db.add(
            User(
                id=TESTER.user_id,
                token_hash=token_hash(TOKEN),
                role="tester",
                display_name="tester",
                enabled=True,
            )
        )
        db.add_all((ordinary, formal))
        db.flush()
        append_progress(db, ordinary)
        append_progress(db, formal)
        db.add(
            EvaluationResult(
                id=result_id,
                experiment_id=formal.id,
                case_id="private-case",
                repetition=1,
                status="RUNNING",
                version=1,
                expected={"private": "evidence"},
                actual=None,
                checks=[],
                trace=[],
                quota={},
                started_at=STAMP,
            )
        )
    settings = SimpleNamespace(profile="test")
    sent = []
    service = EvaluationService(engine, settings, dispatch=sent.append)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.engine = engine
    app.state.evaluation = service
    yield SimpleNamespace(
        engine=engine,
        service=service,
        app=app,
        settings=settings,
        ordinary=ordinary.id,
        formal=formal.id,
        result=result_id,
        sent=sent,
    )
    engine.dispose()


def _requests(formal, result, ordinary):
    experiment = f"/experiments/{formal}"
    return [
        ("GET", experiment, None),
        ("GET", f"{experiment}/progress?after=2147483647", None),
        ("GET", f"{experiment}/progress/stream?follow=false", None),
        ("GET", f"{experiment}/scenario-metrics", None),
        (
            "GET",
            f"{experiment}/scenario-metrics/coverage/contributors?basis_revision={'a' * 64}",
            None,
        ),
        ("GET", f"{experiment}/export?format=json", None),
        ("GET", f"{experiment}/export?format=csv", None),
        ("GET", f"/compare?left={formal}&right={ordinary}", None),
        ("GET", f"/compare?left={ordinary}&right={formal}", None),
        ("GET", f"/compare?left={formal}&right={formal}", None),
        ("GET", f"/results/{result}/supplemental-usage", None),
        (
            "POST",
            f"/results/{result}/reviews",
            {"expected_version": 1, "decision": "PASS", "notes": "fixture only"},
        ),
        ("POST", f"{experiment}/cancel", {"expected_version": 1}),
        ("POST", f"{experiment}/retry", {"expected_version": 1, "result_ids": [result]}),
    ]


def test_formal_experiment_is_indistinguishable_from_missing_across_ordinary_routes(isolated):
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "test"}
    with TestClient(isolated.app, headers=headers) as client:
        listed = client.get(BASE_PATH + "/experiments?limit=1")
        assert [row["id"] for row in listed.json()["items"]] == [isolated.ordinary]
        assert client.get(BASE_PATH + "/experiments?offset=1").json()["items"] == []
        missing = _requests(str(uuid4()), str(uuid4()), isolated.ordinary)
        for (method, path, body), (_, absent, absent_body) in zip(
            _requests(isolated.formal, isolated.result, isolated.ordinary), missing, strict=True
        ):
            result = client.request(method, BASE_PATH + path, json=body)
            nonexistent = client.request(method, BASE_PATH + absent, json=absent_body)
            assert result.status_code == nonexistent.status_code == 404, path
            assert result.json() == nonexistent.json(), path
        ordinary = BASE_PATH + f"/experiments/{isolated.ordinary}"
        assert client.get(ordinary + "/export").status_code == 200
        assert "evaluation-progress" in client.get(ordinary + "/progress/stream?follow=false").text
        assert client.get(BASE_PATH + "/cases?dataset=private-holdout").status_code == 404
    with Session(isolated.engine) as db:
        assert db.get(Experiment, isolated.formal).version == 1
        result = db.get(EvaluationResult, isolated.result)
        assert (result.status, result.version) == ("RUNNING", 1)
        assert db.scalar(select(EvaluationReview.id)) is None


def test_ordinary_create_and_import_cannot_promote_formal_bindings(isolated):
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "test"}
    with TestClient(isolated.app, headers=headers) as client:
        create = {
            "name": "ordinary",
            "dataset_id": "v0.2",
            "source": "fixture",
            "mode": "single",
            "strategy": "routed",
            "repetitions": 1,
            "concurrency": 1,
            "max_calls": 1,
            "budget_usd": 0,
            "formal_freeze_sha256": "a" * 64,
            "formal_condition_key": "single/routed",
        }
        assert client.post(BASE_PATH + "/experiments", json=create).status_code == 422
        ordinary_report = isolated.service.experiment(TESTER, isolated.ordinary)
        marked = {**ordinary_report, "formal_freeze_sha256": "a" * 64}
        unmarked_formal = {**ordinary_report, "id": isolated.formal}
        for index, (document, expected) in enumerate(
            [(marked, 422), (unmarked_formal, 422), (ordinary_report, 201)]
        ):
            content = json.dumps(document)
            response = client.post(
                BASE_PATH + "/imports",
                headers={"Idempotency-Key": f"import-{index}"},
                json={
                    "content": content,
                    "sha256": sha256(content.encode()).hexdigest(),
                    "origin": "fixture",
                },
            )
            assert response.status_code == expected
    with Session(isolated.engine) as db:
        assert len(list(db.scalars(select(Experiment.id)))) == 2
        assert len(list(db.scalars(select(EvaluationImport.id)))) == 1


def test_native_validation_cannot_snapshot_formal_ids(isolated, monkeypatch):
    class ReadConnection:
        def execution_options(self, *, isolation_level):
            assert isolation_level == "REPEATABLE READ"
            return nullcontext(self)

        def begin(self):
            return nullcontext()

        def exec_driver_sql(self, statement):
            assert statement == "SET TRANSACTION READ ONLY"

        def execute(self, _):
            _, database, username, cluster = TARGETS["test"]
            return SimpleNamespace(one=lambda: (database, username, cluster, STAMP))

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=ReadConnection)
    monkeypatch.setattr(evaluation_service, "Session", lambda _: Session(isolated.engine))
    service = EvaluationService(engine, isolated.settings)
    for experiment_id in (isolated.formal, str(uuid4())):
        request = SimpleNamespace(experiments=[SimpleNamespace(experiment_id=experiment_id)])
        with pytest.raises(BusinessError) as error:
            service.native_validation_snapshot(TESTER, None, request)
        assert (error.value.status, error.value.code) == (404, "EXPERIMENT_NOT_FOUND")


def test_queue_filters_formal_before_limit_and_refuses_polluted_and_cross_kind_delivery(
    isolated, monkeypatch
):
    journal = DeliveryJournal(isolated.engine)
    formal_job = journal.ensure("evaluation", isolated.formal)
    ordinary_job = journal.ensure("evaluation", isolated.ordinary)
    absent_id = str(uuid4())
    absent_job = journal.ensure("evaluation", absent_id)
    with Session(isolated.engine) as db, db.begin():
        db.get(DispatchJob, formal_job).next_at = STAMP - timedelta(days=1)
        db.get(DispatchJob, ordinary_job).next_at = STAMP
    assert journal.due(limit=1) == [ordinary_job]
    assert journal.reserve_publish(formal_job) is None and journal.claim(formal_job) is None
    assert queue_delivery.target_generation(isolated.engine, "evaluation", isolated.formal) is None
    assert queue_delivery.target_generation(isolated.engine, "evaluation", isolated.ordinary) == 0
    assert isolated.service.pending(limit=1) == [isolated.ordinary]
    isolated.service._dispatch(isolated.formal)
    isolated.service._dispatch(isolated.ordinary)
    assert isolated.sent == [isolated.ordinary]
    for driver in ("inline", "queued"):
        service = EvaluationService(isolated.engine, isolated.settings, execution_driver=driver)
        with pytest.raises(BusinessError) as error:
            service.process(isolated.formal, runner=None)
        assert error.value.code == "EXPERIMENT_NOT_FOUND"

    @contextmanager
    def runtime(_profile):
        yield isolated.settings, isolated.engine, None, None

    monkeypatch.setattr("aftercare.runtime.open_runtime", runtime)
    monkeypatch.setattr(
        "aftercare.evaluation.runner.EvaluationRunner",
        lambda *_a, **_k: pytest.fail("Runner opened"),
    )
    queue_delivery.process_target("test", "evaluation", isolated.formal, formal_job)
    queue_delivery.process_target("test", "agent", isolated.formal, formal_job)
    queue_delivery.process_target("test", "evaluation", isolated.formal, ordinary_job)
    queue_delivery.process_target("test", "evaluation", absent_id, absent_job)
    with Session(isolated.engine) as db:
        assert (
            db.get(DispatchJob, formal_job).claims == db.get(DispatchJob, ordinary_job).claims == 0
        )
        assert db.get(DispatchJob, absent_job).state == "DONE"
        assert db.get(DispatchJob, absent_job).claims == 1


def test_failed_queue_reconciliation_does_not_touch_formal_rows(isolated, monkeypatch):
    journal = DeliveryJournal(isolated.engine)
    jobs = [journal.ensure("evaluation", ident) for ident in (isolated.formal, isolated.ordinary)]
    with Session(isolated.engine) as db, db.begin():
        for job_id in jobs:
            # Seed below the fenced entry points to simulate a previously polluted job.
            db.get(DispatchJob, job_id).state = "FAILED"
    monkeypatch.setattr("aftercare.evaluation.runner.EvaluationRunner", lambda *_: None)
    queue_delivery.reconcile_failed(isolated.engine, isolated.settings)
    with Session(isolated.engine) as db:
        assert db.get(Experiment, isolated.ordinary).status == "FAILED"
        assert db.get(Experiment, isolated.formal).status == "QUEUED"
        assert db.get(EvaluationResult, isolated.result).status == "RUNNING"


def test_formal_binding_requires_a_pair_and_enforces_one_condition_per_freeze(isolated):
    def store(row):
        with Session(isolated.engine) as db, db.begin():
            db.add(row)

    for row in (
        _experiment(formal_freeze_sha256="a" * 64),
        _experiment(formal_condition_key="single/routed"),
        _experiment(formal=True),
    ):
        with pytest.raises(IntegrityError):
            store(row)
    store(_experiment(formal=True, formal_condition_key="multi/routed"))
    store(_experiment())


def test_formal_migration_is_additive_and_blocks_unmarking_private_experiments():
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    scripts = ScriptDirectory.from_config(config)
    revision = scripts.get_revision("20261110_eval_formal")
    assert scripts.get_current_head() == revision.revision
    assert revision.down_revision == "20261110_eval_progress"
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        revision.module.upgrade()
        revision.module.downgrade()
    upgrade, downgrade = output.getvalue().split("DO $$", 1)
    assert "ADD COLUMN formal_freeze_sha256 VARCHAR(64)" in upgrade
    assert "ADD COLUMN formal_condition_key VARCHAR(32)" in upgrade
    assert "UNIQUE (formal_freeze_sha256, formal_condition_key)" in upgrade
    assert "CHECK" in upgrade and "UPDATE" not in upgrade
    assert "LOCK TABLE eval_experiments IN ACCESS EXCLUSIVE MODE" in upgrade
    assert downgrade.index("FORMAL_EVALUATION_DOWNGRADE_BLOCKED") < downgrade.index(
        "DROP CONSTRAINT"
    )
