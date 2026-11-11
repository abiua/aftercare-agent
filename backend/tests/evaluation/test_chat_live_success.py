import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def success(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("verify_chat_live_success")


def _record(kind, ident, **data):
    return {"ref": {"source_kind": kind, "source_id": ident}, "data": data}


def test_ordinary_accept_requires_exact_requested_amount_and_actual_policy_receipt(success):
    option = {
        "id": "refund",
        "action": "REFUND",
        "lines": [{"line_id": "L01", "amount_cents": 4900}],
        "policy_ids": ["POL01"],
        "evidence_ids": ["L01", "POL01:1.0"],
    }
    snapshot = {
        "candidate": {"options": [option], "unresolved": []},
        "checks": {"refund": {"status": "PASSED"}},
    }
    trace = {
        "receipts": [
            {
                "role": "strong_single",
                "records": [
                    _record("ORDER_LINE", "L01", paid_cents=4900, refunded_cents=0),
                ],
            }
        ]
    }
    assert not success._ordinary_ready(trace, snapshot)
    trace["receipts"][0]["records"].append(
        _record("POLICY", "POL01:1.0", id="POL01", version="1.0")
    )
    assert success._ordinary_ready(trace, snapshot)
    option["lines"][0]["amount_cents"] = 4899
    assert not success._ordinary_ready(trace, snapshot)


def test_complex_scope_distinguishes_customer_conflict_from_business_mapping(success):
    claim = {
        "id": "fact/package",
        "subject": "L02",
        "predicate": "package",
        "value": "TEST-PARCEL-B17",
        "state": "CONTRADICTED",
        "conflicting_refs": ["L02"],
    }
    trace = {
        "inputs": [
            {"role": "fact", "phase_id": "fact", "upstream_claim_ids": []},
            {"role": "policy", "phase_id": "policy", "upstream_claim_ids": []},
            {"role": "verifier", "phase_id": "verify-0", "upstream_claim_ids": ["fact/package"]},
        ],
        "receipts": [
            {
                "role": "fact",
                "records": [
                    _record("ORDER_LINE", "L01", package_id="TEST-PARCEL-A17"),
                    _record("ORDER_LINE", "L02", package_id="TEST-PARCEL-B17"),
                ],
            },
            {"role": "policy", "records": [_record("POLICY", "POL01:1.0")]},
        ],
        "claims": [{"role": "fact", "phase_id": "fact", "claims": [claim]}],
        "assessments": [
            {
                "recheck_count": 0,
                "assessments": [
                    {
                        "claim_id": "fact/package",
                        "verdict": "CONTRADICTED",
                        "evidence_refs": ["L02"],
                    },
                ],
            }
        ],
        "rechecks": [],
    }
    result = success._complex_outcome(trace, customer_statement_bound=True)
    assert result["native_package_mapping_read"] and result["independent_fact_policy_and_verifier"]
    assert not result["model_identified_package_conflict"]
    claim["value"] = "TEST-PARCEL-A17"
    assert not success._complex_outcome(trace, customer_statement_bound=False)[
        "model_identified_package_conflict"
    ]
    result = success._complex_outcome(trace, customer_statement_bound=True)
    assert result["model_identified_package_conflict"]
    assert result["targeted_recheck"] == "NOT_OBSERVED"


def test_ordinary_attempts_use_disjoint_targets_and_keep_business_scope(success):
    prior = success.driver.STATE
    with success._target("ordinary") as first:
        assert success.driver.PORT == 5204
        first_subnets = set(success.driver.SUBNETS.values())
    with success._target("ordinary", 2) as second:
        assert second != first and second.name.endswith("ordinary-r2.json")
        assert success.offline_demo.STATE == success.driver.STATE == second
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5206, 58368)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.96/28", "10.253.252.112/28"}
        second_subnets = set(success.driver.SUBNETS.values())
        assert first_subnets.isdisjoint(second_subnets)
    with success._target("ordinary", 3) as third:
        assert third not in {first, second} and third.name.endswith("ordinary-r3.json")
        assert success.offline_demo.STATE == success.driver.STATE == third
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5207, 58369)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.128/28", "10.253.252.144/28"}
        assert (first_subnets | second_subnets).isdisjoint(success.driver.SUBNETS.values())
        assert success.SCENARIOS["ordinary"]["order"] == "O9902"
        assert success.SCENARIOS["ordinary"]["lines"] == ["L01"]
    with success._target("complex"):
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5205, 58367)
    assert success.driver.STATE == prior
    with (
        pytest.raises(success.VerificationError, match="LIVE_SUCCESS_ATTEMPT_UNSUPPORTED"),
        success._target("complex", 3),
    ):
        pytest.fail("complex retry should not be admitted")
