"""Verify the deployed refund-status and missing-photo replies via app HTTP/Celery.

Default mode performs read-only preflight. --execute-paid requires a new report and
reserves a one-shot private state file before any case creation or run submission.
"""

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from tempfile import mkstemp
from uuid import UUID, uuid4

from aftercare.business.models import Case, Execution
from aftercare.business.rules import digest
from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine
from aftercare.intelligence.tables import AgentRun, RunMutation
from sqlalchemy import select
from sqlalchemy.orm import Session
from verify_container_live import (
    CheckFailed,
    authenticate,
    client_for,
    page,
    preflight,
    request,
    require,
    usage_complete,
    worker_audit,
)

STATE = ROOT / ".runtime/response-clarity-http-state.json"
CITATION_STATE = ROOT / ".runtime/response-clarity-citation-http-state.json"
RUN_BUDGET = Decimal("0.10")
POLL_SECONDS = 155
ACTIVE = {"QUEUED", "RUNNING"}
MESSAGES = {
    "refund_status": "我只想查询订单已有的退款执行记录和退款进度，请不要发起新操作。",
    "wrong_item": "收到的商品和订单不一致，目前没有照片，请告诉我要补哪两种照片。",
}
CITATION_MESSAGE = (
    "我只查退款进度：如果订单退款账本没有记录，是不是就能按执行超时或结果未知条款处理？"
    "请引用你核对的相关条款并说明前提，不发起新操作。"
)
REGISTERED_CITATIONS = {("POL08", "1.0"), ("POL12", "1.0")}
PHOTOS = {"WRONG_ITEM": "错发商品实物照片", "LABEL": "包裹外部标签照片"}
SNAPSHOT_FIELDS = ("case", "order", "ledger", "proposals", "approvals", "materials", "reviews")


def reserve(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


def save(path, value):
    fd, temp = mkstemp(prefix=".response-clarity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def select_orders(engine, support, args, intents):
    with Session(engine) as db:
        require(
            not db.scalar(select(AgentRun.id).where(AgentRun.status.in_(ACTIVE)).limit(1)),
            "APP_ALREADY_HAS_ACTIVE_AGENT_WORK",
        )
        used_scopes = {
            tuple(row) for row in db.execute(select(Case.order_id, Case.intent, Case.scope_hash))
        }
        refunds = set(db.scalars(select(Execution.order_id).where(Execution.action == "REFUND")))
    orders = [
        row
        for row in page(support, "orders")
        if row["status"] == "DELIVERED" and row["payment_status"] == "CONFIRMED" and row["items"]
    ]
    choices = {}
    for intent in intents:
        specified = args.refund_order_id if intent == "refund_status" else args.wrong_order_id
        eligible = [
            row
            for row in orders
            if (specified is None or row["id"] == specified)
            and (intent != "refund_status" or row["id"] not in refunds)
            and (intent != "wrong_item" or row["id"] != choices["refund_status"]["id"])
            and (row["id"], intent, digest([row["items"][0]["line_id"]])) not in used_scopes
        ]
        require(eligible, f"SAFE_{intent.upper()}_ORDER_NOT_FOUND")
        eligible.sort(key=lambda row: (row["id"] != "O0002", row["id"]))
        choices[intent] = eligible[0]
    return choices


def snapshot(detail):
    return {
        "sha256": {key: digest(detail[key]) for key in SNAPSHOT_FIELDS},
        "counts": {
            key: len(detail[key]) for key in SNAPSHOT_FIELDS if isinstance(detail[key], list)
        },
        "historical_refunded_cents": detail["order"]["refunded_cents"],
        "eligibility": detail["eligibility"],
    }


def refresh_evidence(engine, support, path, run_id, evidence):
    attempts, job = worker_audit(engine, run_id)
    known = [
        Decimal(row["estimated_cost_usd"])
        for row in attempts
        if row["estimated_cost_usd"] is not None
    ]
    evidence.update(
        attempts=attempts,
        dispatch=job,
        estimated_known_cost_usd=str(sum(known, Decimal(0))),
        unknown_cost_attempts=len(attempts) - len(known),
    )
    run = request(support, f"{path}/runs/{run_id}")
    evidence.update(
        run_status=run["status"],
        run_reason=run["reason"],
        final_output=run["output"],
        thread_id=run["thread_id"],
        model_usage_scope="provider_reported_usage_and_registry_estimate",
    )
    return run, attempts, job


def check_citation_conditions(output, evidence):
    citations = output.get("citations")
    assessments = output.get("citation_assessments")
    records_valid = all(
        isinstance(rows, list)
        and all(
            isinstance(row, dict)
            and all(isinstance(row.get(key), str) for key in ("id", "version", "quote"))
            for row in rows
        )
        for rows in (citations, assessments)
    )
    match = False
    conditions_valid = False
    observed = set()
    if records_valid:
        citation_keys = [(row["id"], row["version"], row["quote"]) for row in citations]
        assessment_keys = [(row["id"], row["version"], row["quote"]) for row in assessments]
        match = citation_keys == assessment_keys
        observed = {(id_, version) for id_, version, _ in citation_keys} & REGISTERED_CITATIONS
        conditions_valid = all(
            (
                row.get("condition_status") == "UNMET"
                and row.get("reason_code") == "NO_REFUND_EXECUTION_RECORD"
                and row.get("scope")
                == {"kind": "ORDER_REFUND_EXECUTION_LEDGER", "execution_ids": []}
            )
            if (row["id"], row["version"]) in REGISTERED_CITATIONS
            else row.get("condition_status") == "NOT_ASSESSED"
            for row in assessments
        )
    evidence["citation_coverage"] = "OBSERVED" if observed else "NOT_OBSERVED"
    evidence["citation_target_coverage"] = {
        f"{id_}@{version}": "OBSERVED" if (id_, version) in observed else "NOT_OBSERVED"
        for id_, version in sorted(REGISTERED_CITATIONS)
    }
    evidence["checks"].update(
        citation_assessments_match_published_quotes=match,
        citation_conditions_match_empty_ledger=conditions_valid,
    )


def verify_case(
    engine, support, actor, order, intent, config, private, evidence, persist, *, citation_check
):
    stage = private[intent]
    evidence.update(
        status="IN_PROGRESS", order_id=order["id"], line_id=order["items"][0]["line_id"]
    )
    description = CITATION_MESSAGE if citation_check else MESSAGES[intent]
    case_body = {
        "order_id": order["id"],
        "intent": intent,
        "description": description,
        "expected_version": order["version"],
        "line_ids": [evidence["line_id"]],
    }
    stage.update(phase="CASE_POST_PENDING", actor_id=actor["user_id"], case_body=case_body)
    persist()
    case = request(support, "/cases", body=case_body, key=stage["case_key"], expected=201)
    path = f"/cases/{UUID(case['id'])}"
    evidence["case_id"] = stage["case_id"] = case["id"]
    stage["phase"] = "CASE_CREATED"
    persist()
    before = request(support, path)
    evidence["before"] = snapshot(before)
    eligibility = before["eligibility"]
    refund = intent == "refund_status"
    expected_status = "STATUS_ONLY" if refund else "WAITING_INPUT"
    require(
        case["description"] == description
        and case["version"] == 1
        and not request(support, path + "/runs")
        and eligibility["status"] == expected_status
        and not eligibility["actions"]
        and not before["policy_resolution"]["blockers"]
        and all(
            not before[field]
            for field in ("ledger", "proposals", "approvals", "materials", "reviews")
        )
        and (
            not eligibility["missing_materials"]
            if refund
            else set(eligibility["missing_materials"]) == PHOTOS.keys()
        ),
        "CASE_PRECONDITIONS_CHANGED",
    )
    body = {"expected_version": case["version"], "mode": "single", "strategy": "analysis"}
    if citation_check:
        body["message"] = description
    stage.update(phase="RUN_POST_PENDING", run_body=body)
    persist()
    try:
        run = request(support, path + "/runs", body=body, key=stage["run_key"], expected=202)
        run_id = str(UUID(run["id"]))
    except (
        Exception
    ):  # Persist uncertainty and look up a committed mutation without replaying POST.
        with Session(engine) as db:
            mutation = db.get(RunMutation, (case["id"], actor["user_id"], stage["run_key"]))
        if mutation is not None:
            evidence["run_id"] = stage["run_id"] = mutation.run_id
            persist()
        raise
    evidence["run_id"] = stage["run_id"] = run_id
    stage["phase"] = "RUN_SUBMITTED"
    persist()

    deadline = time.monotonic() + POLL_SECONDS
    last = None
    while True:
        run, attempts, job = refresh_evidence(engine, support, path, run_id, evidence)
        observed = digest(evidence)
        if observed != last:
            persist()
            last = observed
        if run["status"] not in ACTIVE and job and job["state"] in {"DONE", "FAILED"}:
            break
        if time.monotonic() >= deadline:
            raise CheckFailed("RUN_OUTCOME_NOT_OBSERVED")
        time.sleep(0.5)

    evidence["after"] = snapshot(request(support, path))
    evidence["business_snapshot_unchanged"] = evidence["before"] == evidence["after"]
    events = request(support, f"{path}/runs/{run_id}/events")
    evidence["retrieval"] = [event["payload"] for event in events if event["kind"] == "retrieval"]
    output = run["output"] or {}
    notes = output.get("notes") or ""
    evidence["checks"] = {
        "business_unchanged": evidence["business_snapshot_unchanged"],
        "single_claim_done": job["state"] == "DONE" and job["claims"] == 1 and job["failures"] == 0,
        "no_action_or_execution": "action" in output
        and output["action"] is None
        and output.get("lines") == []
        and output.get("ledger") == [],
    }
    if refund:
        evidence["checks"].update(
            status_only=run["status"] == "COMPLETED" and run["reason"] == "STATUS_ONLY",
            explicitly_no_execution_record="退款执行账本未查到执行记录" in notes,
            historical_refund_separate="historical_refunded_cents" in output
            and output["historical_refunded_cents"] == before["order"]["refunded_cents"],
            hybrid=any(
                row["mode"] == "HYBRID" and row["semantic"] and not row["blockers"]
                for row in evidence["retrieval"]
            ),
            successful_analysis_attempt=any(
                row["status"] == "SUCCEEDED"
                and usage_complete(row)
                and row["metadata"].get("alias") == "analysis"
                and row["metadata"].get("model") == config["aliases"]["analysis"]["model"]
                and row["metadata"].get("registry_version") == config["registry_version"]
                for row in attempts
            ),
            known_estimate_within_budget=0 < len(attempts) <= config["max_run_attempts"]
            and evidence["unknown_cost_attempts"] == 0
            and Decimal(evidence["estimated_known_cost_usd"]) <= RUN_BUDGET,
        )
    else:
        missing = eligibility["missing_materials"]
        evidence["checks"].update(
            waiting_for_input=run["status"] == run["reason"] == "WAITING_INPUT",
            exact_materials=output.get("missing_materials") == missing,
            names_each_missing_photo=all(PHOTOS[kind] in notes for kind in missing),
            no_model_attempts=not attempts,
        )
    if citation_check:
        check_citation_conditions(output, evidence)
    if all(evidence["checks"].values()):
        evidence["status"] = (
            "PARTIAL" if evidence.get("citation_coverage") == "NOT_OBSERVED" else "PASSED"
        )
    else:
        evidence["status"] = "UNKNOWN" if evidence["unknown_cost_attempts"] else "FAILED"
    stage["phase"] = "FINISHED"
    persist()


def run_checks(args, report, private, persist):
    settings = load_settings("app")
    engine = guarded_engine(settings)
    try:
        credentials = json.loads((ROOT / ".runtime/users-app.json").read_text())
        tokens = {row["role"]: row["token"] for row in credentials}
        with client_for(tokens["customer"]) as customer, client_for(tokens["support"]) as support:
            authenticate(engine, customer, tokens["customer"], "customer")
            actor = authenticate(engine, support, tokens["support"], "support")
            config = preflight(settings, customer, support, args.compose_project, report)
            intents = ("refund_status",) if args.citation_check else ("refund_status", "wrong_item")
            orders = select_orders(engine, support, args, intents)
            report["selected_orders"] = {intent: row["id"] for intent, row in orders.items()}
            if not args.execute_paid:
                return
            report["status"] = "IN_PROGRESS"
            persist()
            # Check the free deterministic gate first. A failure keeps the paid case NOT_RUN.
            for intent in reversed(intents):
                try:
                    verify_case(
                        engine,
                        support,
                        actor,
                        orders[intent],
                        intent,
                        config,
                        private,
                        report[intent],
                        persist,
                        citation_check=args.citation_check,
                    )
                except Exception as error:  # noqa: BLE001 - retain evidence without replay
                    stage = private[intent]
                    evidence = report[intent]
                    run_id = stage.get("run_id")
                    if run_id:
                        try:
                            refresh_evidence(
                                engine, support, f"/cases/{stage['case_id']}", run_id, evidence
                            )
                        except Exception:  # noqa: BLE001 - preserve prior observation on read failure
                            evidence["audit_readback"] = "UNAVAILABLE"
                    evidence.update(
                        status="UNKNOWN"
                        if stage.get("phase") in {"RUN_POST_PENDING", "RUN_SUBMITTED"}
                        else "FAILED",
                        failure=str(error)
                        if isinstance(error, CheckFailed)
                        else type(error).__name__,
                    )
                    persist()
                if report[intent]["status"] != "PASSED":
                    break
            statuses = {report[intent]["status"] for intent in intents}
            if args.citation_check:
                report["status"] = report["refund_status"]["status"]
            else:
                report["status"] = (
                    "PASSED"
                    if statuses == {"PASSED"}
                    else ("UNKNOWN" if "UNKNOWN" in statuses else "FAILED")
                )
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument(
        "--citation-check", action="store_true", help="Run only the conditional citation probe"
    )
    parser.add_argument(
        "--output", type=Path, help="New private report file for this one-shot execution"
    )
    parser.add_argument("--compose-project", default="aftercare-v2")
    parser.add_argument("--refund-order-id")
    parser.add_argument("--wrong-order-id")
    args = parser.parse_args()
    if args.execute_paid != (args.output is not None):
        parser.error("--execute-paid and --output must be used together")
    if args.citation_check and args.wrong_order_id is not None:
        parser.error("--wrong-order-id is unavailable with --citation-check")
    state = CITATION_STATE if args.citation_check else STATE
    if args.output is not None and args.output.resolve() in {
        STATE.resolve(),
        CITATION_STATE.resolve(),
    }:
        parser.error("--output must differ from the private execution state")
    intents = ("refund_status",) if args.citation_check else tuple(MESSAGES)
    report = {
        "status": "NOT_RUN",
        "source": "app_deployed_http_celery_postgresql",
        "execute_paid": args.execute_paid,
        "maximum_paid_runs": 1,
        "maximum_attempts": 12,
        "estimated_run_budget_usd": str(RUN_BUDGET),
        "citation_support_review": "NOT_ASSESSED",
        **{intent: {"status": "NOT_RUN"} for intent in intents},
    }
    if args.citation_check:
        report["probe"] = "conditional_citations_empty_refund_ledger"
        report["refund_status"]["citation_coverage"] = "NOT_OBSERVED"
    private = {
        "report_path": str(args.output.resolve()) if args.output is not None else None,
        **{intent: {"case_key": str(uuid4()), "run_key": str(uuid4())} for intent in intents},
    }

    def persist():
        save(state, private)
        save(args.output, report)

    reserved = False
    try:
        if args.execute_paid:
            require(not os.path.lexists(state), "PRIVATE_STATE_EXISTS_MANUAL_READBACK_ONLY")
            reserve(args.output)
            reserve(state)
            reserved = True
            persist()
        run_checks(args, report, private, persist)
    except Exception as error:  # noqa: BLE001 - redact setup and transport exception text
        report.update(
            status="FAILED",
            failure=str(error) if isinstance(error, CheckFailed) else type(error).__name__,
        )
    finally:
        if reserved:
            persist()
    print(
        json.dumps(
            {
                "status": report["status"],
                "failure": report.get("failure"),
                "selected_orders": report.get("selected_orders"),
                "maximum_paid_runs": 1,
                "maximum_attempts": 12,
                "estimated_run_budget_usd": str(RUN_BUDGET),
            },
            ensure_ascii=False,
        )
    )
    return int(report["status"] not in {"PASSED", "PREFLIGHT_PASSED"})


if __name__ == "__main__":
    raise SystemExit(main())
