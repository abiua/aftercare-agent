from collections import Counter, defaultdict
from io import BytesIO

import pytest
from PIL import Image

from aftercare.business.rules import eligibility
from aftercare.business.storage import validate_image
from aftercare.evaluation.dataset import (
    DATASET_ROOT,
    SPLITS,
    build_artifacts,
    load_cases,
    load_references,
    material_bytes,
)

CASES = load_cases()
REFERENCES = load_references()
CORE = [case for case in CASES if case["split"] != "regression"]


def test_artifacts_rebuild_byte_for_byte():
    for path, content in build_artifacts().items():
        assert (DATASET_ROOT / path).read_bytes() == content, path


def test_counts_families_and_isolation():
    assert Counter(case["split"] for case in CASES) == {
        "dev": 60,
        "validation": 30,
        "holdout": 30,
        "regression": 24,
    }
    assert {case["id"] for case in CASES} == REFERENCES.keys()
    assert len({case["message"] for case in CASES}) == len(CASES)
    assert Counter(REFERENCES[case["id"]]["intent"] for case in CORE) == {
        "damaged": 40,
        "wrong_item": 40,
        "refund_status": 40,
    }
    groups = defaultdict(set)
    for case in CORE:
        groups[REFERENCES[case["id"]]["family"]].add(case["split"])
    assert len(groups) == 24 and all(len(splits) == 1 for splits in groups.values())
    assert all(ref["human_review"] == "PENDING" for ref in REFERENCES.values())
    assert all(ref["label_source"] == "SYNTHETIC_REFERENCE" for ref in REFERENCES.values())
    assert Counter(len(load_cases(split)) for split in SPLITS) == {60: 1, 30: 2, 24: 1}
    assert len({case["fixture"]["business"]["orders"][0]["id"] for case in CASES}) == len(CASES)
    assert len({case["fixture"]["business"]["customers"][0]["id"] for case in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_case_contract_and_integer_ledger(case):
    assert case.keys() == {"id", "split", "message", "fixture", "events"}
    reference = REFERENCES[case["id"]]
    assert reference["checks"]
    fixture = case["fixture"]
    orders = fixture["business"]["orders"]
    (order,) = orders
    assert order["customer_id"] == fixture["business"]["customers"][0]["id"]
    assert {row["product_id"] for row in order["items"]} <= {
        product["id"] for product in fixture["business"]["products"]
    }
    assert order["paid_cents"] == sum(item["paid_cents"] for item in order["items"])
    assert order["refunded_cents"] == sum(item["refunded_cents"] for item in order["items"])
    assert order["subtotal_cents"] - order["discount_cents"] == order["payable_cents"]
    for item in order["items"]:
        assert type(item["refunded_cents"]) is type(item["paid_cents"]) is int
        assert 0 <= item["refunded_cents"] <= item["paid_cents"]
    for history in fixture["refund_history"]:
        assert history["amount_cents"] == sum(line["amount_cents"] for line in history["lines"])
        assert (history["receipt"] is None) == (history["status"] != "SUCCEEDED")
    assert case["events"][0]["op"] == "create_case"
    ids = {e["id"] for e in case["events"]}
    assert len(ids) == len(case["events"])
    for check in reference["checks"]:
        assert check["event_id"] in ids
        assert set(check) == {"event_id", "path", "equals"}


@pytest.mark.parametrize("case", CORE, ids=lambda case: case["id"])
def test_core_script_states_match_business_eligibility(case):
    fixture = case["fixture"]
    (order,) = fixture["business"]["orders"]
    create = case["events"][0]["args"]
    case_context = {
        "intent": create["intent"],
        "created_at": fixture["as_of"],
        "line_ids": create["line_ids"],
    }
    selected = [row for row in order["items"] if row["line_id"] in create["line_ids"]]
    inventory = {row["product_id"]: row["available_quantity"] for row in fixture["inventory"]}
    balances = [
        {
            "line_id": row["line_id"],
            "product_id": row["product_id"],
            "available_cents": row["paid_cents"] - row["refunded_cents"],
            "replace_available_quantity": min(row["quantity"], inventory[row["product_id"]]),
        }
        for row in selected
    ]
    reference = REFERENCES[case["id"]]
    materials = []
    review = None
    for event in case["events"][1:]:
        op, args = event["op"], event["args"]
        if op == "upload":
            materials.append({"kind": args["kind"]})
            review = None
        elif op == "review":
            review = {"decision": args["decision"]}
        elif op in {"start", "resume"}:
            result = eligibility(
                case=case_context,
                order=order,
                policies=fixture["business"]["policies"],
                materials=materials,
                review=review,
                facts={},
                balances=balances,
            )
            expected = next(
                check["equals"]
                for check in reference["checks"]
                if check["event_id"] == event["id"] and check["path"] == "reason"
            )
            assert (
                result["status"]
                if result["reason_code"] != "MATERIAL_REVIEW_REQUIRED"
                else result["reason_code"]
            ) == expected
    assert sorted(reference["allowed_draft_actions"]) == sorted(result["actions"])


def test_assets_are_decodable_and_not_evidence_labels():
    first = material_bytes("case", "item-1")
    assert first == material_bytes("case", "item-1")
    assert first != material_bytes("case", "item-2")
    assert validate_image(first)[0] == "image/png"
    with Image.open(BytesIO(first)) as image:
        assert image.size == (8, 8)
        assert image.getpixel((0, 0)) == image.getpixel((7, 7))
    assert not material_bytes("case", "invalid-bytes").startswith(b"\x89PNG")


def test_interrupted_attempt_reference_checks_explicit_null_output():
    checks = REFERENCES["AC02-R023"]["checks"]
    assert {check["path"]: check["equals"] for check in checks} == {
        "status": "NEEDS_REVIEW",
        "reason": "INTERRUPTED_MODEL_CALL",
        "output": None,
    }
