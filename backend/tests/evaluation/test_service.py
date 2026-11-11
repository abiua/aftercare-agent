from __future__ import annotations

import csv
import io
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, current_user
from aftercare.business.errors import BusinessError
from aftercare.business.rules import now
from aftercare.config import Settings
from aftercare.db import Base, User
from aftercare.evaluation.api import router
from aftercare.evaluation.dataset import load_cases, load_references
from aftercare.evaluation.runner import EvaluationRunner, _interrupted_audit, score
from aftercare.evaluation.service import EvaluationService, ExperimentInput, _allocate
from aftercare.evaluation.tables import EvaluationResult, Experiment
from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

TESTER = UserContext(user_id="tester", role="tester")
CUSTOMER = UserContext(user_id="customer", role="customer", customer_id="C001")


class MemoryObjects:
    def __init__(self):
        self.items = {}

    def put(self, key, data, content_type):
        self.items[key] = data

    def get(self, key, expected_size):
        return self.items[key]

    def presign(self, key, content_type):
        return "https://example.test"


@pytest.fixture
def system(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eval.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            User(
                id=TESTER.user_id,
                display_name="tester",
                token_hash="fake",
                role="tester",
                enabled=True,
            )
        )
    settings = Settings(
        profile="test",
        database_url="",
        redis_url="",
        object_endpoint="",
        object_access_key="",
        object_secret_key="",
    )
    dispatches = []
    service = EvaluationService(engine, settings, dispatch=dispatches.append)
    storage = MemoryObjects()
    runner = EvaluationRunner(engine, settings, InMemorySaver(), storage=storage)
    yield SimpleNamespace(
        service=service, runner=runner, storage=storage, dispatches=dispatches, engine=engine
    )
    engine.dispose()


def _input(**changes):
    return ExperimentInput(
        **{
            "name": "业务演示核对",
            "dataset_id": "v0.2",
            "split": "dev",
            "mode": "single",
            "strategy": "routed",
            "repetitions": 1,
            "concurrency": 1,
            "max_calls": 10,
            "budget_usd": 0,
            "source": "fixture",
            "case_ids": ["AC02-C001"],
            **changes,
        }
    )


def _case(case_id):
    return next(case for case in load_cases() if case["id"] == case_id), load_references()[case_id]


def test_datasets_roles_cancel_replay_and_csv(system):
    service = system.service
    with pytest.raises(BusinessError) as error:
        service.datasets(CUSTOMER)
    assert error.value.status == 403
    assert service.datasets(TESTER)[0]["case_count"] == 144
    cases = service.cases(TESTER, "v0.2", split="dev")
    assert len(cases) == 60
    assert cases[0]["review_status"] == "PENDING"
    request = _input(name="=HYPERLINK(1)")
    plan = service.create(TESTER, request, "request")
    assert service.create(TESTER, request, "request")["id"] == plan["id"]
    assert system.dispatches == [plan["id"]]
    with pytest.raises(BusinessError) as error:
        service.create(TESTER, _input(name="different"), "request")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    cancelled = service.cancel(TESTER, plan["id"], expected_version=1, key="cancel")
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["counts"]["started"] == 0 and cancelled["counts"]["not_run"] == 1
    assert cancelled["results"][0]["status"] == "NOT_RUN"
    assert service.cancel(TESTER, plan["id"], expected_version=1, key="cancel") == cancelled
    output = list(csv.DictReader(io.StringIO(service.export_csv(TESTER, plan["id"]))))
    assert output[0]["name"] == "'=HYPERLINK(1)" and output[0]["started"] == "0"
    assert output[0]["task_success"] == ""


def test_offline_application_result_and_append_only_review(system):
    service = system.service
    plan = service.create(TESTER, _input(), "plan")
    service.process(plan["id"], system.runner)
    report = service.experiment(TESTER, plan["id"])
    result = report["results"][0]
    assert report["status"] == "COMPLETED"
    assert report["counts"]["started"] == report["counts"]["finished"] == 1
    assert result["status"] == "NEEDS_REVIEW", result
    assert result["actual"]["observed_source"] == "application_offline"
    assert not result["actual"]["model_attempts"] and result["actual"]["model_audit_complete"]
    assert all(row["status"] == "PASS" for row in result["checks"])
    assert "expected" not in result["actual"]["events"]
    assert all(key.startswith("evaluation/eval_") for key in system.storage.items)
    review = service.review(
        TESTER,
        result["id"],
        expected_version=result["version"],
        key="review",
        decision="PASS",
        notes="已对照执行记录。",
    )
    assert review == service.review(
        TESTER,
        result["id"],
        expected_version=result["version"],
        key="review",
        decision="PASS",
        notes="已对照执行记录。",
    )
    with pytest.raises(BusinessError) as error:
        service.review(
            TESTER,
            result["id"],
            expected_version=result["version"],
            key="stale",
            decision="FAIL",
            notes="复核依据冲突。",
        )
    assert error.value.code == "VERSION_CONFLICT"
    report = service.experiment(TESTER, plan["id"])
    result = report["results"][0]
    assert result["status"] == "PASSED" and result["machine_status"] == "NEEDS_REVIEW"
    assert report["gate"]["status"] == "INCOMPLETE"
    service.review(
        TESTER,
        result["id"],
        expected_version=result["version"],
        key="correction",
        decision="UNKNOWN",
        notes="等待补充人工证据。",
    )
    result = service.experiment(TESTER, plan["id"])["results"][0]
    assert result["status"] == "NEEDS_REVIEW"
    assert [row["decision"] for row in result["reviews"]] == ["PASS", "UNKNOWN"]


@pytest.mark.parametrize(
    "case_id, source",
    [
        ("AC02-C041", "fixture"),
        ("AC02-C086", "fixture"),
        ("AC02-R022", "fault"),
        ("AC02-R023", "fault"),
    ],
)
def test_application_runner_actual_cases(system, case_id, source):
    case, reference = _case(case_id)
    result = system.runner.run(
        str(uuid4()),
        case,
        reference,
        {"source": source, "mode": "single", "strategy": "routed"},
        {"calls": 0, "budget_usd": "0"},
        cancelled=lambda: False,
    )
    assert result["status"] == "NEEDS_REVIEW", result
    if case_id == "AC02-R023":
        assert result["actual"]["events"]["e03"]["output"] is None
        assert all(
            row["status"] == "PASS"
            for row in result["checks"]
            if row["path"] in {"status", "reason"}
        )
        assert result["actual"]["model_attempts"][0]["status"] == "UNKNOWN"
        assert result["actual"]["observed_source"] == "fault_in_process"
    else:
        assert all(row["status"] == "PASS" for row in result["checks"])


def test_comparison_rejects_changed_evidence_and_both_axes(system):
    service = system.service
    base = service.create(TESTER, _input(), "base")
    other = service.create(TESTER, _input(mode="multi"), "multi")
    incomplete = service.compare(TESTER, base["id"], other["id"])
    assert incomplete["compatible"] and incomplete["metrics"] == []
    assert incomplete["pairing"] == {"planned": 1, "finished": 0, "complete": False}
    assert incomplete["rows"][0]["change"] == "UNPAIRED"
    service.process(base["id"], system.runner)
    assert not service.compare(TESTER, base["id"], other["id"])["metrics"]
    service.process(other["id"], system.runner)
    finished = service.compare(TESTER, base["id"], other["id"])
    assert finished["pairing"] == {"planned": 1, "finished": 1, "complete": True}
    assert finished["metrics"][0]["left_denominator"] == 1
    changed = service.create(TESTER, _input(mode="multi", strategy="analysis"), "multi-model")
    assert not service.compare(TESTER, base["id"], changed["id"])["compatible"]
    budget = service.create(TESTER, _input(max_calls=2), "budget")
    assert not service.compare(TESTER, base["id"], budget["id"])["compatible"]


def test_live_quota_sums_and_gate(system):
    request = _input(source="live", max_calls=7, budget_usd="0.000011")
    quotas = _allocate(request, list(range(4)))
    assert sum(row["calls"] for row in quotas) == 7
    assert sum(float(row["budget_usd"]) for row in quotas) == pytest.approx(0.000011)
    assert not _allocate(request, list(range(8)))[-1]["calls"]
    with pytest.raises(BusinessError) as error:
        system.service.create(TESTER, request, "live")
    assert error.value.code == "LIVE_EVALUATION_UNAVAILABLE"


def test_score_does_not_treat_missing_as_explicit_null():
    reference = {"checks": [{"event_id": "e", "path": "output.rows.0.receipt", "equals": None}]}
    assert score(reference, {})[0]["status"] == "UNKNOWN"
    assert score(reference, {"e": {"output": {"rows": [{"receipt": None}]}}})[0]["status"] == "PASS"


def test_http_uses_tester_rbac_and_json_export(system):
    app = FastAPI()
    app.state.evaluation = system.service
    app.include_router(router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: CUSTOMER
    with TestClient(app) as client:
        assert client.get("/api/evaluation/datasets").status_code == 403
        app.dependency_overrides[current_user] = lambda: TESTER
        response = client.post(
            "/api/evaluation/experiments",
            json=_input().model_dump(mode="json"),
            headers={"Idempotency-Key": "http"},
        )
        assert response.status_code == 201, response.text
        experiment_id = response.json()["id"]
        response = client.get(f"/api/evaluation/experiments/{experiment_id}/export")
        assert response.status_code == 200
        assert response.json()["counts"]["not_run"] == 1
        assert "attachment" in response.headers["Content-Disposition"]


def test_cancelled_experiment_recovers_interrupted_running_attempt(system):
    service = system.service
    plan = service.create(TESTER, _input(), "plan")
    result_id = plan["results"][0]["id"]
    with Session(system.engine) as db, db.begin():
        db.get_one(Experiment, plan["id"]).status = "RUNNING"
        result = db.get_one(EvaluationResult, result_id)
        result.status, result.started_at = "RUNNING", now()
    service.cancel(TESTER, plan["id"], expected_version=1, key="cancel")
    assert service.pending() == [plan["id"]]
    system.runner.recover_audit = lambda _: (
        {
            "model_attempts": [{"status": "UNKNOWN", "estimated_cost_usd": None}],
            "model_audit_complete": False,
        },
        [{"kind": "attempt", "payload": {"status": "STARTED"}}],
    )
    service.process(plan["id"], system.runner)
    report = service.experiment(TESTER, plan["id"])
    assert report["status"] == "CANCELLED" and service.pending() == []
    assert report["counts"]["finished"] == 1
    assert report["results"][0]["status"] == "ERROR"
    assert report["results"][0]["failure_reason"] == "WORKER_INTERRUPTED"
    assert report["results"][0]["actual"]["model_attempts"][0]["status"] == "UNKNOWN"
    metrics = {row["key"]: row["value"] for row in report["metrics"]}
    assert metrics["model_attempts"] == 1
    assert metrics["model_audit_coverage"] == 0
    assert metrics["cost"] is None


def test_read_only_recovery_preserves_interrupted_attempt_audit(system):
    timestamp = now()
    with Session(system.engine) as db, db.begin():
        db.add(
            AgentRun(
                id="run",
                thread_id="thread",
                case_id="case",
                owner_id="actor",
                actor={},
                status="RUNNING",
                mode="single",
                strategy="routed",
                message="fixture",
                case_version=1,
                cycle=0,
                event_seq=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        db.flush()
        db.add(
            ModelAttempt(
                run_id="run",
                cycle=0,
                logical_id="single:0:0",
                ordinal=1,
                status="STARTED",
                started_at=timestamp,
                metadata_json={"model": "fixture", "alias": "economy"},
            )
        )
        db.add(
            RunEvent(
                run_id="run",
                seq=1,
                kind="attempt",
                payload={"status": "STARTED"},
                created_at=timestamp,
            )
        )
    actual, trace = _interrupted_audit(system.engine, "isolated")
    assert actual["model_attempts"][0]["status"] == "UNKNOWN"
    assert actual["model_attempts"][0]["persisted_status"] == "STARTED"
    assert actual["model_attempts"][0]["estimated_cost_usd"] is None
    assert trace[0]["payload"]["status"] == "STARTED"
    with Session(system.engine) as db:
        assert db.get_one(ModelAttempt, ("run", 0, "single:0:0", 1)).status == "STARTED"


def test_worker_rejects_changed_embedding_settings(system):
    service = system.service
    plan = service.create(TESTER, _input(), "plan")
    settings = system.runner.settings
    system.runner.settings = system.runner.settings.model_copy(update={"embedding_enabled": True})
    service.process(plan["id"], system.runner)
    report = service.experiment(TESTER, plan["id"])
    assert report["status"] == "FAILED" and report["counts"]["started"] == 0
    original = report["results"][0]
    assert original["status"] == "NOT_RUN" and original["failure_reason"] == "FROZEN_INPUT_CHANGED"

    child = service.retry(
        TESTER,
        report["id"],
        expected_version=report["version"],
        result_ids=[original["id"]],
        key="retry-not-run",
    )
    assert child["parent_experiment_id"] == report["id"]
    assert child["results"][0]["retry_of_result_id"] == original["id"]
    for digest_key in ("input_digest", "reference_digest"):
        assert child["config"][digest_key] == report["config"][digest_key]
    system.runner.settings = settings
    service.process(child["id"], system.runner)
    assert service.experiment(TESTER, child["id"])["counts"]["started"] == 1
    assert service.experiment(TESTER, report["id"]) == report


def test_dependency_lock_is_frozen_for_create_retry_and_worker(system, monkeypatch):
    from aftercare.evaluation import service as evaluation_service

    original_lock = evaluation_service._dependency_lock_sha256()
    assert original_lock is not None
    service = system.service
    plan = service.create(TESTER, _input(), "original-lock")
    legacy = service.create(TESTER, _input(), "legacy-lock")
    assert plan["config"]["dependency_lock_sha256"] == original_lock
    with Session(system.engine) as db, db.begin():
        row = db.get_one(Experiment, legacy["id"])
        row.config = {
            key: value for key, value in row.config.items() if key != "dependency_lock_sha256"
        }

    # Keep the loaded code fingerprint fixed to exercise the separate worker check.
    changed_lock = "1" * 64
    monkeypatch.setattr(
        evaluation_service, "require_current_code", lambda: plan["config"]["code_digest"]
    )
    monkeypatch.setattr(evaluation_service, "_dependency_lock_sha256", lambda: changed_lock)
    service.process(plan["id"], system.runner)
    service.process(legacy["id"], system.runner)
    report = service.experiment(TESTER, plan["id"])
    assert report["counts"]["started"] == 0
    result = report["results"][0]
    assert (result["status"], result["failure_reason"]) == ("NOT_RUN", "FROZEN_INPUT_CHANGED")
    old_report = service.experiment(TESTER, legacy["id"])
    assert old_report["counts"]["started"] == 1
    assert "dependency_lock_sha256" not in old_report["config"]
    child = service.retry(
        TESTER,
        plan["id"],
        expected_version=report["version"],
        result_ids=[result["id"]],
        key="retry-lock",
    )
    assert child["config"]["dependency_lock_sha256"] == changed_lock
    assert (
        service.experiment(TESTER, plan["id"])["config"]["dependency_lock_sha256"] == original_lock
    )


def test_worker_refuses_application_storage_target(system):
    plan = system.service.create(TESTER, _input(), "plan")
    settings = system.runner.settings.model_copy(
        update={"object_endpoint": "http://127.0.0.1:58350"}
    )
    unsafe = EvaluationRunner(system.engine, settings, InMemorySaver())
    system.service.process(plan["id"], unsafe)
    report = system.service.experiment(TESTER, plan["id"])
    assert report["results"][0]["failure_reason"] == "TEST_OBJECT_STORAGE_REQUIRED"
    assert report["results"][0]["status"] == "ERROR"
    assert system.storage.items == {}


def _fake_provider(system, transport):
    model_spec = ModelSpec(
        alias="economy",
        provider="fixture",
        model="timeout-double",
        endpoint="http://127.0.0.1:9",
        api_key_env="UNUSED_FIXTURE_KEY",
        input_usd_per_million="1",
        output_usd_per_million="1",
        price_date=date(2026, 11, 10),
        source="fixture pricing",
    )
    registry = Registry(version="fixture", allow_unverified=True, models=[model_spec])

    settings = system.runner.settings.model_copy(update={"model_enabled": True})
    return EvaluationRunner(
        system.engine,
        settings,
        InMemorySaver(),
        model=ModelRunner(registry, transport=transport),
        storage=system.storage,
    )


def test_transport_timeout_is_reported_with_uncertain_usage(system):
    def timeout(*_args):
        raise TimeoutError()

    runner = _fake_provider(system, timeout)
    case, reference = _case("AC02-C081")
    result = runner.run(
        str(uuid4()),
        case,
        reference,
        {"source": "live", "mode": "single", "strategy": "economy"},
        {"calls": 1, "budget_usd": "0.5"},
        cancelled=lambda: False,
    )
    assert result["status"] == "TIMEOUT" and result["failure_reason"] == "MODEL_TIMEOUT"
    attempts = result["actual"]["model_attempts"]
    assert len(attempts) == 1 and attempts[0]["status"] == "UNKNOWN"
    assert attempts[0]["usage"] is None and attempts[0]["estimated_cost_usd"] is None


def test_cancellation_during_attempt_prevents_followup_call(system):
    calls = []

    def malformed(*args):
        calls.append(args)
        return AIMessage(content="malformed output")

    runner = _fake_provider(system, malformed)
    case, reference = _case("AC02-C081")
    result = runner.run(
        str(uuid4()),
        case,
        reference,
        {"source": "live", "mode": "single", "strategy": "economy"},
        {"calls": 3, "budget_usd": "0.5"},
        cancelled=lambda: False,
        attempt_admission=lambda _: not calls,
    )
    assert result["status"] == "ERROR" and result["failure_reason"] == "EXPERIMENT_CANCELLED"
    assert len(calls) == len(result["actual"]["model_attempts"]) == 1
    assert result["actual"]["model_attempts"][0]["status"] == "FAILED"


def test_stale_process_cannot_create_or_execute_experiment(system, monkeypatch, tmp_path):
    from aftercare.evaluation import service as evaluation_service

    package = tmp_path / "backend" / "src" / "aftercare"
    source = package / "intelligence" / "policy_versions.py"
    source.parent.mkdir(parents=True)
    source.write_text("SCOPED_VERSION = 1\n")
    (package.parents[1] / "uv.lock").write_text("frozen lock")
    disk_fingerprint = evaluation_service._source_fingerprint
    monkeypatch.setattr(evaluation_service, "_RUNTIME_CODE_DIGEST", disk_fingerprint(package))
    monkeypatch.setattr(
        evaluation_service, "_source_fingerprint", lambda: disk_fingerprint(package)
    )

    service = system.service
    queued = service.create(TESTER, _input(), "original")
    assert queued["config"]["code_digest"] == evaluation_service.require_current_code()
    source.write_text("SCOPED_VERSION = 2\n")

    with pytest.raises(BusinessError) as error:
        service.create(TESTER, _input(), "stale")
    assert error.value.code == "CODE_RESTART_REQUIRED"
    with pytest.raises(BusinessError) as error:
        service.process(queued["id"], system.runner)
    assert error.value.code == "CODE_RESTART_REQUIRED"
    assert service.experiment(TESTER, queued["id"]) == queued
    assert system.dispatches == [queued["id"]]
    assert system.storage.items == {}


def test_imported_json_never_becomes_trusted_experiment(system):
    import json
    from hashlib import sha256

    from aftercare.evaluation.imports import ImportInput

    report = system.service.create(TESTER, _input(), "plan")
    report["gate"]["status"] = "PASS"
    content = json.dumps(report)
    request = ImportInput(
        content=content, sha256=sha256(content.encode()).hexdigest(), origin="test transfer"
    )
    imported = system.service.import_report(TESTER, request, "import")
    assert imported["source"] == "imported_unverified"
    assert system.service.import_report(TESTER, request, "import") == imported
    assert system.service.imported_report(TESTER, imported["id"])["report"] == report
    assert len(system.service.experiments(TESTER)) == 1
    assert system.service.experiments(TESTER)[0]["gate"]["status"] == "INCOMPLETE"
    with pytest.raises(BusinessError) as error:
        system.service.import_report(
            TESTER, request.model_copy(update={"sha256": "0" * 64}), "tamper"
        )
    assert error.value.code == "REPORT_HASH_MISMATCH"


def test_retry_links_parent_without_overwriting_old_attempt(system):
    service = system.service
    plan = service.create(TESTER, _input(), "original")
    service.process(plan["id"], system.runner)
    before = service.experiment(TESTER, plan["id"])
    row = before["results"][0]
    payload = {"expected_version": before["version"], "result_ids": [row["id"]], "key": "retry"}
    after = service.retry(TESTER, before["id"], **payload)
    assert after["parent_experiment_id"] == before["id"]
    assert after["results"][0]["retry_of_result_id"] == row["id"]
    assert service.retry(TESTER, before["id"], **payload)["id"] == after["id"]
    service.process(after["id"], system.runner)
    assert service.experiment(TESTER, before["id"]) == before
    new = service.experiment(TESTER, after["id"])["results"][0]
    assert new["actual"]["namespace"] != row["actual"]["namespace"]


def test_load_source_records_real_repeated_isolated_access(system):
    service = system.service
    request = _input(
        source="load", concurrency=2, load_reads=3, case_ids=["AC02-C001", "AC02-C041"]
    )
    plan = service.create(TESTER, request, "load")
    service.process(plan["id"], system.runner)
    report = service.experiment(TESTER, plan["id"])
    assert report["counts"]["finished"] == 2
    assert all(row["actual"]["observed_source"] == "application_load" for row in report["results"])
    assert sum(len(row["actual"]["load_operations"]) for row in report["results"]) == 6
    metric = next(row for row in report["metrics"] if row["key"] == "load_operation_success")
    assert (metric["numerator"], metric["denominator"]) == (6, 6)


def test_worker_lost_lease_cannot_admit_or_publish(system):
    service = system.service
    plan = service.create(TESTER, _input(), "plan")
    owned = True
    service.attempt_admission = lambda _: owned
    real_run = system.runner.run

    def interrupted(*args, **kwargs):
        nonlocal owned
        outcome = real_run(*args, **kwargs)
        owned = False
        assert not kwargs["queue_admission"](None)
        assert kwargs["attempt_admission"](None)
        return outcome

    system.runner.run = interrupted
    service.process(plan["id"], system.runner)
    abandoned = service.experiment(TESTER, plan["id"])
    assert abandoned["results"][0]["status"] == "RUNNING"
    assert abandoned["results"][0]["actual"] is None
    owned = True
    service.process(plan["id"], system.runner)
    recovered = service.experiment(TESTER, plan["id"])
    assert recovered["results"][0]["failure_reason"] == "WORKER_INTERRUPTED"


def test_gate_freezes_external_human_gold_against_exact_case_and_reference(
    system, tmp_path, monkeypatch
):
    import json

    from aftercare.business.rules import digest
    from aftercare.evaluation.gates import GatePolicy

    case, reference = _case("AC02-C001")
    gold_file = tmp_path / "gold.json"
    gold = [
        {
            "dataset_id": "v0.2",
            "case_id": case["id"],
            "case_sha256": digest(case),
            "reference_sha256": digest(reference),
            "reviewer_id": "independent-test-reviewer",
            "reviewed_at": "2026-11-10T04:00:00+00:00",
            "source": "unit-only evidence",
        }
    ]
    gold_file.write_text(json.dumps(gold))
    monkeypatch.setenv("AFTERCARE_EVALUATION_GOLD_PATH", str(gold_file))
    system.service.gate_policies = [
        GatePolicy.model_validate(
            {
                "id": "test",
                "version": "1",
                "name": "test",
                "source": "unit-only criteria",
                "thresholds": {
                    "min_task_success": 1,
                    "min_independent_cases": 1,
                    "require_live": False,
                },
            }
        )
    ]
    request = _input(gate_policy_id="test")
    plan = system.service.create(TESTER, request, "correct")
    assert plan["results"][0]["expected"]["label_source"] == "INDEPENDENT_HUMAN"
    system.service.process(plan["id"], system.runner)
    completed = system.service.experiment(TESTER, plan["id"])
    assert completed["gate"]["status"] == "INCOMPLETE"
    result = completed["results"][0]
    system.service.review(
        TESTER,
        result["id"],
        expected_version=result["version"],
        key="review",
        decision="PASS",
        notes="单元测试的人工证据记录。",
    )
    assert system.service.experiment(TESTER, plan["id"])["gate"]["status"] == "PASS"
    gold[0]["case_sha256"] = "0" * 64
    gold_file.write_text(json.dumps(gold))
    with pytest.raises(BusinessError) as error:
        system.service.create(TESTER, request, "wrong-digest")
    assert error.value.code == "GOLD_SNAPSHOT_MISMATCH"


def test_inline_plan_cannot_be_recovered_or_claimed_by_queue(system):
    queued = system.service
    inline = EvaluationService(
        system.engine,
        system.runner.settings,
        execution_driver="inline",
        dispatch=lambda _: pytest.fail("Inline work published to queue"),
    )
    cli_plan = inline.create(TESTER, _input(), "same-key")
    queue_plan = queued.create(TESTER, _input(), "same-key")
    assert cli_plan["config"]["execution_driver"] == "inline"
    assert queue_plan["config"]["execution_driver"] == "queued"
    assert queued.pending() == [queue_plan["id"]]
    assert inline.pending() == []
    with pytest.raises(BusinessError) as error:
        queued.process(cli_plan["id"], system.runner)
    assert error.value.code == "EXECUTION_DRIVER_MISMATCH"
    with pytest.raises(BusinessError) as error:
        inline.process(queue_plan["id"], system.runner)
    assert error.value.code == "EXECUTION_DRIVER_MISMATCH"
    assert queued.experiment(TESTER, cli_plan["id"])["status"] == "QUEUED"
    inline.process(cli_plan["id"], system.runner)
    assert inline.experiment(TESTER, cli_plan["id"])["status"] == "COMPLETED"
    assert queued.pending() == [queue_plan["id"]]


def test_fixture_persists_actual_policy_and_lexical_retrieval_evidence(system):
    from aftercare.evaluation.retrieval_evidence import SCHEMA_VERSION

    service = system.service
    plan = service.create(TESTER, _input(case_ids=["AC02-C081"]), "retrieval-evidence")
    assert plan["config"]["retrieval_snapshot"]["mode"] == "BM25"
    assert plan["config"]["retrieval_snapshot"]["encoder"] is None
    assert plan["config"]["policy_snapshot"]["schema_version"] == SCHEMA_VERSION
    service.process(plan["id"], system.runner)
    (row,) = service.experiment(TESTER, plan["id"])["results"]
    actual = row["actual"]["retrieval_evidence"]
    assert actual["catalogue_sha256"] == row["quota"]["policy_catalogue_sha256"]
    assert actual["selector_sha256"] == plan["config"]["policy_snapshot"]["selector_sha256"]
    assert actual["index"]["state"] == "NOT_APPLICABLE" and actual["index"]["keys"] == []
    assert actual["encoder_manifest_sha256"] is None
    (retrieval,) = [event["payload"] for event in row["trace"] if event.get("kind") == "retrieval"]
    assert retrieval["mode"] == "BM25"
    assert retrieval["retrieval_evidence"]["candidate_keys"]
    assert retrieval["retrieval_evidence"]["vector_index"] is None
    (binding,) = [
        event["payload"] for event in row["trace"] if event.get("kind") == "context_binding"
    ]
    assert binding["policy_resolution_sha256"] == retrieval["snapshot"]
    assert binding["selected_document_keys"] == retrieval["retrieval_evidence"]["candidate_keys"]
