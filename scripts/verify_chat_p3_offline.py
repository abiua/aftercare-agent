"""P3 end-to-end acceptance on the isolated, model-disabled localhost 5193 stack."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import offline_demo
from aftercare.business.rules import digest
from verify_chat_p1_http import call, png, read, request_ids, with_key
from verify_chat_p2_offline import P2Stack, _begin, _wait, owned_container
from verify_fresh_bootstrap import ROOT, VerificationError, require
from verify_streaming_compatibility import _private_json, _save

STATE = ROOT / ".runtime" / "offline-browser-demo-p3.json"
PORT, OBJECT_PORT = 5193, 58355
BACKEND, FRONTEND = "aftercare-backend:chat-p3-20261111", "aftercare-frontend:chat-p3-20261111"
IMAGES = {BACKEND: "b394df52ca6d", FRONTEND: "8ce79786eebb"}


class P3Stack(P2Stack):
    def __init__(self, directory):
        super().__init__(directory)
        target = self.runtime / "local.json"
        config = _private_json(target)
        config["app"]["chat_decisions_enabled"] = True
        target.write_text(json.dumps(config))


def _preflight():
    require(not STATE.exists(), "DEDICATED_P3_STACK_ALREADY_EXISTS")
    for port in (PORT, OBJECT_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            try:
                connection.bind(("127.0.0.1", port))
            except OSError:
                raise VerificationError(f"ISOLATED_PORT_{port}_UNAVAILABLE") from None
    context = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    require(
        context.returncode == 0
        and context.stdout.strip().startswith("unix://")
        and (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")),
        "LOCAL_DOCKER_REQUIRED",
    )
    actual = {}
    for image, expected in IMAGES.items():
        checked = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        image_id = checked.stdout.strip()
        require(
            checked.returncode == 0 and image_id.startswith("sha256:" + expected),
            "PINNED_IMAGE_MISMATCH",
        )
        actual[image] = image_id
    return {
        "scope": "P3_ISOLATED_REAL_HTTP_PG_S3_CELERY_OFFLINE_GRAPH_SIMULATED_FINANCE",
        "images": actual,
        "app_port": PORT,
        "object_port": OBJECT_PORT,
        "model_enabled": False,
        "vision_enabled": False,
        "chat_analysis_enabled": True,
        "chat_decisions_enabled": True,
        "scenario_count": 2,
        "maximum_simulated_executions": 1,
    }


def _key(report, name):
    return with_key(request_ids(report, name, message_id=False)["idempotency_key"])


def _json(client, path, status, headers, body):
    return call(client, "POST", path, status, headers=headers, json=body).json()


def _code(payload, expected):
    require(payload["detail"]["code"] == expected, "EXPECTED_BUSINESS_ERROR_MISMATCH")


def _ref(payload):
    return {key: payload[key] for key in ("id", "version", "sha256")}


def _case(client, case_id):
    return read(client, f"cases/{case_id}")


def _version(client, case_id):
    return _case(client, case_id)["case"]["version"]


def _materials(owner, support, report, scenario, case_id):
    ids = []
    for kind, color in (("ITEM", (20, 40, 200)), ("PARCEL", (200, 120, 20))):
        data = png(color)
        response = call(
            owner,
            "POST",
            f"cases/{case_id}/materials",
            201,
            headers=_key(report, f"{scenario}_{kind}"),
            data={"kind": kind, "expected_version": str(_version(owner, case_id))},
            files={"file": (f"synthetic-{kind}.png", data, "image/png")},
        ).json()
        ids.append(response["id"])
        fetched = call(owner, "GET", f"cases/{case_id}/materials/{response['id']}/content", 200)
        require(
            fetched.content == data and response["sha256"] == hashlib.sha256(data).hexdigest(),
            "SIMULATED_MATERIAL_S3_READBACK_FAILED",
        )
    reviewed = _json(
        support,
        f"cases/{case_id}/material-reviews",
        201,
        _key(report, scenario + "_material_review"),
        {
            "expected_version": _version(support, case_id),
            "decision": "APPROVED",
            "notes": "隔离验收合成附件。此处仅登记模拟材料审核，不代表财务批准。",
        },
    )
    require(reviewed["decision"] == "APPROVED", "SIMULATED_MATERIAL_REVIEW_REQUIRED")
    return ids


def _scenario(clients, order, report, name, *, withdraw):
    customer, support, approver = (clients[role] for role in ("customer", "support", "approver"))
    result = {"stage": "link_case", "order_id": order["id"]}
    report["scenarios"][name] = result
    ident, case_id, _ = _begin(
        customer,
        report,
        name + "_conversation",
        order,
        f"订单 {order['id']} 的商品已经破损，请协助核对。",
    )
    result.update(conversation_id=ident, case_id=case_id, stage="durable_candidate")
    url = f"conversations/{ident}/decisions"
    run_key = _key(report, name + "_start")
    started = _json(support, url + "/run", 202, run_key, {})
    run_id = result["run_id"] = started["run_id"]
    current = _wait(support, url, lambda data: bool(data.get("snapshot")), seconds=65)
    original = current["snapshot"]
    result["original_snapshot_id"] = original["id"]
    require(
        original["origin"] == "AGENT"
        and original["original_run_id"] == run_id
        and original["candidate"]["route_version"] == "rules-only"
        and original["candidate"]["options"][0]["action"] == "REQUEST_EVIDENCE",
        "EXPECTED_PERSISTED_OFFLINE_CANDIDATE",
    )
    require(
        _json(support, url + "/run", 202, run_key, {})["run_id"] == run_id, "RUN_RETRY_CHANGED_ID"
    )
    require(
        read(support, f"cases/{case_id}/runs/{run_id}")["status"] == "WAITING_INPUT",
        "OFFLINE_GRAPH_GATE_NOT_OBSERVED",
    )

    result["stage"] = "material_review_and_offer"
    evidence = _materials(customer, support, report, name, case_id)
    case = _case(support, case_id)
    amount = case["eligibility"]["available_cents"]
    line = order["items"][0]["line_id"]
    require(
        type(amount) is int and amount > 0 and case["eligibility"]["status"] == "ELIGIBLE",
        "ELIGIBLE_SIMULATED_AMOUNT_REQUIRED",
    )
    proposal_path = f"cases/{case_id}/proposals"
    proposal = {
        "expected_version": case["case"]["version"],
        "action": "REFUND",
        "notes": "隔离验收客服核对现行业务规则及金额。",
        "lines": [{"line_id": line, "amount_cents": amount}],
        "policy_ids": [],
    }
    _code(
        _json(support, proposal_path, 409, _key(report, name + "_old_api_before_offer"), proposal),
        "DECISION_CUSTOMER_CONSENT_REQUIRED",
    )
    reviewed = _json(
        support,
        url + "/review",
        200,
        _key(report, name + "_support_modify"),
        {
            "ref": _ref(original),
            "expected_review_id": None,
            "review": {
                "action": "MODIFY",
                "reason": "按模拟材料审核结果重新核对整笔金额。",
                "replacement": {
                    "id": "verified-refund",
                    "action": "REFUND",
                    "lines": proposal["lines"],
                    "evidence_ids": evidence,
                    "policy_ids": ["POL01"],
                    "rationale": "已核对订单行全额。",
                },
                "new_evidence_ids": evidence,
            },
        },
    )
    require(
        reviewed["program_check"]["status"] == "PASSED" and reviewed["offer"],
        "PROGRAM_CHECK_OR_OFFER_MISSING",
    )
    result["support_snapshot_id"] = reviewed["snapshot"]["id"]
    require(
        read(support, url + "/history/" + original["id"]) == original
        and reviewed["snapshot"]["id"] != original["id"],
        "ORIGINAL_CANDIDATE_CHANGED",
    )
    offer = read(customer, url + "/offer")["offer"]
    require(
        _ref(offer) == _ref(reviewed["offer"]) and offer["lines"] == proposal["lines"],
        "CUSTOMER_OFFER_BINDING_MISMATCH",
    )
    result["offer_id"] = offer["id"]
    _code(
        _json(
            support, proposal_path, 409, _key(report, name + "_old_api_before_consent"), proposal
        ),
        "DECISION_CUSTOMER_CONSENT_REQUIRED",
    )

    result["stage"] = "customer_and_finance"
    accept_key = _key(report, name + "_customer_accept")
    accept = {"ref": _ref(offer), "response": {"action": "ACCEPT"}}
    accepted = _json(customer, url + "/offer/respond", 200, accept_key, accept)
    require(
        _json(customer, url + "/offer/respond", 200, accept_key, accept) == accepted,
        "CUSTOMER_ACCEPT_REPLAY_MISMATCH",
    )
    result["customer_decision_id"] = accepted["id"]
    submitted = _json(support, proposal_path, 201, _key(report, name + "_proposal"), proposal)
    result["proposal_id"] = submitted["id"]
    execution_path = f"cases/{case_id}/execute"
    body = {"expected_version": _version(support, case_id), "proposal_id": submitted["id"]}
    _code(
        _json(support, execution_path, 409, _key(report, name + "_execute_without_finance"), body),
        "APPROVAL_REQUIRED",
    )
    approval = _json(
        approver,
        f"cases/{case_id}/approvals",
        201,
        _key(report, name + "_finance"),
        {
            "expected_version": _version(approver, case_id),
            "proposal_id": submitted["id"],
            "decision": "APPROVED",
            "notes": "隔离模拟财务独立审批。",
        },
    )
    result["approval_id"] = approval["id"]
    body["expected_version"] = _version(support, case_id)

    if withdraw:
        result["stage"] = "withdrawal"
        withdrew = _json(
            customer,
            url + "/offer/respond",
            200,
            _key(report, name + "_customer_withdraw"),
            {
                "ref": _ref(offer),
                "response": {"action": "REQUEST_CHANGE", "reason": "先停止，我希望换货。"},
            },
        )
        require(withdrew["id"] != accepted["id"], "WITHDRAWAL_OVERWROTE_CONSENT")
        result["withdrawal_id"] = withdrew["id"]
        _code(
            _json(support, execution_path, 409, _key(report, name + "_blocked_execution"), body),
            "DECISION_CUSTOMER_CONSENT_REQUIRED",
        )
        require(
            not read(support, f"cases/{case_id}/executions")["items"], "WITHDRAWN_OFFER_EXECUTED"
        )
    else:
        result["stage"] = "simulated_execution"
        key = _key(report, name + "_execute")
        done = _json(support, execution_path, 200, key, body)
        require(
            done["status"] == "SUCCEEDED"
            and done["receipt"]["mode"] == "SIMULATED"
            and done["amount_cents"] == amount,
            "EXPECTED_SIMULATED_RECEIPT_MISSING",
        )
        require(
            _json(support, execution_path, 200, key, body) == done, "EXECUTION_IDEMPOTENCY_MISMATCH"
        )
        result.update(
            execution_id=done["id"], estimated_model_cost_usd="0", simulated_amount_cents=amount
        )
    result["stage"] = "verified"


def _database_audit(stack, api, report):
    run_ids = [str(UUID(row["run_id"])) for row in report["scenarios"].values()]
    return stack.json_exec(
        "P3_AUDIT_READONLY",
        api,
        f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine = guarded_engine(load_settings('app'))
with engine.connect() as conn, conn.begin():
    conn.execute(text('SET TRANSACTION READ ONLY'))
    counts = {{table: conn.scalar(text('SELECT count(*) FROM ' + table)) for table in (
        'agent_model_attempts', 'chat_model_attempts', 'intake_model_audits', 'business_executions',
        'chat_decision_snapshots', 'chat_support_decisions', 'chat_customer_decisions')}}
    projected = conn.scalar(text("SELECT count(*) FROM chat_decision_projections WHERE state='PUBLISHED' AND run_id = ANY(:ids)"), {{'ids': {run_ids!r}}})
    print(json.dumps({{'counts': counts, 'projected_runs': projected}}))
engine.dispose()
""",
    )


def _execute(report):
    offline_demo.STATE = STATE
    offline_demo.FreshStack = P3Stack
    offline_demo.start(
        SimpleNamespace(
            port=PORT, object_port=OBJECT_PORT, backend_image=BACKEND, frontend_image=FRONTEND
        )
    )
    state = _private_json(STATE)
    stack = offline_demo.restore(state)
    api = owned_container(stack, "api")
    report["isolated_url"] = state["url"]
    require(state["url"] == f"http://127.0.0.1:{PORT}", "P3_LOOPBACK_REQUIRED")
    config = _private_json(stack.runtime / "local.json")["app"]
    require(
        config["chat_analysis_enabled"]
        and config["chat_decisions_enabled"]
        and not config["chat_vision_enabled"]
        and not config["model_enabled"],
        "P3_OFFLINE_SETTINGS_REQUIRED",
    )
    credentials = {item["role"]: item for item in _private_json(stack.credentials_path)}
    with ExitStack() as resources:
        clients = {}
        for role in ("customer", "support", "approver"):
            clients[role] = resources.enter_context(
                httpx.Client(
                    base_url=f"http://127.0.0.1:{PORT}/api/",
                    trust_env=False,
                    timeout=10,
                    headers={"Authorization": f"Bearer {credentials[role]['token']}"},
                )
            )
            require(read(clients[role], "me")["role"] == role, "P3_ROLE_AUTH_REQUIRED")
        require(
            read(clients["customer"], "runtime")["mode"] == "offline", "P3_OFFLINE_RUNTIME_REQUIRED"
        )
        eligible = [
            row
            for row in read(clients["customer"], "orders")["items"]
            if row["status"] == "DELIVERED"
            and row["paid_at"]
            and row["delivered_at"]
            and len(row["items"]) == 1
            and row["paid_cents"] > 0
            and row["refunded_cents"] == 0
        ]
        require(len(eligible) >= 2, "TWO_OWN_SIMULATED_ORDERS_REQUIRED")
        for order, scenario, withdraw in zip(
            eligible[:2], ("complete", "withdrawal"), (False, True), strict=True
        ):
            _scenario(clients, order, report, scenario, withdraw=withdraw)
    audit = _database_audit(stack, api, report)
    report["database_audit"] = audit
    require(
        audit["projected_runs"] == 2
        and audit["counts"]["business_executions"] == 1
        and all(
            audit["counts"][table] == 0
            for table in (
                "agent_model_attempts",
                "chat_model_attempts",
                "intake_model_audits",
            )
        ),
        "P3_DURABLE_COUNT_MISMATCH",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires an approved plan digest and a new report path")
    plan = _preflight()
    checksum = digest(plan)
    if not args.execute:
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
            )
        )
        return 0
    require(checksum == args.expected_plan_sha256, "P3_PLAN_CHANGED")
    report = {
        "status": "IN_PROGRESS",
        "plan_sha256": checksum,
        "plan": plan,
        "request_ids": {},
        "scenarios": {},
    }
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _execute(report)
        report["status"] = "PASSED"
    except VerificationError as error:
        report.update(status="FAILED", failure_code=str(error))
    except httpx.TimeoutException:
        report.update(status="FAILED", failure_code="HTTP_TIMEOUT_OUTCOME_UNKNOWN")
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.TimeoutExpired):
        report.update(status="FAILED", failure_code="P3_ISOLATED_EXECUTION_FAILED")
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, OSError, ValueError, subprocess.TimeoutExpired):
        print("P3_ACCEPTANCE_BLOCKED")
        raise SystemExit(1) from None
