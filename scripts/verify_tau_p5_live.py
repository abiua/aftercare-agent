"""Stage two isolated synthetic P5 investigations and retain their actual model evidence."""

import argparse
import fcntl
import json
import os
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from unittest.mock import patch

import httpx
from tau_p5_runtime import (
    API_PORT,
    CONTINUATION_MAX_TOTAL_CALLS,
    CONTINUATION_MAX_TOTAL_MICROUSD,
    CONTINUATION_TIMEOUT,
    RUNTIME,
    TIMEOUT_CHOICES,
    configure,
)
from verify_chat_live_business_path import _checkpoint, _mutation
from verify_chat_p1_http import call, message, read, request_ids
from verify_fresh_bootstrap import require

CONTINUATION_ORDERS = {"O9951": "O9961", "O9952": "O9962"}
CONTINUATION_ROUND = "timeout-continuation-20261112"
TERMINAL = {"NEEDS_REVIEW", "SUCCEEDED", "FAILED", "CANCELED", "BLOCKED"}


def independent_fixture(fixture):
    """Clone the frozen order under a new synthetic ID without modifying its history."""
    from aftercare.business.seed import initialize
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine

    source_id = fixture["order"]["id"]
    new_id = CONTINUATION_ORDERS[source_id]
    order = {**fixture["order"], "id": new_id}
    engine = guarded_engine(load_settings("test"))
    try:
        initialize(
            engine,
            {
                "customers": [],
                "products": [],
                "orders": [order],
                "shipments": [],
                "policies": [],
            },
        )
    finally:
        engine.dispose()
    return {
        **fixture,
        "order": order,
        "customer_message": fixture["customer_message"].replace(source_id, new_id),
    }


def compare_independent_order(customer, source_id, order, fixture):
    from tau_p5_cases import p5_cases

    original = next(row for row in p5_cases() if row["order"]["id"] == source_id)
    original_order = read(customer, f"orders/{source_id}")
    fields = sorted(set(order) | set(original_order))
    equal = {
        field: order.get(field) == original_order.get(field)
        and (field in order) == (field in original_order)
        for field in fields
        if field != "id"
    }
    fixture_fields_equal = (
        all(order.get(field) == value for field, value in fixture["order"].items())
        and order["products"] == fixture["products"]
    )
    require(all(equal.values()) and fixture_fields_equal, "P5_CLONE_ORDER_DATA_CHANGED")
    source_bytes = json.dumps(original, ensure_ascii=False, sort_keys=True).encode()
    return {
        "source_order_id": source_id,
        "source_fixture_sha256": sha256(source_bytes).hexdigest(),
        "source_order_api_fields_equal": equal,
        "clone_matches_frozen_fixture": fixture_fields_equal,
    }


def stage(clients, report, output, fixture):
    from tau_p5_cases import p5_inputs

    customer, support = clients["customer"], clients["support"]
    require(read(customer, "runtime")["mode"] == "offline", "P5_API_NOT_OFFLINE")
    order_id = fixture["order"]["id"]
    order = read(customer, f"orders/{order_id}")
    require(order["customer_id"] == "C001", "P5_OWNER_CHANGED")
    lines = [row["line_id"] for row in order["items"]]
    require(set(lines) == {"L01", "L02"}, "P5_LINE_SCOPE_CHANGED")
    report["order"] = order
    if "fixture_source_order_id" in report:
        report["fixture_comparison"] = compare_independent_order(
            customer, report["fixture_source_order_id"], order, fixture
        )
    created, _ = _mutation(customer, report, output, "create", "conversations", 201, {})
    conversation = report["conversation_id"] = created["id"]
    path = f"conversations/{conversation}"
    selected, _ = _mutation(
        customer,
        report,
        output,
        "context",
        f"{path}/context",
        200,
        {
            "order_id": order_id,
            "line_ids": lines,
            "expected_revision": created["revision"],
            "new_issue": False,
        },
    )
    require(not selected["conversation"]["active_case_id"], "P5_CONTEXT_ALREADY_BOUND")
    initial = message(
        customer, conversation, fixture["customer_message"], request_ids(report, "initial")
    )
    case = report["case_id"] = initial["conversation"]["active_case_id"]
    require(case, "P5_CASE_NOT_BOUND")
    _, assets, originals, _ = p5_inputs()
    data = message(
        customer,
        conversation,
        "",
        request_ids(report, "images"),
        [(asset["file"], originals[asset["id"]]) for asset in assets],
    )
    (uploaded,) = [row for row in data["messages"] if row["role"] == "customer"]
    attachments = uploaded["attachments"]
    page = read(support, f"{path}/decision-materials")
    report["issue_id"] = page["issue_id"]
    materials = report["materials"] = []
    for attachment, asset, kind in zip(attachments, assets, ("ITEM", "PARCEL"), strict=True):
        page = read(support, f"{path}/decision-materials")
        linked, _ = _mutation(
            support,
            report,
            output,
            f"link_{kind}",
            f"{path}/decision-materials",
            200,
            {
                "attachment_id": attachment["id"],
                "kind": kind,
                "expected_issue_id": report["issue_id"],
                "expected_case_id": case,
                "expected_materials_version": page["materials_version"],
            },
        )
        ident = linked["material"]["id"]
        fetched = call(support, "GET", f"cases/{case}/materials/{ident}/content", 200)
        require(
            attachment["sha256"] == asset["sha256"] and fetched.content == originals[asset["id"]],
            "P5_ORIGINAL_READBACK_CHANGED",
        )
        materials.append(
            {
                "material_id": ident,
                "kind": kind,
                **linked["source"],
                "original_readback_sha256": sha256(fetched.content).hexdigest(),
            }
        )
    detail = read(support, f"cases/{case}")
    review, _ = _mutation(
        support,
        report,
        output,
        "review",
        f"cases/{case}/material-reviews",
        201,
        {
            "expected_version": detail["case"]["version"],
            "decision": "APPROVED",
            "notes": "合成演示材料人工核验记录，仅列出当前来源可归属的商品行。",
            "observations": [
                {
                    "material_id": materials[0]["material_id"],
                    "finding": "VISIBLE_DAMAGE",
                    "line_id": line,
                    "affected_quantity": 1,
                }
                for row in fixture["attachments"]
                if row["kind"] == "ITEM"
                for line in row["line_ids"]
            ],
        },
    )
    report["material_review_id"] = review["id"]
    _checkpoint(report, output, "analysis_pending")


def prepare_revision(customer, prior_path, report, output, *, continuation=False):
    previous = json.loads(prior_path.read_text())
    require(
        (
            previous["status"] in {"LIVE_CAPTURED", "LIVE_REVIEW_REQUIRED"}
            and previous["stage"] == "live_readback"
            and previous["paid_usage"]["run"]["status"] in TERMINAL
            if continuation
            else previous["order"]["id"] == "O9952" and previous["status"] == "LIVE_CAPTURED"
        )
        and previous["paid_usage"]["projection"]["state"] == "PUBLISHED",
        "P5_REVISION_REQUIRES_CAPTURED_MATERIAL_CASE",
    )
    for key in (
        "order",
        "fixture",
        "case_id",
        "conversation_id",
        "issue_id",
        "materials",
        "material_review_id",
    ):
        report[key] = previous[key]
    report["revises"] = {
        "report": prior_path.name,
        "report_sha256": sha256(prior_path.read_bytes()).hexdigest(),
        "turn_id": previous["paid_turn_id"],
        "snapshot_id": previous["paid_usage"]["projection"]["snapshot_id"],
        "run_id": previous["paid_usage"]["run"]["id"],
    }
    report["customer_supplement"] = (
        "补充说明：订单 O9952 的 L01 白色马克杯破损照和同一订单的包裹照片之前已经上传，"
        "客服看过 L01 商品照。L02 只有我的破损描述，目前没有它自己的商品照片。"
        "我仍想处理这两件，请先核对已提交材料，告诉我还缺哪一行的材料。"
    )
    if "revises" in previous:
        report["customer_supplement"] = (
            "再补充确认一下，我继续申请处理 O9952 的两件破损商品。"
            "L01 的商品破损照和同单包裹照已经提交，L02 暂时没有自己的商品照。"
            "请先核对客服记录的已有材料，再告诉我还要补哪张。"
        )
    if continuation:
        report["acceptance_round"] = CONTINUATION_ROUND
        order_id = previous["order"]["id"]
        if "fixture_source_order_id" in previous:
            report["fixture_source_order_id"] = previous["fixture_source_order_id"]
            report["evidence_scope"] = "same_independent_synthetic_case_revision"
        report["customer_supplement"] = f"请继续核对 {order_id} 这两件商品的售后。"
    _checkpoint(report, output, "revision_pending")
    response = message(
        customer,
        report["conversation_id"],
        report["customer_supplement"],
        request_ids(report, "customer_supplement"),
    )
    require(
        response["conversation"]["active_case_id"] == report["case_id"], "P5_REVISION_CASE_CHANGED"
    )
    _checkpoint(report, output, "analysis_pending")


def analyze(report, output):
    from aftercare.conversations.analysis import process
    from aftercare.conversations.investigation_delivery import pending
    from aftercare.conversations.service import ConversationService
    from aftercare.runtime import open_runtime
    from sqlalchemy import text

    with open_runtime("test") as (settings, engine, business, agent):
        chat = ConversationService(business)
        deadline = monotonic() + 20
        while monotonic() < deadline:
            with engine.connect() as db:
                turn = (
                    db.execute(
                        text(
                            "SELECT id, state FROM chat_analysis_turns WHERE conversation_id=:id "
                            "ORDER BY generation DESC LIMIT 1"
                        ),
                        {"id": report["conversation_id"]},
                    )
                    .mappings()
                    .one()
                )
            if turn["state"] == "PUBLISHED" and turn["id"] in pending(business):
                require(
                    turn["id"] != report.get("revises", {}).get("turn_id"),
                    "P5_REVISION_TURN_UNCHANGED",
                )
                report["paid_turn_id"] = turn["id"]
                report["status"] = "READY_FOR_REVIEWED_DISPATCH"
                _checkpoint(report, output, "awaiting_live")
                return
            if turn["id"] in chat.analysis.due():
                process(chat, turn["id"], settings, agent.model)
            sleep(0.5)
    raise RuntimeError("P5_ANALYSIS_NOT_READY")


def audit(engine, report):
    from sqlalchemy import text

    with engine.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
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
        attempts = (
            db.execute(
                text(
                    "SELECT a.run_id,a.logical_id,a.ordinal,a.status,a.error_code,a.usage,"
                    "a.estimated_cost_usd,a.metadata_json,a.result,r.case_id,"
                    "a.started_at::text,a.finished_at::text FROM agent_model_attempts a "
                    "JOIN agent_runs r ON r.id=a.run_id ORDER BY a.started_at"
                )
            )
            .mappings()
            .all()
        )
        vision_calls = db.scalar(text("SELECT count(*) FROM chat_model_attempts"))
        executions = db.scalar(text("SELECT count(*) FROM business_executions"))
        run = events = projection = snapshot = None
        if request and request["run_id"]:
            bindings = {"run": request["run_id"]}
            run = (
                db.execute(
                    text("SELECT id,status,reason,output FROM agent_runs WHERE id=:run"), bindings
                )
                .mappings()
                .one()
            )
            events = (
                db.execute(
                    text(
                        "SELECT kind,payload,seq,created_at::text FROM agent_run_events WHERE run_id=:run ORDER BY seq"
                    ),
                    bindings,
                )
                .mappings()
                .all()
            )
            projection = (
                db.execute(
                    text(
                        "SELECT state,reason,snapshot_id FROM chat_decision_projections WHERE run_id=:run"
                    ),
                    bindings,
                )
                .mappings()
                .one_or_none()
            )
            if projection and projection["snapshot_id"]:
                snapshot = (
                    db.execute(
                        text(
                            "SELECT id,version,parent_id,sha256,checks FROM chat_decision_snapshots "
                            "WHERE id=:snapshot"
                        ),
                        {"snapshot": projection["snapshot_id"]},
                    )
                    .mappings()
                    .one()
                )
        previous_snapshot = report.get("revises", {}).get("snapshot_id")
        prior_snapshot_sha = (
            db.scalar(
                text("SELECT sha256 FROM chat_decision_snapshots WHERE id=:snapshot"),
                {"snapshot": previous_snapshot},
            )
            if previous_snapshot
            else None
        )
    cost = sum((Decimal(row["estimated_cost_usd"] or 0) for row in attempts), Decimal(0))
    unreconciled = sum(
        (
            Decimal(row["metadata_json"]["reserved_cost_usd"])
            for row in attempts
            if row["estimated_cost_usd"] is None
        ),
        Decimal(0),
    )
    report["paid_usage"] = {
        "request": dict(request) if request else None,
        "run": dict(run) if run else None,
        "attempts": [dict(row) for row in attempts],
        "estimated_cost_usd": str(cost),
        "unreconciled_reserved_usd": str(unreconciled),
        "conservative_booked_usd": str(cost + unreconciled),
        "provider_invoice_verified": False,
        "vision_attempts": vision_calls,
        "events": [dict(row) for row in events] if events else [],
        "projection": dict(projection) if projection else None,
        "projected_snapshot": dict(snapshot) if snapshot else None,
        "previous_snapshot_sha256": prior_snapshot_sha,
        "executions": executions,
    }
    return report["paid_usage"]


@contextmanager
def transport_timing(report, max_attempts):
    """Time the unmodified private SDK path and retain role labels only."""
    from aftercare.intelligence import models

    original = models._private_transport
    intervals = report["transport_intervals"] = []
    lock = Lock()

    def observe(spec, messages, tools, timeout, output):
        role = str(messages[0].content).split("。", 1)[0].removeprefix("调查角色 ")
        require(
            role in {"fact", "policy", "verifier", "synthesizer", "strong_single"},
            "P5_TRANSPORT_UNEXPECTED_ROLE",
        )
        with lock:
            require(len(intervals) < max_attempts, "P5_TRANSPORT_LIMIT_EXHAUSTED")
            entry = {
                "role": role,
                "started_at": datetime.now(UTC).isoformat(),
                "timeout_seconds": timeout,
            }
            started = monotonic()
            intervals.append(entry)
        try:
            result = original(spec, messages, tools, timeout, output)
            entry["outcome"] = "SDK_RETURNED"
            return result
        except BaseException as error:
            entry["outcome"] = type(error).__name__
            raise
        finally:
            entry["finished_at"] = datetime.now(UTC).isoformat()
            entry["elapsed_seconds"] = round(monotonic() - started, 3)

    with patch.object(models, "_private_transport", observe):
        yield
    facts = [row for row in intervals if row["role"] == "fact"]
    policies = [row for row in intervals if row["role"] == "policy"]
    report["fact_policy_transport_overlap"] = any(
        max(fact["started_at"], policy["started_at"])
        < min(fact["finished_at"], policy["finished_at"])
        for fact in facts
        for policy in policies
    )


def dispatch(report, output, *, independent_continuation=None):
    from aftercare.conversations.decision_delivery import project
    from aftercare.conversations.investigation_delivery import pending, process
    from aftercare.runtime import open_runtime

    require(report["stage"] == "awaiting_live", "P5_LIVE_ALREADY_ATTEMPTED")
    with open_runtime("test", dispatch=lambda _: None) as (settings, engine, business, agent):
        prior = audit(engine, report)
        max_attempts = agent.model.registry.max_run_attempts
        continuation = agent.model.registry.timeout_seconds == CONTINUATION_TIMEOUT
        require(
            continuation == (report.get("acceptance_round") == CONTINUATION_ROUND),
            "P5_LIVE_ACCEPTANCE_ROUND_MISMATCH",
        )
        if continuation:
            require(
                not (RUNTIME / "controlled-recheck-20261112.sqlite").exists(),
                "P5_CONTROLLED_EVALUATION_RESERVED_FINAL_BUDGET",
            )
        legacy_journal = RUNTIME / "paid-dispatches.jsonl"
        journal = (
            RUNTIME / f"paid-dispatches-{CONTINUATION_ROUND}.jsonl"
            if continuation
            else legacy_journal
        )
        total_calls = CONTINUATION_MAX_TOTAL_CALLS if continuation else 24
        total_usd = (
            Decimal(CONTINUATION_MAX_TOTAL_MICROUSD) / 1_000_000
            if continuation
            else Decimal("1.40")
        )
        previous = (
            [json.loads(row) for row in journal.read_text().splitlines()]
            if journal.exists()
            else []
        )
        if continuation:
            legacy = [json.loads(line) for line in legacy_journal.read_text().splitlines()]
            require(len(legacy) == 4, "P5_LIVE_PRIOR_ROUND_CHANGED")
            previous_reports = [
                (Path(row["report"]), json.loads(Path(row["report"]).read_text()))
                for row in legacy + previous
            ]
            known_runs = set()
            for _, row in previous_reports:
                usage = row["paid_usage"]
                require(row["stage"] == "live_readback", "P5_LIVE_PRIOR_RUN_REQUIRES_REVIEW")
                if usage["run"]:
                    require(usage["run"]["status"] in TERMINAL, "P5_LIVE_PRIOR_RUN_REQUIRES_REVIEW")
                    known_runs.add(usage["run"]["id"])
                else:
                    require(
                        usage["request"]["state"] == "BLOCKED"
                        and usage["request"]["reason"] == "INVESTIGATION_SPEND_UNCERTAIN"
                        and not row.get("transport_intervals"),
                        "P5_LIVE_PRIOR_RUN_REQUIRES_REVIEW",
                    )
            legacy_runs = {
                row["paid_usage"]["run"]["id"] for _, row in previous_reports[: len(legacy)]
            }
            require(
                all(row["run_id"] in known_runs for row in prior["attempts"])
                and sum(row["run_id"] in legacy_runs for row in prior["attempts"]) == 21,
                "P5_LIVE_LEDGER_BASELINE_CHANGED",
            )
            require(
                all(row["paid_turn_id"] != report["paid_turn_id"] for _, row in previous_reports),
                "P5_LIVE_TURN_ALREADY_DISPATCHED",
            )
            same_case = [
                (path, row) for path, row in previous_reports if row["case_id"] == report["case_id"]
            ]
            same_order = [row for _, row in same_case]
            if same_case:
                previous_path, _ = same_case[-1]
                require(
                    "revises" in report
                    and sha256(previous_path.read_bytes()).hexdigest()
                    == report["revises"]["report_sha256"],
                    "P5_LIVE_PRIOR_RUN_REQUIRES_REVIEW",
                )
            else:
                require(
                    not any(
                        row["order"]["id"] == report["order"]["id"] and row["paid_usage"]["run"]
                        for _, row in previous_reports[len(legacy) :]
                    ),
                    "P5_LIVE_REPEAT_INDEPENDENT_ORDER",
                )
        else:
            same_order = [
                json.loads(Path(row["report"]).read_text())
                for row in previous
                if row["order_id"] == report["order"]["id"]
            ]
        require(
            (continuation or len(previous) < 4)
            and all(row.get("turn_id") != report["paid_turn_id"] for row in previous)
            and (
                not same_order
                if "revises" not in report
                else same_order
                and same_order[-1]["paid_turn_id"] == report["revises"]["turn_id"]
                and (continuation or all(row["status"] == "LIVE_CAPTURED" for row in same_order))
            ),
            "P5_LIVE_DISPATCH_RESERVATION_EXHAUSTED",
        )
        earlier_runs = {row["paid_usage"]["run"]["id"] for row in same_order}
        require(
            not prior["request"]
            and 0 < max_attempts <= 12
            and agent.model.registry.max_run_usd <= Decimal("0.70")
            and prior["vision_attempts"] == 0
            and prior["executions"] == 0
            and report["paid_turn_id"] in pending(business)
            and len(prior["attempts"]) + max_attempts <= total_calls
            and Decimal(prior["conservative_booked_usd"]) + agent.model.registry.max_run_usd
            <= total_usd
            and all(
                row["status"] != "STARTED"
                and (row["case_id"] != report["case_id"] or row["run_id"] in earlier_runs)
                for row in prior["attempts"]
            ),
            "P5_LIVE_LEDGER_PREFLIGHT",
        )
        if previous and not continuation:
            prior_path = Path(previous[0]["report"])
            prior_report = json.loads(prior_path.read_text())
            if prior_report["status"] != "LIVE_CAPTURED":
                require(
                    independent_continuation == prior_path
                    and prior_report["status"] == "LIVE_REVIEW_REQUIRED"
                    and prior_report["stage"] == "live_readback"
                    and prior_report["paid_usage"]["run"]["status"] in TERMINAL,
                    "P5_LIVE_PRIOR_RUN_REQUIRES_REVIEW",
                )
                report["independent_continuation"] = {
                    "approved_by": "root review",
                    "preserved_failure_report": prior_path.name,
                    "prior_report_sha256": sha256(prior_path.read_bytes()).hexdigest(),
                    "prior_transport_count": len(prior["attempts"]),
                    "prior_booked_usd": prior["conservative_booked_usd"],
                    "prior_unreconciled_reserved_usd": prior["unreconciled_reserved_usd"],
                    "replay": False,
                }
        if "revises" in report:
            prior_snapshot = report["revises"]["snapshot_id"]
            from sqlalchemy import text

            with engine.connect() as db:
                prior_sha = db.scalar(
                    text("SELECT sha256 FROM chat_decision_snapshots WHERE id=:snapshot"),
                    {"snapshot": prior_snapshot},
                )
            require(prior_sha, "P5_PRIOR_SNAPSHOT_MISSING")
            report["revises"]["snapshot_sha256"] = prior_sha
        report["model_scope"] = {
            "timeout_seconds": agent.model.registry.timeout_seconds,
            "deadline_seconds": agent.model.registry.deadline_seconds,
            "max_attempts": max_attempts,
            "max_run_usd": str(agent.model.registry.max_run_usd),
            "direct_recheck_request": settings.chat_investigation_direct_recheck_request,
            "effective_investigation_max_attempts": min(
                max_attempts,
                agent.model.registry.max_attempts,
                settings.chat_investigation_run_max_calls,
            ),
            "registry_sha256": sha256(settings.model_config_path.read_bytes()).hexdigest(),
            "investigation_source_sha256": {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in sorted(
                    (
                        Path(__file__).resolve().parents[1] / "backend/src/aftercare/investigation"
                    ).glob("*.py")
                )
            },
            "max_total_attempts": total_calls,
            "max_total_usd": str(total_usd),
            **(
                {
                    "acceptance_budget_revision": {
                        "previous_total_attempt_cap": 61,
                        "revised_total_attempt_cap": total_calls,
                        "reason": "complete_final_verifier_recheck_after_output_validation_failure",
                        "recorded_at": "2026-11-12T19:42:12+08:00",
                    }
                }
                if continuation
                else {}
            ),
            "booked_before_dispatch_usd": prior["conservative_booked_usd"],
            "unreconciled_before_dispatch_usd": prior["unreconciled_reserved_usd"],
        }
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(
                json.dumps(
                    {
                        "order_id": report["order"]["id"],
                        "report": str(output),
                        "turn_id": report["paid_turn_id"],
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        report["transport_intervals"] = []
        _checkpoint(report, output, "live_dispatch_pending")
        try:
            process(business, agent, report["paid_turn_id"])
            usage = audit(engine, report)
            require(usage["request"] and usage["request"]["run_id"], "P5_LIVE_REQUEST_NO_RUN")
            run_id = usage["request"]["run_id"]
            with transport_timing(report, max_attempts):
                agent.process(run_id)
            project(business, run_id)
            usage = audit(engine, report)
            current = [row for row in usage["attempts"] if row["run_id"] == run_id]
            require(
                0 < len(current) == len(report["transport_intervals"]) <= max_attempts
                and len(usage["attempts"]) <= total_calls
                and Decimal(usage["conservative_booked_usd"]) <= total_usd
                and usage["run"]["status"] in TERMINAL
                and usage["executions"] == 0
                and usage["previous_snapshot_sha256"]
                == report.get("revises", {}).get("snapshot_sha256"),
                "P5_LIVE_TRANSPORT_OR_STATE_UNVERIFIED",
            )
            require(
                all(
                    row["status"] == "SUCCEEDED" and row["estimated_cost_usd"] is not None
                    for row in current
                ),
                "P5_LIVE_RECONCILIATION_REQUIRED",
            )
            report["status"] = "LIVE_CAPTURED"
        except BaseException as error:
            report["status"] = "LIVE_REVIEW_REQUIRED"
            report["failure"] = type(error).__name__
            raise
        finally:
            audit(engine, report)
            _checkpoint(report, output, "live_readback")


def main():
    from tau_p5_cases import p5_cases

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=("prepare", "prepare-revision", "analyze", "live", "audit")
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--order", choices=sorted(CONTINUATION_ORDERS))
    parser.add_argument("--prior-report", type=Path)
    parser.add_argument("--timeout-seconds", type=int, choices=TIMEOUT_CHOICES, default=25)
    parser.add_argument("--max-attempts", type=int, choices=(9, 12), default=12)
    parser.add_argument("--continuation", action="store_true")
    parser.add_argument("--continue-after-independent-failure", type=Path)
    args = parser.parse_args()
    configure(
        live=args.phase == "live",
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
    )
    output = args.output.resolve()
    with (RUNTIME / "acceptance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.phase in {"prepare", "prepare-revision"}:
            require(not output.exists(), "P5_PREPARE_REQUIRES_NEW_REPORT")
            report = {"status": "STAGING", "request_ids": {}, "mutations": {}}
            _checkpoint(report, output, "prepare")
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
                    require(args.order, "P5_PREPARE_REQUIRES_ORDER")
                    fixture = next(row for row in p5_cases() if row["order"]["id"] == args.order)
                    if args.continuation:
                        report["acceptance_round"] = CONTINUATION_ROUND
                        report["evidence_scope"] = "independent_synthetic_case"
                        report["fixture_source_order_id"] = args.order
                        fixture = independent_fixture(fixture)
                    report["fixture"] = fixture
                    stage(clients, report, output, fixture)
                else:
                    require(args.prior_report, "P5_REVISION_REQUIRES_PRIOR_REPORT")
                    prepare_revision(
                        clients["customer"],
                        args.prior_report,
                        report,
                        output,
                        continuation=args.continuation,
                    )
                analyze(report, output)
        else:
            report = json.loads(output.read_text())
            if args.phase == "live":
                prior = args.continue_after_independent_failure
                dispatch(
                    report, output, independent_continuation=prior.resolve() if prior else None
                )
            elif args.phase == "analyze":
                require(report["stage"] == "analysis_pending", "P5_ANALYSIS_ALREADY_ATTEMPTED")
                analyze(report, output)
            else:
                from aftercare.config import load_settings
                from aftercare.db import guarded_engine

                engine = guarded_engine(load_settings("test"))
                try:
                    audit(engine, report)
                    _checkpoint(report, output, report["stage"])
                finally:
                    engine.dispose()


if __name__ == "__main__":
    main()
