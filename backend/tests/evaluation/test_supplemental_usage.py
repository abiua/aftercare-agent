from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from evaluation_technical_fixtures import TechnicalCatalogue
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.rules import now
from aftercare.db import Base
from aftercare.evaluation import service as evaluation
from aftercare.evaluation.supplemental_usage import changes
from aftercare.evaluation.tables import EvaluationResult, Experiment

TESTER = UserContext(user_id="tester", role="tester")
SUPPORT = UserContext(user_id="support", role="support")
ATTEMPT_ID = {"run_id": "run-a", "cycle": 0, "logical_id": "single:0:0", "ordinal": 1}


def test_changes_uses_full_attempt_key_and_preserves_read_time_projection():
    raw = {**ATTEMPT_ID, "status": "STARTED", "finished_at": None}
    frozen = {**ATTEMPT_ID, "status": "UNKNOWN", "persisted_status": "STARTED"}
    assert changes([raw], [frozen]) == ([], 0, 0)
    assert changes([raw], [{**frozen, "run_id": "run-b"}]) == ([], 1, 1)
    assert changes([raw], [{key: value for key, value in frozen.items() if key != "run_id"}]) == (
        [],
        1,
        1,
    )
    assert changes([raw], [frozen, frozen]) == ([], 1, 2)
    assert changes([], [frozen]) == ([], 0, 1)
    assert changes([], []) == ([], 0, 0)


def test_changes_exports_only_safe_usage_and_exact_cost_for_late_attempt():
    stamp = now()
    raw = {
        **ATTEMPT_ID,
        "status": "SUCCEEDED",
        "usage": {
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
            "input_token_details": {"cache_read": 2, "api_key": "secret"},
            "output_token_details": {"reasoning": 0, "audio": True, "other": -1},
            "text": "secret",
        },
        "estimated_cost_usd": "0.00002",
        "finished_at": stamp,
        "result": {"content": "secret"},
    }
    frozen = {**ATTEMPT_ID, "status": "UNKNOWN", "persisted_status": "STARTED"}
    difference, unmatched, unmatched_snapshot = changes([raw], [frozen])
    assert unmatched == unmatched_snapshot == 0
    assert difference == [
        {
            **ATTEMPT_ID,
            "status": "SUCCEEDED",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 1,
                "total_tokens": 6,
                "input_token_details": {"cache_read": 2},
                "output_token_details": {"reasoning": 0},
            },
            "estimated_cost_usd": "0.00002",
            "finished_at": stamp.isoformat(),
            "snapshot_status": "UNKNOWN",
            "snapshot_usage": None,
            "snapshot_estimated_cost_usd": None,
        }
    ]


@pytest.fixture
def frozen_result():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    experiment_id, result_id = str(uuid4()), str(uuid4())
    stamp = now()
    actual = {
        "observed_source": "interrupted_attempt_audit",
        "model_attempts": [{**ATTEMPT_ID, "status": "UNKNOWN", "persisted_status": "STARTED"}],
        "model_audit_complete": False,
    }
    with Session(engine) as db, db.begin():
        db.add(
            Experiment(
                id=experiment_id,
                name="interrupted",
                created_by=TESTER.user_id,
                status="CANCELLED",
                config={"source": "fixture"},
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.add(
            EvaluationResult(
                id=result_id,
                experiment_id=experiment_id,
                case_id="dev-1",
                repetition=1,
                status="ERROR",
                failure_reason="WORKER_INTERRUPTED",
                expected={},
                actual=actual,
                checks=[],
                trace=[{"kind": "attempt", "payload": {"status": "STARTED"}}],
                quota={},
                started_at=stamp,
                finished_at=stamp,
            )
        )
    yield engine, experiment_id, result_id
    engine.dispose()


def test_supplemental_read_keeps_frozen_actual_metrics_and_gate(frozen_result, monkeypatch):
    engine, experiment_id, result_id = frozen_result
    service = evaluation.EvaluationService(engine, SimpleNamespace(profile="test"), catalog=TechnicalCatalogue())
    seen = []

    def read_attempts(engine_seen, result_seen):
        seen.append((engine_seen, result_seen))
        return [
            {
                **ATTEMPT_ID,
                "status": "SUCCEEDED",
                "usage": {"input_tokens": 5, "output_tokens": 1},
                "estimated_cost_usd": "0.00002",
                "finished_at": now(),
            }
        ], None

    monkeypatch.setattr(evaluation, "read_supplemental_attempts", read_attempts)
    baseline = deepcopy(service.experiment(TESTER, experiment_id))
    supplement = service.supplemental_usage(TESTER, result_id)
    assert supplement["status"] == "AVAILABLE"
    assert supplement["unavailable_reason"] is None and supplement["unmatched_attempt_count"] == 0
    assert supplement["unmatched_snapshot_attempt_count"] == 0
    assert supplement["as_of"] and supplement["result_id"] == result_id
    assert supplement["attempts"][0]["estimated_cost_usd"] == "0.00002"
    assert seen == [(engine, result_id)]
    assert service.experiment(TESTER, experiment_id) == baseline


def test_supplemental_read_requires_tester_test_profile_and_existing_result(
    frozen_result, monkeypatch
):
    engine, _, result_id = frozen_result
    service = evaluation.EvaluationService(engine, SimpleNamespace(profile="test"), catalog=TechnicalCatalogue())
    app_service = evaluation.EvaluationService(engine, SimpleNamespace(profile="app"))
    monkeypatch.setattr(
        evaluation, "read_supplemental_attempts", lambda *_: pytest.fail("Unexpected schema access")
    )
    for scoped_service, actor, identifier, code in (
        (service, SUPPORT, result_id, "EVALUATION_FORBIDDEN"),
        (app_service, TESTER, result_id, "EVALUATION_REQUIRES_TEST"),
        (service, TESTER, str(uuid4()), "RESULT_NOT_FOUND"),
    ):
        with pytest.raises(BusinessError) as error:
            scoped_service.supplemental_usage(actor, identifier)
        assert error.value.code == code
    assert service.capabilities(TESTER)["eval_supplemental_usage"] is True
    assert app_service.capabilities(TESTER)["eval_supplemental_usage"] is False


@pytest.mark.parametrize("reason", ["SCHEMA_NOT_FOUND", "AUDIT_UNREADABLE"])
def test_unavailable_raw_evidence_remains_distinct_from_no_changes(
    frozen_result, monkeypatch, reason
):
    engine, _, result_id = frozen_result
    service = evaluation.EvaluationService(engine, SimpleNamespace(profile="test"), catalog=TechnicalCatalogue())
    monkeypatch.setattr(evaluation, "read_supplemental_attempts", lambda *_: (None, reason))
    supplement = service.supplemental_usage(TESTER, result_id)
    assert supplement["status"] == "UNAVAILABLE" and supplement["unavailable_reason"] == reason
    assert supplement["attempts"] == [] and supplement["unmatched_attempt_count"] == 0
    assert supplement["unmatched_snapshot_attempt_count"] == 0


def test_missing_raw_attempt_is_reported_as_unmatched(frozen_result, monkeypatch):
    engine, _, result_id = frozen_result
    service = evaluation.EvaluationService(engine, SimpleNamespace(profile="test"), catalog=TechnicalCatalogue())
    monkeypatch.setattr(evaluation, "read_supplemental_attempts", lambda *_: ([], None))
    supplement = service.supplemental_usage(TESTER, result_id)
    assert supplement["status"] == "AVAILABLE" and supplement["attempts"] == []
    assert supplement["unmatched_attempt_count"] == 0
    assert supplement["unmatched_snapshot_attempt_count"] == 1
