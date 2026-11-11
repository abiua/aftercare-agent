"""Prepare one new test-profile refund case for a later browser-only financial flow."""

import argparse
import hashlib
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from aftercare.config import ROOT
from PIL import Image, ImageDraw
from verify_container_live import page
from verify_evaluation_http import CheckFailed, client_for, request, require
from verify_response_clarity_http import reserve, save

BASE = "http://127.0.0.1:8022"
STATE = ROOT / ".runtime/browser-financial-case-state.json"
PHOTO_KINDS = ("ITEM", "PARCEL")
MATERIAL_NOTE = "隔离模拟验收。两张图片均为合成占位，模拟材料审核通过，财务审批与模拟执行另行验收。"


def timestamp():
    return datetime.now(UTC).isoformat()


def sha(content):
    return hashlib.sha256(content).hexdigest()


def synthetic_photos():
    images = {}
    for index, kind in enumerate(PHOTO_KINDS):
        color = ("#e34a58", "#376bba")[index]
        with Image.new("RGB", (96, 64), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((8, 8, 87, 55), fill=color)
            draw.text((20, 24), kind, fill="white")
            out = io.BytesIO()
            image.save(out, format="PNG")
            images[kind] = out.getvalue()
    return images


def suitable_orders(orders, used, specified):
    return [
        order
        for order in orders
        if order["id"] not in used
        and (specified is None or order["id"] == specified)
        and order["status"] == "DELIVERED"
        and order["payment_status"] == "CONFIRMED"
        and order.get("paid_at")
        and order.get("delivered_at")
        and order["refunded_cents"] == 0
        and len(order["items"]) == 1
        and 0 < order["paid_cents"] <= 50000
        and order["items"][0]["paid_cents"] == order["paid_cents"]
        and order["items"][0]["refunded_cents"] == 0
    ]


def preflight(clients, credentials):
    for role, client in clients.items():
        me = request(client, "/api/me")
        require(
            me["role"] == role and me["user_id"] == credentials[role]["user_id"],
            "TEST_ROLE_IDENTITY_MISMATCH",
        )
    require(request(clients["support"], "/api/readyz")["status"] == "ready", "TEST_API_NOT_READY")
    capabilities = request(clients["tester"], "/api/evaluation/capabilities")
    require(capabilities.get("eval_gates") is True, "TEST_ONLY_EVALUATION_NOT_ENABLED")
    return request(clients["support"], "/api/runtime")["mode"]


def order_for(support, specified):
    used = {case["order_id"] for case in page(support, "cases")}
    matches = suitable_orders(page(support, "orders"), used, specified)
    require(matches, "NO_UNUSED_SINGLE_LINE_PAID_TEST_ORDER")
    return matches[0]


def order_summary(order):
    line = order["items"][0]
    return {
        "id": order["id"],
        "version": order["version"],
        "status": order["status"],
        "payment_status": order["payment_status"],
        "paid_cents": order["paid_cents"],
        "refunded_cents": order["refunded_cents"],
        "line_id": line["line_id"],
        "suggested_refund_cents": line["paid_cents"] - line["refunded_cents"],
    }


def find_fixture(support, state):
    if "case_id" in state:
        return str(UUID(state["case_id"]))
    if "order_id" not in state:
        return None
    candidates = [
        case
        for case in page(support, "cases")
        if case["order_id"] == state["order_id"]
        and case["intent"] == "damaged"
        and case["description"] == state["description"]
    ]
    require(len(candidates) <= 1, "MULTIPLE_MATCHING_FIXTURE_CASES")
    return str(UUID(candidates[0]["id"])) if candidates else None


def inspect_case(support, case_id, state):
    path = f"/api/cases/{case_id}"
    detail = request(support, path)
    case = detail["case"]
    require(
        case["description"] == state["description"]
        and case["order_id"] == state["order_id"]
        and case["intent"] == "damaged"
        and case["line_ids"] == [state["line_id"]],
        "CASE_IS_NOT_THIS_SYNTHETIC_FIXTURE",
    )
    return detail, request(support, path + "/runs")


def readback(support, state):
    case_id = find_fixture(support, state)
    if case_id is None:
        return {"case_id": None, "phase": state.get("phase"), "status": "FIXTURE_NOT_OBSERVED"}
    detail, runs = inspect_case(support, case_id, state)
    return {
        "case_id": case_id,
        "order": order_summary(detail["order"]),
        "case_version": detail["case"]["version"],
        "eligibility": detail["eligibility"],
        "materials": [
            {k: row[k] for k in ("id", "kind", "sha256", "size")} for row in detail["materials"]
        ],
        "reviews": [
            {k: row[k] for k in ("id", "decision", "effective", "materials_version")}
            for row in detail["reviews"]
        ],
        "counts": {key: len(detail[key]) for key in ("proposals", "approvals", "ledger")}
        | {"agent_runs": len(runs)},
    }


def prepare(support, order, images, state, report, persist):
    report["order"] = order_summary(order)
    state.update(
        order_id=order["id"],
        line_id=report["order"]["line_id"],
        description=f"浏览器财务验收 {state['fixture_id']}。模拟货损，仅用于测试环境。",
        photos={kind: {"sha256": sha(data), "bytes": len(data)} for kind, data in images.items()},
    )
    state["create_body"] = {
        "order_id": order["id"],
        "intent": "damaged",
        "description": state["description"],
        "expected_version": order["version"],
        "line_ids": [state["line_id"]],
    }
    state["phase"] = "CREATE_POST_PENDING"
    persist()
    case = request(
        support, "/api/cases", expected=201, body=state["create_body"], key=state["keys"]["case"]
    )
    require(
        case["description"] == state["description"] and case["version"] == 1,
        "CREATE_DID_NOT_RETURN_NEW_CASE",
    )
    case_id = str(UUID(case["id"]))
    state.update(case_id=case_id, phase="CASE_CREATED")
    report["case_id"] = case_id
    persist()
    detail, runs = inspect_case(support, case_id, state)
    require(
        not any(
            detail[field] for field in ("materials", "reviews", "proposals", "approvals", "ledger")
        )
        and not runs
        and set(detail["eligibility"]["missing_materials"]) == set(PHOTO_KINDS)
        and detail["order"] == order,
        "NEW_CASE_FIXTURE_PRECONDITIONS_CHANGED",
    )
    for kind in PHOTO_KINDS:
        image = images[kind]
        version = detail["case"]["version"]
        state.update(phase=f"{kind}_POST_PENDING", expected_version=version)
        persist()
        response = support.post(
            f"/api/cases/{case_id}/materials",
            headers={"Idempotency-Key": state["keys"][kind]},
            data={"kind": kind, "expected_version": str(version)},
            files={"file": (f"synthetic-{kind.lower()}.png", image, "image/png")},
        )
        require(response.status_code == 201, f"UPLOAD_{kind}_HTTP_{response.status_code}")
        photo = response.json()
        require(
            photo["kind"] == kind
            and photo["sha256"] == sha(image)
            and photo["size"] == len(image)
            and photo["content_type"] == "image/png",
            "SYNTHETIC_MATERIAL_METADATA_MISMATCH",
        )
        material_id = str(UUID(photo["id"]))
        content = support.get(f"/api/cases/{case_id}/materials/{material_id}/content")
        require(
            content.status_code == 200 and content.content == image,
            "MATERIAL_CONTENT_READBACK_MISMATCH",
        )
        state["photos"][kind]["id"] = material_id
        report["materials"] = state["photos"]
        state["phase"] = f"{kind}_VERIFIED"
        persist()
        detail, runs = inspect_case(support, case_id, state)
        require(not runs, "UNEXPECTED_AGENT_ACTIVITY")

    state["review_body"] = {
        "expected_version": detail["case"]["version"],
        "decision": "APPROVED",
        "notes": MATERIAL_NOTE,
    }
    state["phase"] = "REVIEW_POST_PENDING"
    persist()
    review = request(
        support,
        f"/api/cases/{case_id}/material-reviews",
        expected=201,
        body=state["review_body"],
        key=state["keys"]["review"],
    )
    state.update(phase="REVIEW_CREATED", review_id=str(UUID(review["id"])))
    persist()
    final, runs = inspect_case(support, case_id, state)
    eligibility = final["eligibility"]
    reports = readback(support, state)
    report["final_readback"] = reports
    report["order_unchanged"] = final["order"] == order
    require(
        eligibility["status"] == "ELIGIBLE"
        and "REFUND" in eligibility["actions"]
        and eligibility["missing_materials"] == []
        and eligibility["available_cents"] == report["order"]["suggested_refund_cents"]
        and len(reports["reviews"]) == 1
        and reports["reviews"][0]["id"] == state["review_id"]
        and reports["reviews"][0]["decision"] == "APPROVED"
        and reports["reviews"][0]["effective"] is True
        and len(reports["materials"]) == 2
        and all(n == 0 for n in reports["counts"].values())
        and not runs
        and report["order_unchanged"],
        "FINANCIAL_CASE_NOT_READY_OR_UNEXPECTED_SIDE_EFFECT",
    )
    state["phase"] = "READY_FOR_BROWSER"
    report.update(
        status="READY_FOR_BROWSER",
        case_version=final["case"]["version"],
        material_review_id=state["review_id"],
        model_runs_created=0,
        next_step="browser_support_proposal_then_approver_decision_then_support_simulated_execution",
    )
    persist()


def fixture_check():
    photos = synthetic_photos()
    assert set(photos) == set(PHOTO_KINDS)
    assert len({sha(content) for content in photos.values()}) == 2
    for data in photos.values():
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "PNG"
            image.verify()
    good = {
        "id": "unused",
        "version": 1,
        "status": "DELIVERED",
        "payment_status": "CONFIRMED",
        "paid_at": "fixture",
        "delivered_at": "fixture",
        "paid_cents": 35000,
        "refunded_cents": 0,
        "items": [{"line_id": "L01", "paid_cents": 35000, "refunded_cents": 0}],
    }
    others = [
        {**good, "id": "used"},
        {**good, "id": "refunded", "refunded_cents": 1},
        {**good, "id": "multiple", "items": [*good["items"], *good["items"]]},
    ]
    assert suitable_orders([*others, good], {"used"}, None) == [good]
    assert order_summary(good)["suggested_refund_cents"] == 35000
    print("FIXTURE_PASSED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--readback", action="store_true")
    mode.add_argument("--fixture-check", action="store_true")
    parser.add_argument("--order-id", help="Require a specific unused test order")
    parser.add_argument("--output", type=Path, help="New private preparation report")
    args = parser.parse_args()
    if args.execute != (args.output is not None):
        parser.error("--execute requires a new --output")
    if args.output is not None and args.output.resolve() == STATE.resolve():
        parser.error("--output must differ from the private state file")
    if args.readback and args.order_id:
        parser.error("--readback uses only the original state")
    if args.fixture_check:
        fixture_check()
        return 0

    state = {
        "fixture_id": uuid4().hex,
        "keys": {step: str(uuid4()) for step in ("case", *PHOTO_KINDS, "review")},
    }
    report = {
        "status": "NOT_RUN",
        "scope": "real_test_http_synthetic_material_preparation",
        "created_at": timestamp(),
        "no_financial_actions_requested": True,
    }
    reserved = False

    def persist():
        if reserved:
            save(STATE, state)
            save(args.output, report)

    try:
        if args.execute:
            require(not os.path.lexists(STATE), "PREPARATION_STATE_EXISTS_USE_READBACK")
            require(not os.path.lexists(args.output), "PREPARATION_REPORT_ALREADY_EXISTS")
        elif args.readback:
            state = json.loads(STATE.read_text())
        roles = {"support", "approver", "tester"}
        credentials = {
            row["role"]: row
            for row in json.loads((ROOT / ".runtime/users-test.json").read_text())
            if row["role"] in roles
        }
        with (
            client_for(BASE, credentials["support"]["token"]) as support,
            client_for(BASE, credentials["approver"]["token"]) as approver,
            client_for(BASE, credentials["tester"]["token"]) as tester,
        ):
            report["runtime_mode"] = preflight(
                {"support": support, "approver": approver, "tester": tester}, credentials
            )
            if args.readback:
                print(json.dumps(readback(support, state), ensure_ascii=False))
                return 0
            order = order_for(support, args.order_id)
            if args.execute:
                reserve(args.output)
                reserve(STATE)
                reserved = True
                report["status"] = "IN_PROGRESS"
                try:
                    prepare(support, order, synthetic_photos(), state, report, persist)
                except Exception:  # Preserve uncertain writes by readback only.
                    if "order_id" in state:
                        try:
                            report["failure_readback"] = readback(support, state)
                        except Exception:  # noqa: BLE001 - leave previous safe evidence intact
                            report["failure_readback"] = "UNAVAILABLE"
                    raise
            else:
                report.update(status="PREFLIGHT_PASSED", order=order_summary(order))
    except Exception as error:  # noqa: BLE001 - redact HTTP, identity and I/O exception details
        report.update(
            status="UNKNOWN" if state.get("phase", "").endswith("POST_PENDING") else "FAILED",
            failure=str(error) if isinstance(error, CheckFailed) else type(error).__name__,
        )
    finally:
        persist()
    print(
        json.dumps(
            {
                "status": report["status"],
                "order": report.get("order"),
                "case_id": report.get("case_id"),
                "failure": report.get("failure"),
            },
            ensure_ascii=False,
        )
    )
    return int(report["status"] not in {"PREFLIGHT_PASSED", "READY_FOR_BROWSER"})


if __name__ == "__main__":
    raise SystemExit(main())
