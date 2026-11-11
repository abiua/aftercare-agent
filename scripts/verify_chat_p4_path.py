"""Verify the isolated P4 chat-to-investigation path without provider calls."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import offline_demo
import verify_chat_p3_offline as p3
from aftercare.business.rules import digest
from verify_chat_p1_http import call, message, png, read, request_ids
from verify_chat_p2_offline import _begin, _wait, owned_container
from verify_fresh_bootstrap import ROOT, VerificationError, require
from verify_streaming_compatibility import _private_json, _save

STATE = ROOT / ".runtime" / "offline-browser-demo-p4.json"
PORT, OBJECT_PORT = 5195, 58357
BACKEND = "aftercare-backend:chat-p4-offline-20261111"
FRONTEND = "aftercare-frontend:chat-p4-offline-20261111"
PROTOCOLS = {
    "original": SimpleNamespace(
        state=STATE,
        port=PORT,
        object_port=OBJECT_PORT,
        backend=BACKEND,
        frontend=FRONTEND,
    ),
    "compact-v2": SimpleNamespace(
        state=ROOT / ".runtime" / "offline-browser-demo-p4-compact-v2.json",
        port=5196,
        object_port=58358,
        backend="aftercare-backend:chat-p4-compact-20261111",
        frontend="aftercare-frontend:chat-p4-compact-20261111",
    ),
}
READY_CHECKS = [
    "ORIGINAL_CHAT_ATTACHMENT_PROVENANCE",
    "AUTO_PROGRAM_NO_MODEL_ATTEMPT",
    "SEPARATE_STAFF_MATERIAL_REVIEW",
    "MANUAL_P4_REINVESTIGATION",
    "NO_EXECUTABLE_OFFLINE_OFFER_OR_EXECUTION",
]
TERMINAL = {"WAITING_INPUT", "NEEDS_REVIEW", "COMPLETED", "FAILED", "CANCELED"}
_preflight = p3._preflight


class P4Stack(p3.P3Stack):
    def __init__(self, directory):
        super().__init__(directory)
        target = self.runtime / "local.json"
        config = _private_json(target)
        config["app"]["chat_investigation_enabled"] = True
        target.write_text(json.dumps(config))


def plan(images):
    return {
        **_preflight(),
        "scope": "P4_OFFLINE_REAL_HTTP_PG_S3_CELERY_CHAT_TO_INVESTIGATION",
        "images": images,
        "chat_investigation_enabled": True,
        "scenario_count": 1,
        "maximum_simulated_executions": 0,
        "checks": READY_CHECKS,
        "true_model_acceptance": "NOT_RUN_SEPARATE_BOUNDED_PROTOCOL_REQUIRED",
    }


def _bridge(customer, support, report, conversation_id, case_id, attachment, content, kind):
    base = f"conversations/{conversation_id}"
    path = f"{base}/decision-materials"
    page = read(support, path)
    require(page["case_id"] == case_id, "P4_BRIDGE_CASE_MISMATCH")
    require(
        not any(
            item["material_id"]
            for item in page["items"]
            if item["attachment_id"] == attachment["id"]
        ),
        "P4_ATTACHMENT_ALREADY_LINKED",
    )
    candidate = next(
        (item for item in page["items"] if item["attachment_id"] == attachment["id"]), None
    )
    expected = hashlib.sha256(content).hexdigest()
    require(
        candidate and candidate["sha256"] == attachment["sha256"] == expected,
        "P4_CHAT_ORIGINAL_SHA_MISMATCH",
    )
    preview = call(support, "GET", f"{base}/attachments/{attachment['id']}/content", 200)
    require(preview.content == content, "P4_CHAT_ORIGINAL_CONTENT_MISMATCH")
    body = {
        "attachment_id": attachment["id"],
        "kind": kind,
        "expected_issue_id": page["issue_id"],
        "expected_case_id": case_id,
        "expected_materials_version": page["materials_version"],
    }
    call(customer, "GET", path, 404)
    p3._json(customer, path, 404, p3._key(report, "p4_customer_link_denied_" + kind), body)
    key = p3._key(report, "p4_link_" + kind)
    linked = p3._json(support, path, 200, key, body)
    require(p3._json(support, path, 200, key, body) == linked, "P4_BRIDGE_REPLAY_MISMATCH")
    material = linked["material"]
    require(
        linked["materials_version"] == page["materials_version"] + 1
        and linked["source"]
        == {
            "attachment_id": attachment["id"],
            "message_id": candidate["message_id"],
            "issue_id": page["issue_id"],
            "sha256": expected,
        }
        and material["kind"] == kind
        and material["sha256"] == expected,
        "P4_BRIDGE_PROVENANCE_MISMATCH",
    )
    material_content = call(
        support, "GET", f"cases/{case_id}/materials/{material['id']}/content", 200
    )
    require(material_content.content == content, "P4_BUSINESS_MATERIAL_BYTES_MISMATCH")
    return {**linked["source"], "material_id": material["id"], "kind": kind}


def _events(support, case_id, run_id):
    return read(support, f"cases/{case_id}/runs/{run_id}/events?after=0")


def _route(events):
    return next((row["payload"] for row in events if row["kind"] == "investigation_route"), {})


def _scenario(customer, support, order, report):
    result = report["scenario"] = {"stage": "conversation_and_originals", "order_id": order["id"]}
    conversation_id, case_id, _ = _begin(
        customer,
        report,
        "p4_conversation",
        order,
        f"订单 {order['id']} 的商品有破损，请帮忙核对。",
    )
    result.update(conversation_id=conversation_id, case_id=case_id)
    base = f"conversations/{conversation_id}"
    decisions = base + "/decisions"
    photos = [
        ("synthetic-item.png", png((182, 75, 70))),
        ("synthetic-parcel.png", png((63, 98, 144))),
    ]
    posted = message(
        customer,
        conversation_id,
        "这是商品和包裹的两张合成验收照片。",
        request_ids(report, "p4_photos"),
        photos,
    )
    originals = [
        item
        for row in posted["messages"]
        if row["role"] == "customer"
        for item in row["attachments"]
    ]
    require(len(originals) == len(photos), "P4_CHAT_ATTACHMENT_COUNT_MISMATCH")
    result["stage"] = "automatic_offline_investigation"
    _wait(
        customer,
        base,
        lambda detail: (
            (detail.get("investigation_progress") or {}).get("state") == "PENDING_SUPPORT"
        ),
        seconds=90,
    )
    original = read(support, decisions)["snapshot"]
    require(original and original["origin"] == "AGENT", "P4_AUTO_SNAPSHOT_MISSING")
    auto_run = result["auto_run_id"] = original["original_run_id"]
    auto_route = _route(_events(support, case_id, auto_run))
    require(
        auto_route.get("selected_path") == "PROGRAM"
        and all(
            option["action"] not in {"REFUND", "REPLACE"}
            for option in original["candidate"]["options"]
        ),
        "P4_AUTO_PROGRAM_GATE_MISSING",
    )
    result.update(auto_selected_path="PROGRAM", auto_snapshot_id=original["id"])
    require(
        read(customer, decisions + "/offer")["offer"] is None, "P4_OFFLINE_PUBLIC_OFFER_UNEXPECTED"
    )

    result["stage"] = "staff_original_association"
    result["materials"] = [
        _bridge(customer, support, report, conversation_id, case_id, original_photo, content, kind)
        for original_photo, (_, content), kind in zip(
            originals, photos, ("ITEM", "PARCEL"), strict=True
        )
    ]
    result["stage"] = "separate_material_review"
    before = p3._case(support, case_id)
    require(before["eligibility"]["status"] == "HUMAN_REVIEW", "P4_BRIDGE_ALREADY_APPROVED")
    require(not before["reviews"], "P4_BRIDGE_REVIEW_EXISTS")
    review = p3._json(
        support,
        f"cases/{case_id}/material-reviews",
        201,
        p3._key(report, "p4_review_materials"),
        {
            "expected_version": before["case"]["version"],
            "decision": "APPROVED",
            "notes": "隔离验收：人工确认两张合成材料，继续验证调查入口。",
        },
    )
    result["material_review_id"] = review["id"]
    require(
        p3._case(support, case_id)["eligibility"]["status"] == "ELIGIBLE",
        "P4_MATERIAL_REVIEW_NOT_EFFECTIVE",
    )

    result["stage"] = "staff_offline_reinvestigation"
    initiated = p3._json(
        support, decisions + "/run", 202, p3._key(report, "p4_staff_reinvestigate"), {}
    )
    run_id = result["staff_run_id"] = initiated["run_id"]
    require(run_id != auto_run, "P4_STAFF_RUN_REUSED_OLD_CONTEXT")
    settled = _wait(
        support, f"cases/{case_id}/runs/{run_id}", lambda run: run["status"] in TERMINAL, seconds=90
    )
    route = _route(_events(support, case_id, run_id))
    require(
        route.get("selected_path") in {"PROGRAM", "SINGLE", "STAGED", "MULTI"}
        and (settled.get("output") or {}).get("graph_profile") == "chat-investigation-v1"
        and settled["status"] in {"WAITING_INPUT", "NEEDS_REVIEW"}
        and not (settled.get("output") or {}).get("action"),
        "P4_STAFF_OFFLINE_GATE_MISSING",
    )
    result.update(
        staff_selected_path=route["selected_path"],
        staff_run_status=settled["status"],
        staff_run_reason=settled["reason"],
    )
    require(
        read(customer, decisions + "/offer")["offer"] is None, "P4_OFFLINE_PUBLIC_OFFER_UNEXPECTED"
    )
    require(
        not read(support, f"cases/{case_id}/executions")["items"], "P4_OFFLINE_EXECUTION_UNEXPECTED"
    )
    result["finance"] = "NOT_RUN_NO_EXECUTABLE_OFFLINE_OFFER"
    result["stage"] = "verified"


def _audit(stack, api, report):
    result = report["scenario"]
    return stack.json_exec(
        "P4_AUDIT_READONLY",
        api,
        f"""
import json
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from aftercare.business.models import Execution
from aftercare.config import load_settings
from aftercare.conversations.tables import ChatMaterialSource, InvestigationRequest
from aftercare.db import guarded_engine
from aftercare.intelligence.tables import ModelAttempt
from aftercare.conversation_analysis.tables import ChatModelAttempt
engine = guarded_engine(load_settings("app"))
with Session(engine) as db, db.begin():
    db.execute(text("SET TRANSACTION READ ONLY"))
    conversation_id = {result["conversation_id"]!r}
    request = db.scalar(select(InvestigationRequest).where(
        InvestigationRequest.conversation_id == conversation_id,
        InvestigationRequest.run_id == {result["auto_run_id"]!r},
    ))
    sources = db.scalars(select(ChatMaterialSource).where(
        ChatMaterialSource.conversation_id == conversation_id
    ).order_by(ChatMaterialSource.kind)).all()
    print(json.dumps({{
        "automatic_request_state": request.state if request else None,
        "automatic_turn_id": request.turn_id if request else None,
        "automatic_request_run_id": request.run_id if request else None,
        "sources": [
            {{name: getattr(source, name) for name in (
                "attachment_id", "message_id", "issue_id", "sha256", "material_id", "kind"
            )}} for source in sources
        ],
        "agent_model_attempts": len(db.scalars(select(ModelAttempt.run_id)).all()),
        "chat_model_attempts": len(db.scalars(select(ChatModelAttempt.turn_id)).all()),
        "intake_model_attempts": db.scalar(text("SELECT count(*) FROM intake_model_audits")),
        "execution_count": len(db.scalars(select(Execution.id)).all()),
    }}))
engine.dispose()
""",
    )


def _execute(report, context=PROTOCOLS["original"]):
    with patch.multiple(offline_demo, STATE=context.state, FreshStack=P4Stack):
        offline_demo.start(
            SimpleNamespace(
                port=context.port,
                object_port=context.object_port,
                backend_image=report["plan"]["images"][context.backend],
                frontend_image=report["plan"]["images"][context.frontend],
            )
        )
        state = _private_json(context.state)
        stack = offline_demo.restore(state)
    require(state["url"] == f"http://127.0.0.1:{context.port}", "P4_LOOPBACK_REQUIRED")
    report["isolated_url"] = state["url"]
    config = _private_json(stack.runtime / "local.json")["app"]
    require(
        all(
            config[key]
            for key in (
                "chat_analysis_enabled",
                "chat_decisions_enabled",
                "chat_investigation_enabled",
            )
        )
        and not any(
            config[key] for key in ("chat_vision_enabled", "model_enabled", "embedding_enabled")
        ),
        "P4_OFFLINE_SETTINGS_REQUIRED",
    )
    credentials = {item["role"]: item for item in _private_json(stack.credentials_path)}
    with ExitStack() as resources:
        clients = {}
        for role in ("customer", "support"):
            item = credentials[role]
            client = resources.enter_context(
                httpx.Client(
                    base_url=f"http://127.0.0.1:{context.port}/api/",
                    trust_env=False,
                    timeout=10,
                    headers={"Authorization": f"Bearer {item['token']}"},
                )
            )
            who = read(client, "me")
            require(
                all(who.get(key) == item.get(key) for key in ("role", "user_id", "customer_id")),
                "P4_ACTOR_MISMATCH",
            )
            clients[role] = client
        customer, support = clients["customer"], clients["support"]
        require(read(support, "runtime")["mode"] == "offline", "P4_OFFLINE_RUNTIME_REQUIRED")
        eligible = [
            row
            for row in read(customer, "orders")["items"]
            if row["status"] == "DELIVERED"
            and row["paid_at"]
            and row["delivered_at"]
            and len(row["items"]) == 1
            and row["paid_cents"] > 0
            and row["refunded_cents"] == 0
        ]
        require(eligible, "P4_ELIGIBLE_SYNTHETIC_ORDER_REQUIRED")
        _scenario(customer, support, eligible[0], report)
    audit = _audit(stack, owned_container(stack, "api"), report)
    report["database_audit"] = audit
    require(
        audit["automatic_request_state"] == "STARTED"
        and audit["automatic_request_run_id"] == report["scenario"]["auto_run_id"]
        and audit["sources"] == report["scenario"]["materials"]
        and all(
            audit[key] == 0
            for key in (
                "agent_model_attempts",
                "chat_model_attempts",
                "intake_model_attempts",
                "execution_count",
            )
        ),
        "P4_DURABLE_AUDIT_MISMATCH",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=PROTOCOLS, default="original")
    parser.add_argument("--backend-image-id")
    parser.add_argument("--frontend-image-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    context = PROTOCOLS[args.protocol]
    ids = (args.backend_image_id, args.frontend_image_id)
    if any(ids) and not all(value and re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in ids):
        parser.error("supply both full immutable sha256 image IDs")
    images = dict(zip((context.backend, context.frontend), ids, strict=True)) if all(ids) else {}
    if args.execute and (not images or not args.expected_plan_sha256 or args.report is None):
        parser.error(
            "--execute requires pinned images, the exact preflight digest and a new report path"
        )
    with patch.multiple(
        p3,
        STATE=context.state,
        PORT=context.port,
        OBJECT_PORT=context.object_port,
        IMAGES={tag: value[7:] for tag, value in images.items()},
    ):
        frozen = plan(images)
    if args.protocol == "compact-v2":
        frozen["protocol"] = "compact-v2"
    if not images:
        print(json.dumps({"status": "UNPINNED_PREFLIGHT_ONLY", "plan": frozen}, indent=2))
        return 0
    checksum = digest(frozen)
    if not args.execute:
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": frozen}, indent=2
            )
        )
        return 0
    require(checksum == args.expected_plan_sha256, "P4_PLAN_CHANGED")
    report = {"status": "IN_PROGRESS", "plan_sha256": checksum, "plan": frozen, "request_ids": {}}
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with offline_demo.lifecycle_lock(context.state):
            _execute(report, context)
        report["status"] = "PASSED"
    except VerificationError as error:
        report.update(status="FAILED", failure_code=str(error))
    except httpx.TimeoutException:
        report.update(status="FAILED", failure_code="HTTP_TIMEOUT_OUTCOME_UNKNOWN")
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.TimeoutExpired):
        report.update(status="FAILED", failure_code="P4_ISOLATED_EXECUTION_FAILED")
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, OSError, ValueError, subprocess.TimeoutExpired):
        print("P4_PATH_ACCEPTANCE_BLOCKED")
        raise SystemExit(1) from None
