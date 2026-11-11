import json
from datetime import UTC, datetime, timedelta

import pytest

from aftercare.business.rules import eligibility
from aftercare.business.seed import DAMAGE_REMEDY_POLICY, WRONG_ITEM_POLICY
from aftercare.config import ROOT

DELIVERED = datetime(2026, 2, 1, 12, tzinfo=UTC)


@pytest.fixture
def assess():
    baseline = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    policies = [*baseline["policies"], WRONG_ITEM_POLICY, DAMAGE_REMEDY_POLICY]

    def evaluate(
        intent, *, elapsed=timedelta(days=1), order=None, facts=None, materials=(), review=None
    ):
        return eligibility(
            case={
                "intent": intent,
                "created_at": (DELIVERED + elapsed).isoformat(),
                "line_ids": ["L1"],
            },
            order={
                "status": "DELIVERED",
                "version": 2,
                "delivered_at": DELIVERED.isoformat(),
                "paid_at": (DELIVERED - timedelta(days=1)).isoformat(),
                "paid_cents": 60000,
                "payment_status": "CONFIRMED",
                "items": [{"line_id": "L1"}],
                "products": [{"id": "P1", "returnable": True, "warranty_days": 30}],
                **(order or {}),
            },
            policies=policies,
            materials=[{"kind": kind} for kind in materials],
            review=review,
            facts=facts or {},
            balances=[
                {
                    "line_id": "L1",
                    "product_id": "P1",
                    "available_cents": 60000,
                    "replace_available_quantity": 1,
                }
            ],
        )

    return evaluate


@pytest.mark.parametrize(
    ("payment", "expected"),
    [
        ({}, "ELIGIBLE"),
        ({"payment_status": "UNKNOWN"}, "HUMAN_REVIEW"),
        ({"paid_at": None}, "HUMAN_REVIEW"),
        ({"paid_at": "2026-01-31T12:00:00"}, "HUMAN_REVIEW"),
        ({"paid_cents": 0}, "HUMAN_REVIEW"),
    ],
)
def test_pol02_payment_requires_confirmed_positive_and_dated_evidence(assess, payment, expected):
    result = assess(
        "damaged", order=payment, materials=("ITEM", "PARCEL"), review={"decision": "APPROVED"}
    )
    assert "POL02" in result["policy_ids"]
    assert result["status"] == expected
    assert result["actions"] == (["REFUND"] if expected == "ELIGIBLE" else [])


@pytest.mark.parametrize(
    ("interception", "verified_version", "expected"),
    [
        ("PENDING", 2, "HUMAN_REVIEW"),
        ("FAILED", 2, "HUMAN_REVIEW"),
        ("CONFIRMED", 1, "HUMAN_REVIEW"),
        ("CONFIRMED", 2, "ELIGIBLE"),
    ],
)
def test_pol04_interception_must_be_confirmed_for_current_order(
    assess, interception, verified_version, expected
):
    result = assess(
        "cancel",
        order={"status": "SHIPPED"},
        facts={
            "fulfillment_status": "UNSHIPPED",
            "fulfillment_order_version": verified_version,
            "interception_status": interception,
        },
    )
    assert "POL04" in result["policy_ids"] and "POL03" not in result["policy_ids"]
    assert result["status"] == expected
    assert result["actions"] == (["REFUND"] if expected == "ELIGIBLE" else [])


@pytest.mark.parametrize(
    ("elapsed", "returnable", "expected"),
    [
        (timedelta(hours=168), True, "ELIGIBLE"),
        (timedelta(hours=168, seconds=1), True, "INELIGIBLE"),
        (timedelta(hours=168), False, "INELIGIBLE"),
    ],
)
def test_pol05_return_window_has_inclusive_end_and_per_product_scope(
    assess, elapsed, returnable, expected
):
    result = assess(
        "return",
        elapsed=elapsed,
        order={"products": [{"id": "P1", "returnable": returnable}]},
        facts={
            "return_lines": {
                "L1": {"return_receipt_status": "RECEIVED", "inspection_status": "PASSED"}
            }
        },
    )
    assert "POL05" in result["policy_ids"]
    assert result["status"] == expected
    assert result["actions"] == (["REFUND"] if expected == "ELIGIBLE" else [])


@pytest.mark.parametrize(
    ("elapsed", "days", "status", "line_status"),
    [
        (timedelta(days=30), 30, "HUMAN_REVIEW", "COVERED"),
        (timedelta(days=30, microseconds=1), 30, "INELIGIBLE", "OUTSIDE_WARRANTY"),
        (timedelta(days=1), 0, "INELIGIBLE", "OUTSIDE_WARRANTY"),
        (timedelta(days=1), 10**9, "HUMAN_REVIEW", "COVERED"),
    ],
)
def test_pol11_warranty_is_inclusive_and_does_not_authorize_refund(
    assess, elapsed, days, status, line_status
):
    result = assess(
        "warranty",
        elapsed=elapsed,
        order={"products": [{"id": "P1", "returnable": False, "warranty_days": days}]},
    )
    assert result["policy_ids"] == ["POL01", "POL11"]
    assert result["status"] == status
    assert result["lines"][0]["rule_status"] == line_status
    assert result["actions"] == []


@pytest.mark.parametrize(
    "warranty",
    [
        pytest.param({}, id="missing"),
        pytest.param({"warranty_days": None}, id="null"),
        pytest.param({"warranty_days": True}, id="boolean-true"),
        pytest.param({"warranty_days": False}, id="boolean-false"),
        pytest.param({"warranty_days": -1}, id="negative"),
        pytest.param({"warranty_days": 30.0}, id="floating-point"),
        pytest.param({"warranty_days": "30"}, id="string"),
        pytest.param({"warranty_days": []}, id="array"),
    ],
)
def test_pol11_unknown_warranty_period_requires_human_review_without_refund(assess, warranty):
    result = assess(
        "warranty",
        order={"products": [{"id": "P1", "returnable": False, **warranty}]},
    )
    assert result["policy_ids"] == ["POL01", "POL11"]
    assert result["status"] == "HUMAN_REVIEW"
    assert result["reason_code"] == result["lines"][0]["rule_status"] == "WARRANTY_PERIOD_UNKNOWN"
    assert result["reason"] == "当前订单行质保期限无法确认，请人工核对。"
    assert result["actions"] == [] and result["available_cents"] == 0


@pytest.mark.parametrize(
    ("materials", "review", "status", "missing"),
    [
        (("ITEM", "PARCEL"), None, "WAITING_INPUT", ["WRONG_ITEM", "LABEL"]),
        (("WRONG_ITEM",), None, "WAITING_INPUT", ["LABEL"]),
        (("WRONG_ITEM", "LABEL"), None, "HUMAN_REVIEW", []),
        (("WRONG_ITEM", "LABEL"), {"decision": "APPROVED"}, "ELIGIBLE", []),
    ],
)
def test_pol13_wrong_item_photo_kinds_and_review_are_separate_gates(
    assess, materials, review, status, missing
):
    result = assess("wrong_item", materials=materials, review=review)
    assert "POL13" in result["policy_ids"]
    assert result["status"] == status and result["missing_materials"] == missing
    assert ("REPLACE" in result["actions"]) is (status == "ELIGIBLE")
