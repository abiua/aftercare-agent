"""Stage one approved synthetic order and audit its real investigation separately."""

import argparse
import json
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import verify_chat_live_business_path as historical_flow
from tau_p4_runtime import API_PORT, RUNTIME, configure
from verify_chat_live_business_path import (
    _checkpoint,
    _complete_business,
    _mutation,
    _public_chat,
)
from verify_chat_p1_http import call, message, read, request_ids
from verify_chat_p2_offline import _wait
from verify_chat_p5_multimodal_source import _inputs
from verify_fresh_bootstrap import require


def prepare(clients, report, output):
    _, assets, originals, _ = _inputs()
    conversation, case, attachments = _public_chat(clients, report, output, assets, originals)
    support = clients["support"]
    path = f"conversations/{conversation}/decision-materials"
    materials = report["materials"] = []
    for attachment, asset, kind in zip(attachments, assets, ("ITEM", "PARCEL"), strict=True):
        page = read(support, path)
        linked, _ = _mutation(
            support,
            report,
            output,
            "LINK_" + kind,
            path,
            200,
            {
                "attachment_id": attachment["id"],
                "kind": kind,
                "expected_issue_id": report["issue_id"],
                "expected_case_id": case,
                "expected_materials_version": page["materials_version"],
            },
        )
        material = linked["material"]
        fetched = call(support, "GET", f"cases/{case}/materials/{material['id']}/content", 200)
        require(fetched.content == originals[asset["id"]], "S3_ORIGINAL_MISMATCH")
        materials.append({"material_id": material["id"], "kind": kind, **linked["source"]})
    detail = read(support, f"cases/{case}")
    review, _ = _mutation(
        support,
        report,
        output,
        "SYNTHETIC_MATERIAL_REVIEW",
        f"cases/{case}/material-reviews",
        201,
        {
            "expected_version": detail["case"]["version"],
            "decision": "APPROVED",
            "notes": "自动化合成材料验收，记录商品照片中可见的破损与对应商品行。",
            "observations": [
                {
                    "material_id": materials[0]["material_id"],
                    "finding": "VISIBLE_DAMAGE",
                    "line_id": "L01",
                    "affected_quantity": 1,
                }
            ],
        },
    )
    report["material_review_id"] = review["id"]
    _checkpoint(report, output, "awaiting_paid_dispatch")


def dispatch(report, output):
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.queue import make_queue
    from sqlalchemy import text

    require(report["stage"] == "awaiting_paid_dispatch", "PAID_DISPATCH_ALREADY_ATTEMPTED")
    engine = guarded_engine(load_settings("test"))
    try:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            row = (
                db.execute(
                    text(
                        "SELECT t.id, t.state, t.revision=c.revision AS current, "
                        "t.quiet_until<=now() AS due, b.order_id "
                        "FROM chat_analysis_turns t JOIN chat_conversations c "
                        "ON t.conversation_id=c.id JOIN business_cases b ON b.id=c.active_case_id "
                        "WHERE t.conversation_id=:conversation AND b.id=:case "
                        "ORDER BY t.generation DESC LIMIT 1"
                    ),
                    {"conversation": report["conversation_id"], "case": report["case_id"]},
                )
                .mappings()
                .one()
            )
            require(
                row["state"] == "PUBLISHED"
                and row["current"]
                and row["due"]
                and row["order_id"] == "O9902",
                "PAID_INVESTIGATION_TURN_NOT_READY",
            )
            prior_count = len(report.get("prior_budget_attempts", []))
            require(
                db.scalar(text("SELECT count(*) FROM agent_model_attempts")) == prior_count,
                "P4_LIVE_ATTEMPT_BASELINE_CHANGED",
            )
            report["paid_turn_id"] = row["id"]
    finally:
        engine.dispose()
    _checkpoint(report, output, "paid_dispatch_pending")
    result = make_queue("test").send_task(
        "aftercare.chat_investigation_start", args=[report["paid_turn_id"]]
    )
    report["paid_dispatch_task"] = result.id
    _checkpoint(report, output, "paid_dispatched")


def audit(report):
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from sqlalchemy import text

    engine = guarded_engine(load_settings("test"))
    try:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            attempts = list(
                db.execute(
                    text(
                        "SELECT a.run_id, a.logical_id, a.ordinal, a.status, a.error_code, a.usage, "
                        "a.estimated_cost_usd, r.case_id, a.metadata_json->>'model' AS model "
                        "FROM agent_model_attempts a JOIN agent_runs r ON r.id=a.run_id "
                        "ORDER BY a.started_at"
                    )
                ).mappings()
            )
            request = (
                db.execute(
                    text(
                        "SELECT run_id,state,reason FROM chat_investigation_requests WHERE turn_id=:turn"
                    ),
                    {"turn": report["paid_turn_id"]},
                )
                .mappings()
                .one_or_none()
            )
            events = (
                []
                if not request or not request["run_id"]
                else list(
                    db.execute(
                        text(
                            "SELECT kind, payload FROM agent_run_events WHERE run_id=:run "
                            "AND (kind IN ('status','investigation_route','investigation_result',"
                            "'investigation_tool','route') OR (kind='investigation_receipt' "
                            "AND payload->'receipt'->>'name'='search_applicable_policy')) ORDER BY seq"
                        ),
                        {"run": request["run_id"]},
                    ).mappings()
                )
            )
            projection = (
                None
                if not request or not request["run_id"]
                else db.execute(
                    text(
                        "SELECT state, reason, snapshot_id FROM chat_decision_projections WHERE run_id=:run"
                    ),
                    {"run": request["run_id"]},
                )
                .mappings()
                .one_or_none()
            )
    finally:
        engine.dispose()
    require(all(row["case_id"] == report["case_id"] for row in attempts), "P4_PAID_CASE_SCOPE")
    cost = sum((Decimal(row["estimated_cost_usd"] or 0) for row in attempts), Decimal(0))
    report["paid_usage"] = {
        "request": dict(request) if request else None,
        "attempts": [dict(row) for row in attempts],
        "estimated_cost_usd": str(cost),
        "provider_invoice_verified": False,
        "run_events": [dict(row) for row in events],
        "projection": dict(projection) if projection else None,
    }


def finish(clients, report, output):
    path = f"conversations/{report['conversation_id']}/decisions"
    audit(report)
    request = report["paid_usage"]["request"]
    require(request and request["run_id"], "P4_INVESTIGATION_REQUEST_NOT_STARTED")
    run = request["run_id"]
    try:
        page = _wait(
            clients["support"],
            path,
            lambda data: (data.get("snapshot") or {}).get("original_run_id") == run,
            seconds=85,
        )
        snapshot = page["snapshot"]
        report["model_snapshot"] = snapshot
    finally:
        audit(report)
        _checkpoint(report, output, "paid_usage_readback")
    original = snapshot["candidate"]
    refund = next((item for item in original["options"] if item["action"] == "REFUND"), None)
    if refund is None or snapshot["checks"][refund["id"]]["status"] != "PASSED":
        report["business_completed"] = False
        report["status"] = "NEEDS_REVIEW"
        _checkpoint(report, output, "no_program_passed_refund_candidate")
        return
    report["model_candidate_program_passed"] = True
    unresolved = set(original["unresolved"])
    reviewed_items = {
        "缺少关联本次4900分退款处置方案的财务审批记录。",
        "现有人工材料审核仅登记材料所见及业务判断，不能据此确认图片真实性或运输责任。",
        "事实或条款主张仍有未核实或冲突项。",
    }
    if unresolved == reviewed_items:
        require(
            refund["lines"] == [{"line_id": "L01", "amount_cents": 4900, "quantity": None}]
            or refund["lines"] == [{"line_id": "L01", "amount_cents": 4900}],
            "P4_ORIGINAL_REFUND_SCOPE_CHANGED",
        )

        def document_authority_gap(current, eligible, _materials, _control):
            require(
                current == snapshot and eligible["status"] == "ELIGIBLE", "P4_REVIEW_SCOPE_CHANGED"
            )
            return {
                "action": "MODIFY",
                "reason": (
                    "合成验收核对：原商品行、金额、依据及方案内容原样采用。此处只生成待确认的方案，"
                    "财务审批在客户确认后由独立账号完成，批准前禁止执行。"
                    "已审核材料符合损坏处置要求，图片真实性与运输责任仍无结论，未作为退款依据。"
                ),
                "resolved_items": original["unresolved"],
                "replacement": refund,
            }

        with patch.object(historical_flow, "_review_choice", document_authority_gap):
            report["business_completed"] = _complete_business(
                clients,
                report["case_id"],
                report["conversation_id"],
                snapshot,
                report,
                output,
                control_modify=False,
            )
        review_snapshot = read(
            clients["support"], path + "/history/" + report["support_snapshot"]["id"]
        )
        require(
            review_snapshot["candidate"]["options"] == [refund]
            and read(clients["support"], path + "/history/" + snapshot["id"]) == snapshot,
            "P4_REVIEW_CHANGED_MODEL_REFUND_FIELDS",
        )
        report["support_kept_exact_model_option"] = True
    else:
        report["business_completed"] = _complete_business(
            clients,
            report["case_id"],
            report["conversation_id"],
            snapshot,
            report,
            output,
            control_modify=False,
        )
    report["status"] = "PASSED" if report["business_completed"] else "NEEDS_REVIEW"
    _checkpoint(report, output, "verified")


def verify_replay(clients, report, output):
    require(
        report["status"] == "PASSED" and report["business_completed"], "P4_REPLAY_NEEDS_RECEIPT"
    )
    support, customer = clients["support"], clients["customer"]
    case = report["case_id"]
    case_path = f"cases/{case}"
    ledger_before = read(support, case_path + "/executions")["items"]
    require(len(ledger_before) == 1, "P4_REPLAY_NEEDS_SINGLE_LEDGER_ENTRY")
    replay, _ = _mutation(
        support,
        report,
        output,
        "SIMULATED_EXECUTION_NEW_KEY",
        case_path + "/execute",
        200,
        {
            "expected_version": read(support, case_path)["case"]["version"],
            "proposal_id": report["proposal_id"],
        },
    )
    ledger_after = read(support, case_path + "/executions")["items"]
    order = read(customer, "orders/O9902")
    require(
        replay == ledger_before[0]
        and ledger_before == ledger_after
        and order["refunded_cents"] == 4900,
        "P4_NEW_KEY_EXECUTED_TWICE",
    )
    report["simulated_execution"]["replay_new_key"] = True
    report["simulated_execution"]["order_refunded_cents"] = order["refunded_cents"]
    _checkpoint(report, output, "verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "restage", "dispatch", "finish", "audit", "verify-replay"]
    )
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--budget-retry", action="store_true")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    configure(investigation=True)
    if args.phase == "prepare":
        if args.output.exists():
            parser.error("Output already exists")
        report = {
            "status": "RUNNING",
            "request_ids": {},
            "mutations": {},
            "data_scope": "approved synthetic O9902 fixture",
        }
        _checkpoint(report, args.output, "prepare")
    elif args.phase == "restage":
        if args.output.exists() or args.prior is None:
            parser.error("Restaging requires a prior audit and a new output")
        report = json.loads(args.prior.read_text())
        usage = report.get("paid_usage", {})
        require(
            usage.get("request", {}).get("state") == "STARTED", "P4_LIVE_RESTAGE_REQUEST_UNKNOWN"
        )
        if args.budget_retry:
            attempts = usage["attempts"]
            require(
                len(attempts) == 3
                and all(row["status"] == "SUCCEEDED" for row in attempts)
                and Decimal(usage["estimated_cost_usd"]) <= Decimal("0.05")
                and any(
                    event["kind"] == "status"
                    and event["payload"].get("reason") == "MODEL_COST_BUDGET_EXHAUSTED"
                    for event in usage["run_events"]
                ),
                "P4_LIVE_RESTAGE_REQUIRES_KNOWN_BUDGET_BLOCK",
            )
            report["prior_budget_attempts"] = attempts
            report["prior_budget_report"] = str(args.prior)
        else:
            require(usage.get("attempts") == [], "P4_LIVE_RESTAGE_REQUIRES_ZERO_ATTEMPTS")
            report["prior_zero_call_report"] = str(args.prior)
    else:
        report = json.loads(args.output.read_text())
    if args.phase == "dispatch":
        dispatch(report, args.output)
        return
    if args.phase == "audit":
        audit(report)
        _checkpoint(report, args.output, "paid_usage_readback")
        return
    credentials = json.loads((RUNTIME / ".runtime/users-test.json").read_text())
    with ExitStack() as resources:
        clients = {
            row["role"]: resources.enter_context(
                httpx.Client(
                    base_url=f"http://127.0.0.1:{API_PORT}/api/",
                    headers={"Authorization": f"Bearer {row['token']}"},
                    timeout=15,
                    trust_env=False,
                )
            )
            for row in credentials
        }
        if args.phase == "prepare":
            prepare(clients, report, args.output)
        elif args.phase == "restage":
            case = report["case_id"]
            reply = message(
                clients["customer"],
                report["conversation_id"],
                (
                    "补充说明：原商品照片与包裹照片仍对应这件杯子；杯身裂纹，我申请按当前订单实付金额退款。"
                    if args.budget_retry
                    else "补充说明：订单 O9902 的 L01 杯身有一处裂纹，仅一件受损，我希望退这一件。"
                ),
                request_ids(
                    report, "budget_retry_scope" if args.budget_retry else "scope_clarification"
                ),
            )
            require(reply["conversation"]["active_case_id"] == case, "P4_RESTAGE_CASE_CHANGED")
            _checkpoint(report, args.output, "awaiting_paid_dispatch")
        elif args.phase == "verify-replay":
            verify_replay(clients, report, args.output)
        else:
            finish(clients, report, args.output)


if __name__ == "__main__":
    main()
