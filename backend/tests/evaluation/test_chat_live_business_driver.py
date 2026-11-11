import importlib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("verify_chat_live_business_path")


def test_driver_does_not_reclassify_ambiguous_writes_as_known_failure(driver):
    report = {"stage": "CUSTOMER_TWO_ORIGINALS", "mutations": {}, "queue": {}, "paid_phases": {}}
    assert driver._unknown(report, httpx.ReadTimeout("request outcome unknown"))
    assert not driver._unknown(report, driver.VerificationError("HTTP_EXPECTED_201_GOT_409"))
    report["stage"] = "QUEUE_TEXT"
    report["queue"]["TEXT"] = {"state": "PUBLISHED"}
    report["paid_phases"]["TEXT"] = {"wire": {"delivery_unknown": True}}
    assert driver._unknown(report, driver.VerificationError("LIVE_BUSINESS_NATIVE_OUTCOME_TIMEOUT"))


def test_driver_preserves_nonexecuting_model_candidate_and_requires_explicit_control(driver):
    snapshot = {
        "candidate": {
            "options": [{"id": "handoff", "action": "HUMAN_HANDOFF"}],
            "unresolved": [],
        },
        "checks": {},
    }
    eligible = {
        "status": "ELIGIBLE",
        "actions": ["REFUND"],
        "policy_ids": ["POL01"],
        "lines": [{"line_id": "L01", "available_cents": 4900}],
    }
    assert driver._review_choice(snapshot, eligible, ["m1", "m2"], False) is None
    modified = driver._review_choice(snapshot, eligible, ["m1", "m2"], True)
    assert modified["action"] == "MODIFY"
    assert modified["replacement"]["lines"] == [{"line_id": "L01", "amount_cents": 4900}]
    assert "CONTROL" in modified["reason"]
    assert snapshot["candidate"]["options"][0]["action"] == "HUMAN_HANDOFF"
    snapshot["candidate"]["unresolved"] = ["Missing fact"]
    assert driver._review_choice(snapshot, eligible, ["m1", "m2"], True) is None


def test_driver_prefers_program_passed_original_refund(driver):
    snapshot = {
        "candidate": {
            "options": [
                {
                    "id": "eligible",
                    "action": "REFUND",
                    "lines": [{"line_id": "L01", "amount_cents": 3500}],
                }
            ],
            "unresolved": [],
        },
        "checks": {"eligible": {"status": "PASSED"}},
    }
    review = driver._review_choice(snapshot, {}, [], True)
    assert review["action"] == "ACCEPT"
    assert review["option_id"] == "eligible"


def test_transport_success_is_not_a_validated_candidate_or_business_pass(driver):
    snapshot = {"original_run_id": "run", "candidate": {"options": [{"id": "refund"}]}}
    native = {"attempts": [{"status": "SUCCEEDED"}], "validated_result_events": []}
    assert not driver._model_candidate(native, snapshot)
    native["validated_result_events"] = [{"run_id": "run", "option_ids": []}]
    assert not driver._model_candidate(native, snapshot)
    native["validated_result_events"] = [{"run_id": "run", "option_ids": ["refund"]}]
    assert driver._model_candidate(native, snapshot)
    assert (
        driver._business_status(model_candidate=True, completed=True, agent_executable=True)
        == "PASSED"
    )
    assert (
        driver._business_status(model_candidate=True, completed=True, agent_executable=False)
        == "PARTIAL"
    )
    assert (
        driver._business_status(model_candidate=False, completed=False, agent_executable=False)
        == "PARTIAL"
    )


def test_settled_invalid_model_output_preserves_verified_transport_as_partial(driver, monkeypatch):
    wire = [{"run_id": "run", "native_logical_id": "logical", "native_ordinal": 1}]
    monkeypatch.setattr(driver, "_assert_wire", lambda *_: wire)
    report = {
        "vision_attempt": {"estimated_cost_microusd": 1500},
        "paid_phases": {"TEXT": {"turn_id": "turn"}},
    }
    run = {"run_id": "run", "turn_id": "turn", "projection": "PUBLISHED", "snapshot_id": "snapshot"}
    native = {
        "runs": [run],
        "counts": {"agent_runs": 1, "agent_model_attempts": 1},
        "attempts": [
            {
                "run_id": "run",
                "logical_id": "logical",
                "ordinal": 1,
                "status": "FAILED",
                "error_code": "INVALID_MODEL_OUTPUT",
                "estimated_cost_usd": "0.007",
                "transport_evidence": {"path": "DEFAULT_SYNC", "response": "SDK_RETURNED"},
            }
        ],
    }
    assert driver._verify_text(native, report) == run
    assert report["text_attempts"][0]["error_code"] == "INVALID_MODEL_OUTPUT"
    native["attempts"][0]["status"] = "UNKNOWN"
    with pytest.raises(driver.VerificationError, match="TEXT_NATIVE_BINDING_CHANGED"):
        driver._verify_text(native, report)


def test_paid_cleanup_keeps_wire_read_error_and_marks_dispatched_phase_unknown(driver, tmp_path):
    credential = tmp_path / "model-secret.json"
    credential.write_text('{"api_key":"synthetic-test-secret"}')
    malformed = tmp_path / "admission-01.json"
    malformed.write_text("{")
    malformed.chmod(0o600)
    phase = {}
    driver._record_paid_cleanup(tmp_path, credential, phase, removed=True)
    assert not credential.exists()
    assert phase["temporary_key_removed"] is True and phase["worker_removed"] is True
    assert phase["offline_worker_restored"] is False
    assert phase["wire_receipt_read_error"] == "JSONDecodeError"
    report = {
        "stage": "QUEUE_TEXT",
        "mutations": {},
        "queue": {},
        "paid_phases": {"TEXT": phase},
        "failure_native_readback": {"attempts": [{"status": "FAILED"}]},
    }
    failure = driver.VerificationError("LIVE_BUSINESS_WIRE_RECEIPT_UNVERIFIED")
    assert not driver._unknown(report, failure)
    report["queue"]["TEXT"] = {"state": "PUBLISHED"}
    assert driver._unknown(report, failure)
    report["paid_phases"]["TEXT"] = {
        "wire": {"delivery_unknown": False},
        "worker_removed": False,
    }
    assert driver._unknown(report, failure)
    report["paid_phases"]["TEXT"]["worker_removed"] = True
    assert not driver._unknown(report, failure)
