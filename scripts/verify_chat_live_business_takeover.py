"""Continue only the known failed 5203 case through synthetic support takeover."""

import argparse
import json
import os
import re
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import offline_demo
import verify_chat_live_business_path as driver
import verify_chat_p5_pg as pg
from aftercare.business.rules import digest
from verify_chat_backup_restore import private_json
from verify_chat_p1_http import call, read, with_key
from verify_chat_p2_offline import _wait, owned_container
from verify_chat_p3_offline import _code, _ref
from verify_fresh_bootstrap import VerificationError, require
from verify_material_review_migration import _docker, _hash
from verify_streaming_compatibility import _save

SOURCE = driver.ROOT / ".runtime/chat-live-business-execution-20261111.json"
SOURCE_SHA = "16cbe8ceb6bc1f153d8d91ec5f061346c60e7da0b6acacfc14adc882e6eb6386"
STACK_ID = "00a24b61290a455c8c21ad2b4b0cfed4"
SYSTEM_ID = "7706902315829465127"
CONVERSATION = "a45fc8d3-12a5-4d5d-b66d-3be309854df4"
CASE = "8f7e45ab-3e1c-42c3-8db3-b54b93c4abd8"
ISSUE = "f9dde49d-d947-46b6-8f5c-d47265a0f263"
SNAPSHOT = "02b1e016-fea8-46e2-9353-50a6d64a945d"
RUN = "66375250-0921-40b7-9e95-2ea2bc570f2f"
REVIEW = "dbdae4dd-4617-4157-b1de-9b75c01d073e"
FAILURE = "LIVE_BUSINESS_WORKER_RETRY_OR_UNPRICED_ATTEMPT"
TECHNICAL = ("本轮调查尚未形成可审核的完整方案，需要客服继续核对。", FAILURE)
ORDER, LINE, POLICY, CENTS = "O9902", "L01", "POL01", 4900
LINES = [{"line_id": LINE, "amount_cents": CENTS}]
PATH = f"conversations/{CONVERSATION}/decisions"
CASE_PATH = f"cases/{CASE}"
JOURNAL = "live-business-takeover-admitted.json"
ZERO_BEFORE = (
    "chat_support_decisions",
    "chat_customer_decisions",
    "business_proposals",
    "business_approvals",
    "business_executions",
    "business_simulated_payments",
)


def _source():
    require(_hash(SOURCE) == SOURCE_SHA, "LIVE_TAKEOVER_SOURCE_SHA_CHANGED")
    original = private_json(SOURCE)
    require(
        original["status"] == "FAILED"
        and original["failure_code"] == "LIVE_BUSINESS_TEXT_NATIVE_BINDING_CHANGED"
        and digest(original["plan"]) == original["plan_sha256"]
        and (
            original["owned_stack"],
            original["system_identifier"],
            original["owned_url"],
            original["conversation_id"],
            original["case_id"],
            original["issue_id"],
            original["material_review_id"],
        )
        == (STACK_ID, SYSTEM_ID, driver.URL, CONVERSATION, CASE, ISSUE, REVIEW),
        "LIVE_TAKEOVER_SOURCE_IDENTITY_CHANGED",
    )
    return original


def _model_fingerprint(stack):
    return stack.json_exec(
        "LIVE_TAKEOVER_MODEL_LEDGER_READONLY",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine=guarded_engine(load_settings('test'),test_only=True)
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()'))=={SYSTEM_ID!r}
    hashes={{table: db.scalar(text(
        "SELECT encode(sha256(convert_to(coalesce(string_agg(datum, ',' ORDER BY datum), ''), "
        "'UTF8')), 'hex') FROM (SELECT to_jsonb(r)::text AS datum FROM public."+table+" r) rows"
    )) for table in ('chat_model_attempts','agent_model_attempts','intake_model_audits')}}
print(json.dumps(hashes))
engine.dispose()
""",
    )


def _native_before(native, original):
    old = original["failure_native_readback"]
    (vision,) = native["vision"]
    (text,) = native["attempts"]
    (run,) = native["runs"]
    require(
        all(
            native[key] == old[key]
            for key in (
                "vision",
                "attempts",
                "observations",
                "runs",
                "reviews",
                "customer_messages",
            )
        )
        and native["counts"] == old["counts"]
        and native["counts"]["chat_model_attempts"] == native["counts"]["agent_model_attempts"] == 1
        and native["counts"]["agent_runs"] == native["counts"]["chat_decision_snapshots"] == 1
        and native["counts"]["intake_model_audits"] == 0
        and native["customer_messages"] == len(native["observations"]) == 2
        and not any(native["counts"][key] for key in ZERO_BEFORE)
        and vision["state"] == "SUCCEEDED"
        and vision["response_evidence"]["response"] == "SDK_RETURNED"
        and text["run_id"] == RUN
        and text["ordinal"] == 1
        and text["status"] == "FAILED"
        and text["error_code"] == "INVALID_MODEL_OUTPUT"
        and text["transport_evidence"]["response"] == "SDK_RETURNED"
        and (
            run["run_id"],
            run["snapshot_id"],
            run["run_status"],
            run["run_reason"],
            run["projection"],
        )
        == (RUN, SNAPSHOT, "NEEDS_REVIEW", FAILURE, "PUBLISHED")
        and native["reviews"] == [{"id": REVIEW, "materials_version": 2, "no_observations": True}],
        "LIVE_TAKEOVER_NATIVE_NOT_PRISTINE",
    )


def _review_body(snapshot, material_ids):
    candidate = snapshot["candidate"]
    options = candidate["options"]
    require(
        len(candidate["unresolved"]) == len(TECHNICAL)
        and set(candidate["unresolved"]) == set(TECHNICAL)
        and len(options) == 1
        and options[0]["action"] == "HUMAN_HANDOFF"
        and options[0]["target_line_ids"] == [LINE]
        and snapshot["checks"][options[0]["id"]]["status"] == "NON_EXECUTING"
        and len(set(material_ids)) == 2,
        "LIVE_TAKEOVER_NONTECHNICAL_UNRESOLVED",
    )
    return {
        "ref": _ref(snapshot),
        "expected_review_id": None,
        "review": {
            "action": "MODIFY",
            "reason": driver.CONTROL
            + "原调查失败，按已核对合成业务事实接管。仅在接管版本标记两条技术阻断已处置，原候选及待核对项保留。",
            "resolved_items": list(candidate["unresolved"]),
            "replacement": {
                "id": "control-support-takeover",
                "action": "REFUND",
                "lines": LINES,
                "evidence_ids": material_ids,
                "policy_ids": [POLICY],
                "rationale": driver.CONTROL
                + "合成订单行L01实付49元、无既有退款，当前模拟材料审核通过。",
            },
        },
    }


def _readiness(clients, original):
    customer, support = clients["customer"], clients["support"]
    current = read(support, PATH)
    snapshot = current["snapshot"]
    scope = snapshot["input"]
    require(
        current["issue_id"] == ISSUE
        and not current["expected_review_id"]
        and current["latest_review"] is None
        and current["customer_decision"] is None
        and _ref(snapshot) == _ref(read(support, PATH + "/history/" + SNAPSHOT))
        and snapshot["id"] == SNAPSHOT
        and snapshot["original_run_id"] == RUN
        and snapshot["origin"] == "AGENT"
        and datetime.fromisoformat(snapshot["expires_at"]) > datetime.now(UTC)
        and (
            scope["conversation_id"],
            scope["issue_id"],
            scope["case_id"],
            scope["order_id"],
            scope["line_ids"],
        )
        == (CONVERSATION, ISSUE, CASE, ORDER, [LINE])
        and read(customer, PATH + "/offer")["offer"] is None,
        "LIVE_TAKEOVER_CANDIDATE_SCOPE_CHANGED",
    )
    detail = read(support, CASE_PATH)
    eligibility = detail["eligibility"]
    order = read(customer, "orders/" + ORDER)
    material_ids = [row["material_id"] for row in original["materials"]]
    require(
        detail["case"]["id"] == CASE
        and detail["case"]["order_id"] == ORDER
        and detail["case"]["line_ids"] == [LINE]
        and eligibility["status"] == "ELIGIBLE"
        and "REFUND" in eligibility["actions"]
        and POLICY in eligibility["policy_ids"]
        and any(
            row["line_id"] == LINE and row["available_cents"] == CENTS
            for row in eligibility["lines"]
        )
        and order["id"] == ORDER
        and order["payment_status"] == "CONFIRMED"
        and order["paid_cents"] == CENTS
        and order["refunded_cents"] == 0
        and detail["chat_proposal"]["status"] == "NO_OFFER"
        and not any(detail[key] for key in ("ledger", "proposals", "approvals"))
        and {item["id"] for item in detail["materials"]} == set(material_ids)
        and len(detail["reviews"]) == 1
        and detail["reviews"][0]["id"] == REVIEW
        and detail["reviews"][0]["decision"] == "APPROVED"
        and not detail["reviews"][0].get("observations")
        and "CONTROL" in detail["reviews"][0]["notes"],
        "LIVE_TAKEOVER_BUSINESS_FACTS_CHANGED",
    )
    return snapshot, _review_body(snapshot, material_ids), detail["case"]["version"]


def _plan(resources):
    require(
        (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "LIVE_TAKEOVER_LOCAL_DOCKER_REQUIRED",
    )
    original = _source()
    state = private_json(driver.STATE)
    require(
        (state["run_id"], state["url"], state["profile"], state["status"])
        == (STACK_ID, driver.URL, "test", "READY"),
        "LIVE_TAKEOVER_STACK_CHANGED",
    )
    stack = offline_demo.restore(state)
    require(not (stack.directory / JOURNAL).exists(), "LIVE_TAKEOVER_ALREADY_ADMITTED")
    images = original["plan"]["target"]["images"]
    with patch.multiple(pg, PORT=driver.PORT, OBJECT_PORT=driver.OBJECT_PORT):
        topology = pg._topology(
            stack, {pg.BACKEND: images["backend"], pg.FRONTEND: images["frontend"]}
        )
    require(topology == (original["network"], SYSTEM_ID), "LIVE_TAKEOVER_TOPOLOGY_CHANGED")
    driver._owned_process(stack, "beat", "true true")
    driver._owned_process(stack, "worker", "true false")
    for phase in original["paid_phases"].values():
        require(
            phase["worker_removed"]
            and phase["temporary_key_removed"]
            and phase["offline_worker_restored"]
            and stack.owned("container", phase["worker"]) is None
            and not (Path(phase["private_stage"]) / "model-secret.json").exists(),
            "LIVE_TAKEOVER_PAID_WORKER_NOT_CLEAN",
        )
    clients = driver._actor_clients(resources, stack)
    native = driver._native(stack, original)
    _native_before(native, original)
    snapshot, body, case_version = _readiness(clients, original)
    plan = {
        "protocol": "chat-live-business-takeover-v1",
        "source_sha256": SOURCE_SHA,
        "stack_id": STACK_ID,
        "system_identifier": SYSTEM_ID,
        "url": driver.URL,
        "conversation_id": CONVERSATION,
        "case_id": CASE,
        "issue_id": ISSUE,
        "original_snapshot": _ref(snapshot),
        "original_snapshot_sha256": digest(snapshot),
        "run_id": RUN,
        "case_version": case_version,
        "control_review_sha256": digest(body),
        "model_ledger_fingerprint": _model_fingerprint(stack),
        "maximum_new_model_calls": 0,
        "maximum_simulated_executions": 1,
        "control_refund_cents": CENTS,
        "operator_provenance": "AUTOMATED_SYNTHETIC_CONTROL",
        "script_sha256": _hash(Path(__file__)),
        "driver_sha256": _hash(Path(driver.__file__)),
    }
    return plan, stack, clients, original, native, snapshot, body


def _execute(plan, stack, clients, original, before, snapshot, body, report, output):
    journal = stack.directory / JOURNAL
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"plan_sha256": digest(plan), "report": str(output)}, stream)
        stream.flush()
        os.fsync(stream.fileno())

    customer, support, approver = (clients[role] for role in ("customer", "support", "approver"))

    def post(stage, actor, path, expected, body):
        return driver._mutation(actor, report, output, stage, path, expected, body)

    updated, _ = post("CONTROL_SUPPORT_TAKEOVER", support, PATH + "/review", 200, body)
    require(
        updated["program_check"]["status"] == "PASSED"
        and updated["offer"]
        and updated["snapshot"]["id"] != SNAPSHOT
        and read(support, PATH + "/history/" + SNAPSHOT) == snapshot,
        "LIVE_TAKEOVER_SUPPORT_GATE_OR_HISTORY_CHANGED",
    )
    revised = read(support, PATH)["snapshot"]
    require(
        _ref(revised) == updated["snapshot"]
        and revised["origin"] == "SUPPORT_MODIFICATION"
        and revised["parent_id"] == SNAPSHOT
        and not revised["candidate"]["unresolved"],
        "LIVE_TAKEOVER_REVISED_SNAPSHOT_NOT_BOUND",
    )
    report.update(
        support_decision_id=updated["id"],
        support_snapshot=_ref(revised),
        original_unresolved_retained=list(snapshot["candidate"]["unresolved"]),
    )
    offer = read(customer, PATH + "/offer")["offer"]
    require(
        _ref(offer) == _ref(updated["offer"])
        and offer["action"] == "REFUND"
        and offer["lines"] == LINES
        and POLICY in offer["policy_ids"],
        "LIVE_TAKEOVER_PUBLIC_OFFER_CHANGED",
    )
    report["offer"] = _ref(offer)
    accepted, _ = post(
        "CONTROL_CUSTOMER_CONFIRM",
        customer,
        PATH + "/offer/respond",
        200,
        {
            "ref": _ref(offer),
            "response": {"action": "ACCEPT"},
        },
    )
    require(
        accepted["action"] == "ACCEPT" and accepted["offer"] == _ref(offer),
        "LIVE_TAKEOVER_CUSTOMER_CONFIRM_MISMATCH",
    )
    report["customer_decision_id"] = accepted["id"]
    detail = read(support, CASE_PATH)
    preview = detail["chat_proposal"]
    require(
        (preview["status"], preview["case_id"], preview["conversation_id"], preview["issue_id"])
        == ("READY", CASE, CONVERSATION, ISSUE)
        and _ref(preview["offer"]) == _ref(offer),
        "LIVE_TAKEOVER_PROPOSAL_PREVIEW_MISMATCH",
    )
    proposal, _ = post(
        "CONTROL_PROPOSAL",
        support,
        CASE_PATH + "/proposals",
        201,
        {
            "expected_version": detail["case"]["version"],
            "expected_offer": _ref(offer),
            "action": "REFUND",
            "lines": LINES,
            "policy_ids": offer["policy_ids"],
            "notes": driver.CONTROL + "已确认方案进入独立模拟审批。",
        },
    )
    report["proposal_id"] = proposal["id"]
    execute_path = CASE_PATH + "/execute"
    execute_body = {
        "expected_version": read(support, CASE_PATH)["case"]["version"],
        "proposal_id": proposal["id"],
    }
    blocked, _ = post("NO_APPROVAL_BLOCKED", support, execute_path, 409, execute_body)
    _code(blocked, "APPROVAL_REQUIRED")
    require(
        not read(support, CASE_PATH + "/executions")["items"], "LIVE_TAKEOVER_PREAPPROVAL_EXECUTION"
    )
    approval, _ = post(
        "CONTROL_INDEPENDENT_APPROVAL",
        approver,
        CASE_PATH + "/approvals",
        201,
        {
            "expected_version": read(approver, CASE_PATH)["case"]["version"],
            "proposal_id": proposal["id"],
            "decision": "APPROVED",
            "notes": driver.CONTROL + "独立审批账号仅批准一次模拟执行。",
        },
    )
    report["approval_id"] = approval["id"]
    execute_body["expected_version"] = read(support, CASE_PATH)["case"]["version"]
    result, key = post("SIMULATED_EXECUTION", support, execute_path, 200, execute_body)
    require(
        result["status"] == "SUCCEEDED"
        and result["receipt"]["mode"] == "SIMULATED"
        and result["amount_cents"] == CENTS,
        "LIVE_TAKEOVER_SIMULATED_RECEIPT_CHANGED",
    )
    report["execution_id"] = result["id"]
    driver._checkpoint(report, output, "SIMULATED_EXECUTION_REPLAY")
    same = call(support, "POST", execute_path, 200, headers=with_key(key), json=execute_body).json()
    require(same == result, "LIVE_TAKEOVER_EXECUTION_REPLAY_CHANGED")
    progress = _wait(
        customer,
        f"conversations/{CONVERSATION}",
        lambda data: (data.get("execution_progress") or {}).get("receipt_confirmed") is True,
        seconds=35,
    )["execution_progress"]
    ledger = read(support, CASE_PATH + "/executions")["items"]
    require(
        len(ledger) == 1
        and ledger[0]["id"] == result["id"]
        and progress["id"] == result["id"]
        and progress["case_id"] == CASE
        and progress["simulated"] is True
        and progress["status"] == "SUCCEEDED"
        and progress["amount_cents"] == CENTS
        and read(support, PATH + "/history/" + SNAPSHOT) == snapshot,
        "LIVE_TAKEOVER_RECEIPT_OR_ORIGINAL_CHANGED",
    )
    native = driver._native(stack, original)
    require(
        all(
            native[key] == before[key]
            for key in (
                "vision",
                "attempts",
                "observations",
                "runs",
                "reviews",
                "customer_messages",
            )
        )
        and _model_fingerprint(stack) == plan["model_ledger_fingerprint"]
        and native["counts"]
        == {
            **before["counts"],
            **dict.fromkeys(ZERO_BEFORE, 1),
            "chat_decision_snapshots": 2,
        }
        and _hash(SOURCE) == SOURCE_SHA,
        "LIVE_TAKEOVER_FINAL_NATIVE_SCOPE_CHANGED",
    )
    report.update(
        stage="VERIFIED",
        business_takeover="PASSED",
        model_quality="PARTIAL",
        agent_executable=False,
        support_control_modified=True,
        model_ledger_unchanged=True,
        new_model_calls=0,
        simulated_amount_cents=CENTS,
        replay_same_execution=True,
        customer_receipt_confirmed=True,
        final_native_counts=native["counts"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact --expected-plan-sha256 and new --report")
    with (
        offline_demo.lifecycle_lock(driver.STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", driver.STATE),
        ExitStack() as resources,
    ):
        plan, stack, clients, original, native, snapshot, body = _plan(resources)
        checksum = digest(plan)
        if not args.execute:
            print(json.dumps({"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}))
            return 0
        require(checksum == args.expected_plan_sha256, "LIVE_TAKEOVER_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "STARTING",
            "plan": plan,
            "plan_sha256": checksum,
            "model_quality": "PARTIAL",
            "model_candidate_status": "FAILED_FALLBACK_ONLY",
            "request_ids": {},
            "mutations": {},
        }
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _execute(plan, stack, clients, original, native, snapshot, body, report, args.report)
            report["status"] = "PASSED"
        except Exception as error:  # noqa: BLE001 - do not print provider, database or credential data
            code = str(error)
            pending = any(
                row["state"] == "DISPATCH_PENDING" for row in report["mutations"].values()
            )
            unknown = isinstance(error, httpx.TransportError) and (
                pending or report["stage"] == "SIMULATED_EXECUTION_REPLAY"
            )
            report.update(
                status="UNKNOWN" if unknown else "FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, ValueError, OSError, httpx.TransportError) as error:
        code = str(error)
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "LIVE_TAKEOVER_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
