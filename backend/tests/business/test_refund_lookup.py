import pytest

from aftercare.business.refund_lookup import (
    canonical_execution_id,
    locate_refund,
    resolve_refund_text,
)

EXECUTION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_ID = "11111111-2222-3333-4444-555555555555"


def record(status="SUCCEEDED", receipt=None):
    return {
        "id": EXECUTION_ID,
        "action": "REFUND",
        "status": status,
        "amount_cents": 300,
        "created_at": "2026-11-10T10:00:00+00:00",
        "receipt": receipt,
        "business_key": "redact",
    }


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("查询订单 O001 的退款", "REFERENCE_REQUIRED"),
        (EXECUTION_ID, "REFERENCE_REQUIRED"),
        (f"这是工单编号：{EXECUTION_ID}，请帮我查看", "REFERENCE_REQUIRED"),
        (f"退款执行编号：{EXECUTION_ID.upper()}现在怎样", "FOUND"),
        (f"退款执行编号：{EXECUTION_ID}-suffix", "INVALID_REFERENCE"),
        ("退款执行编号：不是完整编号", "INVALID_REFERENCE"),
        (f"退款执行编号：{EXECUTION_ID}、{OTHER_ID}", "AMBIGUOUS_REFERENCE"),
        (f"退款执行编号：{EXECUTION_ID}；工单编号：{OTHER_ID}", "FOUND"),
        (f"退款执行编号：{OTHER_ID}", "NOT_FOUND"),
        (f"退款执行编号：{EXECUTION_ID}；退款执行记录编号：{OTHER_ID}", "AMBIGUOUS_REFERENCE"),
        (f"退款执行编号：{EXECUTION_ID}，退款执行编号：{EXECUTION_ID}", "FOUND"),
    ],
)
def test_only_labelled_refund_identifiers_select_records(message, expected):
    lookup = resolve_refund_text([record()], message)
    assert lookup["status"] == expected
    if expected != "FOUND":
        assert lookup["execution"] is None
    else:
        assert lookup["execution"]["id"] == EXECUTION_ID


@pytest.mark.parametrize("separator", [" ", "\n", "\t", "以及", " 以及 "])
def test_contiguous_number_list_requests_clarification(separator):
    lookup = resolve_refund_text([record()], f"退款执行编号：{EXECUTION_ID}{separator}{OTHER_ID}")
    assert lookup == {"status": "AMBIGUOUS_REFERENCE", "execution": None}


def test_list_does_not_scan_past_unrelated_order_label():
    lookup = resolve_refund_text([record()], f"退款执行编号：{EXECUTION_ID}\n订单号：{OTHER_ID}")
    assert lookup["status"] == "FOUND"
    assert lookup["execution"]["id"] == EXECUTION_ID


def test_conflicting_description_and_run_message_are_ambiguous():
    assert resolve_refund_text(
        [record()], f"退款执行编号：{EXECUTION_ID}", f"退款执行编号：{OTHER_ID}"
    ) == {"status": "AMBIGUOUS_REFERENCE", "execution": None}
    assert canonical_execution_id(f" {EXECUTION_ID.upper()} ") == EXECUTION_ID
    assert canonical_execution_id("{" + EXECUTION_ID + "}") is None


@pytest.mark.parametrize(
    ("status", "receipt", "confirmed"),
    [
        ("SUCCEEDED", {"id": "synthetic-local"}, True),
        ("SUCCEEDED", None, False),
        ("FAILED", {"id": "simulated-error"}, True),
        ("FAILED", {"id": ""}, False),
        ("UNKNOWN", {"id": "unconfirmed"}, False),
        ("SUBMITTING", {"id": "unconfirmed"}, False),
    ],
)
def test_result_is_a_safe_projection_of_the_local_record(status, receipt, confirmed):
    assert locate_refund([record(status, receipt)], EXECUTION_ID) == {
        "status": "FOUND",
        "execution": {
            "id": EXECUTION_ID,
            "status": status,
            "amount_cents": 300,
            "created_at": "2026-11-10T10:00:00+00:00",
            "receipt_confirmed": confirmed,
        },
    }
