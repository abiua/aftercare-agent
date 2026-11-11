"""Verify a bounded real-model path on an isolated synthetic after-sales stack."""

import argparse
import ipaddress
import json
import os
import re
import socket
from contextlib import ExitStack
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
import offline_demo
from aftercare.business.rules import digest
from aftercare.intelligence import models, vision
from verify_chat_backup_restore import private_json
from verify_chat_material_review_reassessment import _start_case
from verify_chat_p1_http import call, message, read, request_ids, with_key
from verify_chat_p2_offline import P2Stack, _wait, owned_container
from verify_chat_p3_offline import _code, _ref
from verify_chat_p5_multimodal_source import PINS, ROOT, _inputs
from verify_fresh_bootstrap import VerificationError, private_write, require
from verify_material_review_migration import _docker, _hash
from verify_streaming_compatibility import _configuration, _save

PORT, OBJECT_PORT = 5203, 58365
STATE = ROOT / ".runtime/offline-browser-demo-live-business.json"
WIRE_GATE = ROOT / "scripts/chat_live_business_wire_gate"
WORKER = ROOT / "scripts/live_business_worker.py"
WIRE_CAPS = {"VISION": (1, 50_000, 44_500, 900), "TEXT": (8, 400_000, 39_720, 1800)}
MAX_TEXT_CALLS = 8
MAX_TEXT_USD = Decimal("0.40")
MAX_VISION_USD = Decimal("0.05")
ISSUE_MICROUSD = 200_000
ISSUE_CALLS = 16
VISION_OUTPUT = 900
URL = f"http://127.0.0.1:{PORT}"
SUBNETS = {"net": "10.253.252.0/28", "ingress": "10.253.252.16/28"}
CONTROL = "CONTROL：自动化隔离演示，非真人审核或真实资金处置。"
ORDER, LINE, PRODUCT = "O9902", "L01", "DEV-VISION-MUG-01"
CONTROL_CENTS = 4900


def _micro(value):
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _budgets(spec, photos):
    text_output = models.P4_OUTPUT_RESERVE
    text_input = models.P4_INPUT_LIMIT
    photo_input, _ = models._estimate_input(vision._messages(photos), [], vision=True)
    vision_reserve = models._reserve_price(spec, spec.context_tokens, VISION_OUTPUT)
    text_reserve = models._reserve_price(spec, text_input, text_output)
    require(
        spec.alias == "economy"
        and spec.output_tokens == VISION_OUTPUT
        and spec.cache_write_ttl != "12h"
        and spec.cache_write_usd_per_million is not None
        and spec.context_tokens >= text_input + text_output
        and photo_input + VISION_OUTPUT < spec.context_tokens
        and vision_reserve is not None
        and vision_reserve > 0
        and text_reserve is not None
        and text_reserve > 0,
        "LIVE_BUSINESS_MODEL_SPEC_NOT_ADMITTED",
    )
    vision_micro, text_micro = _micro(vision_reserve), _micro(text_reserve)
    require(
        (vision_micro, text_micro) == (WIRE_CAPS["VISION"][2], WIRE_CAPS["TEXT"][2]),
        "LIVE_BUSINESS_WIRE_RESERVATION_CHANGED",
    )
    require(
        vision_micro <= _micro(MAX_VISION_USD)
        and text_micro * MAX_TEXT_CALLS <= _micro(MAX_TEXT_USD),
        "LIVE_BUSINESS_ESTIMATED_RESERVATION_CAP_EXCEEDED",
    )
    return {
        "provider": "openai",
        "model": spec.model,
        "source_model_spec_sha256": digest(spec.model_dump(mode="json")),
        "vision": {
            "maximum_posts": 1,
            "maximum_estimated_usd": str(MAX_VISION_USD),
            "reserved_microusd": vision_micro,
            "output_tokens": VISION_OUTPUT,
            "reserve_basis_input_tokens": spec.context_tokens,
        },
        "text": {
            "maximum_posts": MAX_TEXT_CALLS,
            "maximum_estimated_usd": str(MAX_TEXT_USD),
            "reserve_per_post_microusd": text_micro,
            "reserve_at_post_limit_microusd": text_micro * MAX_TEXT_CALLS,
            "input_tokens": text_input,
            "output_tokens": text_output,
        },
        "native_shared_issue": {
            "calls": ISSUE_CALLS,
            "microusd": ISSUE_MICROUSD,
            "upper_text_posts_if_vision_full_reserve_remains_booked": min(
                MAX_TEXT_CALLS, (ISSUE_MICROUSD - vision_micro) // text_micro
            ),
            "runtime_uses_remaining_native_ledger_headroom": True,
        },
        "automatic_transport_retries": 0,
        "unknown_delivery": "RECONCILE_ONLY_NO_REPLAY",
        "cost_basis": "CONSERVATIVE_REGISTRY_ESTIMATE_NOT_PROVIDER_BILL",
    }


def _image_pins(path):
    receipt = private_json(path)
    require(
        re.fullmatch(r"[0-9a-f]{40}", receipt["commit"])
        and receipt.get("runtime_updated") is False,
        "LIVE_BUSINESS_IMAGE_RECEIPT_INVALID",
    )
    images = {role: receipt[role] for role in ("backend", "frontend")}
    for image in images.values():
        require(
            re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "LIVE_BUSINESS_IMAGE_PIN_MISMATCH",
        )
    return {"declared_commit": receipt["commit"], "images": images, "receipt_sha256": _hash(path)}


def _network_pins():
    names = _docker("network", "ls", "-q").splitlines()
    used = []
    if names:
        for line in _docker(
            "network", "inspect", "--format", "{{json .IPAM.Config}}", *names
        ).splitlines():
            for entry in json.loads(line) or []:
                if entry.get("Subnet"):
                    used.append(ipaddress.ip_network(entry["Subnet"], strict=False))
    for configured in SUBNETS.values():
        candidate = ipaddress.ip_network(configured)
        require(not any(candidate.overlaps(row) for row in used), "LIVE_BUSINESS_SUBNET_IN_USE")
    return SUBNETS


def _preflight(image_receipt, *, control_modify=False):
    require(not STATE.exists(), "LIVE_BUSINESS_STATE_ALREADY_EXISTS")
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "LIVE_BUSINESS_LOCAL_DOCKER_REQUIRED",
    )
    for port in (PORT, OBJECT_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    seed, assets, _, photos = _inputs()
    budget = _budgets(_configuration()["gpt-6.1-terra"], photos)
    return {
        "protocol": "chat-live-business-path-plan-v1",
        "target": {
            "profile": "test",
            "port": PORT,
            "object_port": OBJECT_PORT,
            "state": str(STATE),
            "subnets": _network_pins(),
            **_image_pins(image_receipt),
        },
        "protected_state": "5202_ORIGINAL_WAITING_FOR_INDEPENDENT_REVIEW",
        "control_modify": control_modify,
        "control_modification": {
            "order": ORDER,
            "line": LINE,
            "action": "REFUND",
            "amount_cents": CONTROL_CENTS,
            "required_policy": "POL01",
            "unresolved_model_items_are_not_cleared": True,
        }
        if control_modify
        else None,
        "fixture": {
            "provenance": "SYNTHETIC_CUP_AND_PARCEL",
            "order": "O9902",
            "customer": "C001",
            "product": "DEV-VISION-MUG-01",
            "seed_sha256": digest(seed),
            "seed_and_manifest_pins": PINS,
            "original_sha256s": [row["sha256"] for row in assets],
            "sent_jpeg_sha256s": [photo.sent_sha256 for photo in photos],
            "vision_transform_version": vision.TRANSFORM_VERSION,
        },
        "budget": budget,
        "helpers": {
            name: _hash(ROOT / "scripts" / name)
            for name in (
                "offline_demo.py",
                "setup_runtime.py",
                "verify_fresh_bootstrap.py",
                "verify_chat_p1_http.py",
                "verify_chat_p2_offline.py",
                "verify_chat_p3_offline.py",
                "verify_chat_p5_multimodal_source.py",
                "verify_chat_p5_pg.py",
                "verify_chat_material_review_reassessment.py",
                "verify_chat_backup_restore.py",
                "verify_material_review_migration.py",
                "verify_streaming_compatibility.py",
            )
        },
        "gates": {
            "gate_script_sha256": _hash(WIRE_GATE / "sitecustomize.py"),
            "gate_policy_sha256": _hash(WIRE_GATE / "policy.py"),
            "worker_script_sha256": _hash(WORKER),
            "script_sha256": _hash(Path(__file__)),
        },
        "phases": [
            {"stage": "PUBLIC_CHAT", "path": "CLIENT_TEXT_THEN_TWO_ORIGINALS_IN_ONE_MESSAGE"},
            {
                "stage": "P2",
                "path": "NATIVE_CELERY_EXACT_TWO_IMAGE_GATE",
                "success": "ONE_ATTEMPT_TWO_PUBLISHED_ORIGINAL_BOUND_OBSERVATIONS",
            },
            {
                "stage": "SUPPORT_MATERIAL",
                "path": "SUPPORT_NORMAL_HTTP_CONTROL",
                "success": "CONFIRMED_ITEM_AND_PARCEL_THEN_REVIEW_WITHOUT_STRUCTURED_HUMAN_LABELS",
            },
            {
                "stage": "P3",
                "path": "NATIVE_CELERY_V7_AUTO_SINGLE_ISSUE",
                "success": "REAL_TEXT_ATTEMPT_AND_NATIVE_ORIGINAL_CANDIDATE",
            },
            {"stage": "CONTROL_HITL", "path": "SUPPORT_ACCEPT_OR_MODIFY_WITH_PROGRAM_PASS"},
            {"stage": "CONTROL_CUSTOMER", "path": "CUSTOMER_CONFIRMS_EXACT_OFFER_VERSION"},
            {"stage": "CONTROL_APPROVAL", "path": "SEPARATE_APPROVER_ACCOUNT"},
            {
                "stage": "SIMULATED_EXECUTION",
                "path": "NO_APPROVAL_BLOCKED_THEN_IDEMPOTENT_SUPPORT_EXECUTE_WITH_CUSTOMER_RECEIPT",
            },
        ],
    }


def _checkpoint(report, output, stage):
    report["stage"] = stage
    _save(output, report)


def _mutation(client, report, output, stage, path, expected, body):
    ids = request_ids(report, stage, message_id=False)
    report["mutations"][stage] = {
        "path": path,
        "expected_http": expected,
        "body_sha256": digest(body),
        "state": "DISPATCH_PENDING",
    }
    _checkpoint(report, output, stage)
    response = call(
        client, "POST", path, expected, headers=with_key(ids["idempotency_key"]), json=body
    )
    report["mutations"][stage].update(state="HTTP_RETURNED", http_status=response.status_code)
    _checkpoint(report, output, stage)
    return response.json(), ids["idempotency_key"]


def _actor_clients(resources, stack):
    entries = private_json(stack.credentials_path)
    clients = {}
    users = {}
    for role in ("customer", "support", "approver"):
        (entry,) = [
            row
            for row in entries
            if row["role"] == role and (role != "customer" or row["customer_id"] == "C001")
        ]
        client = resources.enter_context(
            httpx.Client(
                base_url=URL + "/api/",
                trust_env=False,
                follow_redirects=False,
                timeout=12,
                headers={"Authorization": f"Bearer {entry['token']}"},
            )
        )
        actor = read(client, "me")
        require(
            actor["role"] == role
            and actor["user_id"] == entry["user_id"]
            and (role != "customer" or actor["customer_id"] == "C001"),
            "LIVE_BUSINESS_ACTOR_MISMATCH",
        )
        clients[role], users[role] = client, actor["user_id"]
    require(len(set(users.values())) == 3, "LIVE_BUSINESS_SEPARATE_ROLES_REQUIRED")
    return clients


def _review_choice(snapshot, eligible, materials, control_modify):
    options = snapshot["candidate"]["options"]
    unresolved = snapshot["candidate"]["unresolved"]
    for option in options:
        lines = option.get("lines", [])
        if (
            not unresolved
            and option["action"] == "REFUND"
            and len(lines) == 1
            and lines[0]["line_id"] == LINE
            and type(lines[0].get("amount_cents")) is int
            and 0 < lines[0]["amount_cents"] <= CONTROL_CENTS
            and snapshot["checks"][option["id"]]["status"] == "PASSED"
        ):
            return {
                "action": "ACCEPT",
                "option_id": option["id"],
                "reason": CONTROL + "原候选可执行，核对后采用其原订单行和金额。",
            }
    if (
        not control_modify
        or unresolved
        or eligible["status"] != "ELIGIBLE"
        or "REFUND" not in eligible["actions"]
        or "POL01" not in eligible["policy_ids"]
        or not any(
            row["line_id"] == LINE and row["available_cents"] == CONTROL_CENTS
            for row in eligible["lines"]
        )
    ):
        return None
    return {
        "action": "MODIFY",
        "reason": CONTROL + "使用已核对的固定合成订单行测试独立审批及模拟执行。",
        "replacement": {
            "id": "control-refund",
            "action": "REFUND",
            "lines": [{"line_id": LINE, "amount_cents": CONTROL_CENTS}],
            "evidence_ids": materials,
            "policy_ids": ["POL01"],
            "rationale": CONTROL + "当前合成订单行实付49元且未退款。",
        },
    }


def _complete_business(clients, case, conversation, snapshot, report, output, *, control_modify):
    support, customer, approver = (clients[role] for role in ("support", "customer", "approver"))
    case_path = f"cases/{case}"
    decisions = f"conversations/{conversation}/decisions"
    detail = read(support, case_path)
    require(
        detail["case"]["line_ids"] == [LINE]
        and detail["case"]["order_id"] == ORDER
        and detail["eligibility"]["status"] == "ELIGIBLE"
        and not any(detail[key] for key in ("proposals", "approvals", "ledger")),
        "LIVE_BUSINESS_REVIEW_INPUT_NOT_ELIGIBLE",
    )
    materials = [row["material_id"] for row in report["materials"]]
    review = _review_choice(snapshot, detail["eligibility"], materials, control_modify)
    report["agent_executable"] = bool(review and review["action"] == "ACCEPT")
    report["support_control_modified"] = False
    if review is None:
        _checkpoint(report, output, "MODEL_CANDIDATE_NO_SAFE_EXECUTABLE_CONTROL_PATH")
        return False

    def post(stage, actor, path, status, body):
        return _mutation(clients[actor], report, output, stage, path, status, body)

    reviewed, _ = post(
        "CONTROL_SUPPORT_REVIEW",
        "support",
        decisions + "/review",
        200,
        {
            "ref": _ref(snapshot),
            "expected_review_id": None,
            "review": review,
        },
    )
    require(
        reviewed["program_check"]["status"] == "PASSED"
        and reviewed["offer"]
        and read(support, decisions + "/history/" + snapshot["id"]) == snapshot,
        "LIVE_BUSINESS_PROGRAM_GATE_OR_ORIGINAL_CHANGED",
    )
    changed = review["action"] == "MODIFY"
    require(
        (reviewed["snapshot"]["id"] != snapshot["id"]) == changed,
        "LIVE_BUSINESS_REVIEW_SNAPSHOT_BINDING",
    )
    report["support_control_modified"] = changed
    report["support_decision_id"] = reviewed["id"]
    report["support_snapshot"] = _ref(reviewed["snapshot"])
    offer = read(customer, decisions + "/offer")["offer"]
    require(
        _ref(offer) == _ref(reviewed["offer"])
        and offer["action"] == "REFUND"
        and len(offer["lines"]) == 1
        and offer["lines"][0]["line_id"] == LINE
        and 0 < offer["lines"][0]["amount_cents"] <= CONTROL_CENTS,
        "LIVE_BUSINESS_CUSTOMER_OFFER_SCOPE_CHANGED",
    )
    report["offer"] = _ref(offer)
    consent, _ = post(
        "CONTROL_CUSTOMER_CONFIRM",
        "customer",
        decisions + "/offer/respond",
        200,
        {
            "ref": _ref(offer),
            "response": {"action": "ACCEPT"},
        },
    )
    require(consent["action"] == "ACCEPT", "LIVE_BUSINESS_CUSTOMER_CONFIRM_MISSING")
    report["customer_decision_id"] = consent["id"]
    managed = read(support, case_path)
    preview = managed["chat_proposal"]
    require(
        preview["status"] == "READY"
        and preview["case_id"] == case
        and preview["conversation_id"] == conversation
        and preview["issue_id"] == report["issue_id"]
        and _ref(preview["offer"]) == _ref(offer),
        "LIVE_BUSINESS_HANDOFF_PREVIEW_MISMATCH",
    )
    proposal, _ = post(
        "CONTROL_SUBMIT_PROPOSAL",
        "support",
        case_path + "/proposals",
        201,
        {
            "expected_version": managed["case"]["version"],
            "expected_offer": _ref(offer),
            "action": offer["action"],
            "lines": offer["lines"],
            "policy_ids": offer["policy_ids"],
            "notes": CONTROL + "按照客户控制确认的同一方案提交。",
        },
    )
    report["proposal_id"] = proposal["id"]
    exec_path = case_path + "/execute"
    payload = {
        "expected_version": read(support, case_path)["case"]["version"],
        "proposal_id": proposal["id"],
    }
    blocked, _ = post("NO_APPROVAL_BLOCKED", "support", exec_path, 409, payload)
    _code(blocked, "APPROVAL_REQUIRED")
    require(
        not read(support, case_path + "/executions")["items"],
        "LIVE_BUSINESS_PRE_APPROVAL_EXECUTION",
    )
    approval, _ = post(
        "CONTROL_INDEPENDENT_APPROVAL",
        "approver",
        case_path + "/approvals",
        201,
        {
            "expected_version": read(approver, case_path)["case"]["version"],
            "proposal_id": proposal["id"],
            "decision": "APPROVED",
            "notes": CONTROL + "独立审批账号批准一次模拟执行。",
        },
    )
    report["approval_id"] = approval["id"]
    payload["expected_version"] = read(support, case_path)["case"]["version"]
    executed, key = post("SIMULATED_EXECUTION", "support", exec_path, 200, payload)
    require(
        executed["status"] == "SUCCEEDED"
        and executed["receipt"]["mode"] == "SIMULATED"
        and executed["amount_cents"] == offer["lines"][0]["amount_cents"],
        "LIVE_BUSINESS_SIMULATED_RECEIPT_MISSING",
    )
    report["execution_id"] = executed["id"]
    _checkpoint(report, output, "SIMULATED_EXECUTION_REPLAY")
    replay = call(support, "POST", exec_path, 200, headers=with_key(key), json=payload).json()
    require(replay == executed, "LIVE_BUSINESS_EXECUTION_REPLAY_CHANGED")
    ledger = read(support, case_path + "/executions")["items"]
    progress = _wait(
        customer,
        f"conversations/{conversation}",
        lambda data: (data.get("execution_progress") or {}).get("receipt_confirmed") is True,
        seconds=35,
    )["execution_progress"]
    require(
        len(ledger) == 1
        and ledger[0]["id"] == executed["id"]
        and progress["id"] == executed["id"]
        and progress["case_id"] == case
        and progress["simulated"] is True
        and progress["status"] == "SUCCEEDED"
        and progress["amount_cents"] == executed["amount_cents"]
        and read(support, decisions + "/history/" + snapshot["id"]) == snapshot,
        "LIVE_BUSINESS_FINAL_RECEIPT_BINDING_MISMATCH",
    )
    report["simulated_execution"] = {
        "id": executed["id"],
        "amount_cents": executed["amount_cents"],
        "replay_same_key": True,
        "customer_receipt_confirmed": True,
        "ledger_count": 1,
    }
    return True


def _native(stack, report):
    conversation = str(UUID(report["conversation_id"]))
    case = str(UUID(report["case_id"]))
    return stack.json_exec(
        "LIVE_BUSINESS_NATIVE_READBACK",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
settings=load_settings('test')
assert settings.container and settings.chat_analysis_enabled and settings.chat_decisions_enabled
assert settings.chat_investigation_protocol=='compact-v7'
assert not any((settings.model_enabled,settings.chat_vision_enabled,settings.embedding_enabled))
engine=guarded_engine(settings,test_only=True)
P={{'conversation':{conversation!r},'case':{case!r}}}
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    def many(sql): return [dict(row) for row in db.execute(text(sql),P).mappings()]
    def scalar(sql): return db.scalar(text(sql),P)
    assert scalar('SELECT system_identifier::text FROM pg_control_system()')=={report["system_identifier"]!r}
    counts={{name:scalar('SELECT count(*) FROM public.'+name) for name in (
        'chat_conversations','chat_model_attempts','chat_observations','agent_runs','agent_model_attempts',
        'intake_model_audits','business_material_reviews','chat_decision_snapshots','chat_support_decisions',
        'chat_customer_decisions','business_proposals','business_approvals','business_executions','business_simulated_payments')}}
    turns=many("SELECT id,state,reason,quiet_until::text,deadline::text,deadline<=now() AS expired, "
               "quiet_until<=now() AS due, attachments FROM chat_analysis_turns WHERE conversation_id=:conversation ORDER BY generation")
    vision=many("SELECT id,turn_id,state,reserved_microusd,estimated_cost_microusd,budget_violation,response_evidence "
                "FROM chat_model_attempts WHERE conversation_id=:conversation")
    observations=many("SELECT id,turn_id,attempt_id,attachment_id,original_sha256,sent_sha256,transform_version "
                      "FROM chat_observations WHERE turn_id IN (SELECT id FROM chat_analysis_turns WHERE conversation_id=:conversation)")
    runs=many("SELECT q.turn_id,q.run_id,q.state,q.reason,r.status AS run_status,r.reason AS run_reason,"
              "p.state AS projection,s.id AS snapshot_id "
              "FROM chat_investigation_requests q LEFT JOIN agent_runs r ON r.id=q.run_id "
              "LEFT JOIN chat_decision_projections p ON p.run_id=q.run_id "
              "LEFT JOIN chat_decision_snapshots s ON s.id=p.snapshot_id WHERE q.conversation_id=:conversation")
    attempts=many("SELECT a.run_id,a.logical_id,a.ordinal,a.status,a.error_code,a.estimated_cost_usd,"
                  "a.metadata_json->'transport_evidence' AS transport_evidence "
                  "FROM agent_model_attempts a JOIN agent_runs r ON r.id=a.run_id WHERE r.case_id=:case "
                  "ORDER BY a.started_at,a.logical_id,a.ordinal")
    validated=many("SELECT e.run_id,e.payload->'selected_option_ids' AS option_ids "
                   "FROM agent_run_events e JOIN agent_runs r ON r.id=e.run_id "
                   "WHERE r.case_id=:case AND e.kind='investigation_result'")
    reviews=many("SELECT id,materials_version,observations IS NULL AS no_observations "
                 "FROM business_material_reviews WHERE case_id=:case")
    messages=scalar("SELECT count(*) FROM chat_messages WHERE conversation_id=:conversation AND role='customer'")
print(json.dumps(dict(counts=counts,turns=turns,vision=vision,observations=observations,runs=runs,
                      attempts=attempts,reviews=reviews,customer_messages=messages,validated_result_events=validated)))
engine.dispose()
""",
    )


def _wait_native(stack, report, predicate, *, seconds=90):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        data = _native(stack, report)
        if predicate(data):
            return data
        sleep(1)
    raise VerificationError("LIVE_BUSINESS_NATIVE_OUTCOME_TIMEOUT")


def _dispatch(stack, task, target, phase, report, output):
    from uuid import uuid4

    task_id = str(uuid4())
    report["queue"][phase] = {
        "task": task,
        "id": task_id,
        "target": target,
        "state": "PUBLISH_PENDING",
    }
    _checkpoint(report, output, "QUEUE_" + phase)
    result = stack.json_exec(
        "LIVE_BUSINESS_CELERY_PUBLISH",
        owned_container(stack, "api"),
        f"""
import json
from aftercare.queue import make_queue
app=make_queue('test')
result=app.send_task({task!r}, args=[{target!r}], task_id={task_id!r})
print(json.dumps(dict(id=result.id)))
""",
    )
    require(result["id"] == task_id, "LIVE_BUSINESS_CELERY_TASK_ID_CHANGED")
    report["queue"][phase]["state"] = "PUBLISHED"
    _checkpoint(report, output, "QUEUE_" + phase)


def _phase_registry(spec, plan, phase, photos):
    spec_data = spec.model_dump(mode="json")
    budget = plan["budget"][phase.lower()]
    if phase == "VISION":
        prompt_tokens, _ = models._estimate_input(vision._messages(photos), [], vision=True)
        spec_data.update(
            vision=True,
            vision_checked_at="2026-11-11",
            vision_compatibility_version="p2-exact-two-images-v1",
            vision_input_tokens_per_image=(spec.context_tokens - VISION_OUTPUT - prompt_tokens)
            // 2,
            data_scopes=["customer_attachment"],
        )
    else:
        spec_data.update(
            alias="analysis",
            data_scopes=["customer_conversation"],
            vision=False,
            vision_checked_at=None,
            vision_compatibility_version=None,
            vision_input_tokens_per_image=None,
            output_tokens=budget["output_tokens"],
        )
    cap = (
        str(Decimal(budget["reserved_microusd"]) / 1_000_000)
        if phase == "VISION"
        else str(MAX_TEXT_USD)
    )
    return models.Registry.model_validate(
        {
            "version": "p5-live-business-" + phase.lower() + "-v1",
            "models": [spec_data],
            "allowed_providers": ["openai"],
            "native_streaming": False,
            "max_attempts": budget["maximum_posts"],
            "max_run_attempts": budget["maximum_posts"],
            "max_usd": cap,
            "max_run_usd": cap,
        }
    ).model_dump(mode="json")


def _owned_process(stack, role, expected):
    name = owned_container(stack, role)
    actual = stack.docker(
        "LIVE_BUSINESS_PROCESS_STATE",
        "container",
        "inspect",
        "--format",
        "{{.State.Running}} {{.State.Paused}}",
        name,
        log_output=False,
    ).stdout.strip()
    require(actual == expected, "LIVE_BUSINESS_PROCESS_STATE_CHANGED")
    return name


def _wait_worker(stack, name):
    deadline = monotonic() + 30
    while monotonic() < deadline:
        reply = stack.json_exec(
            "LIVE_BUSINESS_CELERY_WORKER_STATS",
            owned_container(stack, "api"),
            """
import json
from aftercare.queue import make_queue
rows=make_queue('test').control.inspect(timeout=2).ping() or {}
print(json.dumps(dict(workers=list(rows))))
""",
        )
        if len(reply["workers"]) == 1:
            container_id = stack.docker(
                "LIVE_BUSINESS_WORKER_HOSTNAME",
                "inspect",
                "--format",
                "{{.Config.Hostname}}",
                name,
                log_output=False,
            ).stdout.strip()
            if reply["workers"][0].endswith("@" + container_id):
                return reply["workers"][0]
        sleep(1)
    raise VerificationError("LIVE_BUSINESS_CELERY_EXCLUSIVE_WORKER_NOT_READY")


def _wait_worker_idle(stack, worker):
    deadline = monotonic() + 18
    while monotonic() < deadline:
        snapshot = stack.json_exec(
            "LIVE_BUSINESS_CELERY_DRAIN_READBACK",
            owned_container(stack, "api"),
            """
import json
from aftercare.queue import make_queue
inspect=make_queue('test').control.inspect(timeout=2)
print(json.dumps(dict(active=inspect.active() or {}, reserved=inspect.reserved() or {})))
""",
        )
        if all(value == {worker: []} for value in snapshot.values()):
            return
        sleep(1)
    raise VerificationError("LIVE_BUSINESS_WORKER_DRAIN_UNVERIFIED")


def _wire_receipt(stage):
    admissions = [private_json(p) for p in sorted(stage.glob("admission-*.json"))]
    replies = [private_json(p) for p in sorted(stage.glob("response-*.json"))]
    return {
        "admissions": admissions,
        "responses": replies,
        "delivery_unknown": bool(
            {row["index"] for row in admissions} - {row["index"] for row in replies}
        ),
    }


def _record_paid_cleanup(stage, key_path, phase_log, *, removed):
    phase_log["worker_removed"] = removed
    phase_log["offline_worker_restored"] = False
    phase_log["temporary_key_removed"] = False
    try:
        key_path.unlink(missing_ok=True)
        phase_log["temporary_key_removed"] = not key_path.exists()
    except OSError as error:
        phase_log["temporary_key_cleanup_error"] = type(error).__name__
    try:
        phase_log["wire"] = _wire_receipt(stage)
    except (OSError, ValueError, TypeError, KeyError, VerificationError) as error:
        phase_log["wire_receipt_read_error"] = type(error).__name__


def _paid_phase(stack, state, plan, report, output, phase, turn, photos, *, project=False):
    _owned_process(stack, "beat", "true true")
    offline = _owned_process(stack, "worker", "true false")
    stage = stack.directory / ("live-business-" + phase.lower())
    stage.mkdir(mode=0o700)
    name = f"{stack.prefix}-live-{phase.lower()}"
    require(stack.owned("container", name) is None, "LIVE_BUSINESS_PAID_WORKER_ALREADY_EXISTS")
    count, cap, reserve, tokens = WIRE_CAPS[phase]
    spec = _configuration()["gpt-6.1-terra"]
    require(
        digest(spec.model_dump(mode="json")) == plan["budget"]["source_model_spec_sha256"],
        "LIVE_BUSINESS_MODEL_SPEC_CHANGED",
    )
    registry = _phase_registry(spec, plan, phase, photos)
    protocol = plan.get("wire_protocol", "p5-live-business-wire-v1")
    require(
        protocol in {"p5-live-business-wire-v1", "p5-live-business-wire-v2"},
        "LIVE_BUSINESS_WIRE_PROTOCOL_UNSUPPORTED",
    )
    gate_plan = {
        "protocol": protocol,
        "kind": phase,
        "campaign_id": stack.run_id,
        "conversation_id": report["conversation_id"],
        "case_id": report["case_id"],
        "issue_id": report["issue_id"],
        "turn_id": turn,
        "model": spec.model,
        "output_tokens": tokens,
        "maximum_provider_posts": count,
        "maximum_reservation_microusd": cap,
        "per_post_reserved_microusd": reserve,
        "system_identifier": report["system_identifier"],
        "registry_sha256": digest(registry),
        "worker_script_sha256": plan["gates"]["worker_script_sha256"],
        "gate_script_sha256": plan["gates"]["gate_script_sha256"],
    }
    if protocol == "p5-live-business-wire-v2":
        gate_plan.update(case_scope=plan["case_scope"], expected_route=plan["expected_route"])
    if phase == "VISION" or protocol == "p5-live-business-wire-v2":
        gate_plan.update(
            original_sha256s=plan["fixture"]["original_sha256s"],
            sent_sha256s=plan["fixture"]["sent_jpeg_sha256s"],
        )
    staged_gate = stage / "gate"
    staged_gate.mkdir(mode=0o700)
    for path, target, key in (
        (WIRE_GATE / "sitecustomize.py", staged_gate / "sitecustomize.py", "gate_script_sha256"),
        (WIRE_GATE / "policy.py", staged_gate / "policy.py", "gate_policy_sha256"),
        (WORKER, stage / "live_business_worker.py", "worker_script_sha256"),
    ):
        private_write(target, path.read_text())
        require(_hash(target) == plan["gates"][key], "LIVE_BUSINESS_WORKER_CODE_CHANGED")
    private_write(stage / "plan.json", json.dumps(gate_plan))
    private_write(stage / "registry.json", json.dumps(registry))
    private_write(stage / "lock", "")
    worker_plan_sha = _hash(stage / "plan.json")
    key_path = stage / "model-secret.json"
    phase_log = report["paid_phases"][phase] = {
        "worker": name,
        "worker_plan_sha256": worker_plan_sha,
        "turn_id": turn,
        "private_stage": str(stage),
    }
    _checkpoint(report, output, "PREPARING_" + phase)
    require(
        stack.owned("network", report["network"]) is True
        and stack.owned("network", report["ingress"]) is True,
        "LIVE_BUSINESS_NETWORK_OWNERSHIP_CHANGED",
    )
    removed = False
    settled = False
    try:
        key = private_json(ROOT / ".runtime/model-secret.json")["api_key"]
        require(isinstance(key, str) and bool(key.strip()), "LIVE_BUSINESS_PROVIDER_KEY_MISSING")
        stack.secrets.append(key)
        private_write(key_path, json.dumps({"api_key": key}))
        stack.docker("LIVE_BUSINESS_STOP_OFFLINE_WORKER", "stop", offline, log_output=False)
        _owned_process(stack, "worker", "false false")
        stack.resources.append(("container", name))
        state["resources"] = stack.resources
        offline_demo.save(state)
        blank_env = tuple(
            value
            for variable in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "LANGSMITH_TRACING",
                "LANGCHAIN_TRACING_V2",
            )
            for value in ("--env", variable + "=")
        )
        stack.docker(
            "LIVE_BUSINESS_START_PAID_WORKER",
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            stack.label,
            "--network",
            report["network"],
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--env",
            "PYTHONPATH=/live-business-gate:/workspace/scripts",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            f"P5_LIVE_BUSINESS_PLAN_SHA256={worker_plan_sha}",
            *blank_env,
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={stage},target=/live-business-wire",
            "--mount",
            f"type=bind,source={staged_gate},target=/live-business-gate,readonly",
            "--mount",
            f"type=bind,source={stage / 'live_business_worker.py'},target=/workspace/scripts/live_business_worker.py,readonly",
            "--entrypoint",
            "python",
            plan["target"]["images"]["backend"],
            "/workspace/scripts/live_business_worker.py",
            log_output=False,
        )
        require(stack.owned("container", name) is True, "LIVE_BUSINESS_PAID_WORKER_UNOWNED")
        stack.docker(
            "LIVE_BUSINESS_ENABLE_OWNED_EGRESS",
            "network",
            "connect",
            report["ingress"],
            name,
            log_output=False,
        )
        phase_log["celery_worker"] = _wait_worker(stack, name)
        native_task = (
            "aftercare.chat_analysis" if phase == "VISION" else "aftercare.chat_investigation_start"
        )
        _dispatch(stack, native_task, turn, phase, report, output)
        if phase == "VISION":
            native = _wait_native(
                stack,
                report,
                lambda row: row["turns"][-1]["state"] in {"PUBLISHED", "FAILED", "STALE"},
            )
        else:
            native = _wait_native(
                stack,
                report,
                lambda row: (
                    bool(row["runs"])
                    and (
                        row["runs"][-1]["run_status"]
                        in {"COMPLETED", "FAILED", "NEEDS_REVIEW", "WAITING_INPUT", "CANCELED"}
                        or row["runs"][-1]["state"] in {"BLOCKED", "SUPERSEDED", "CANCELED"}
                    )
                ),
                seconds=200,
            )
            latest = native["runs"][-1]
            if (
                project
                and latest["run_id"]
                and latest["projection"] not in {"PUBLISHED", "BLOCKED"}
            ):
                _dispatch(
                    stack,
                    "aftercare.project_chat_decision",
                    latest["run_id"],
                    "PROJECT",
                    report,
                    output,
                )
                native = _wait_native(
                    stack,
                    report,
                    lambda row: row["runs"][-1]["projection"] in {"PUBLISHED", "BLOCKED"},
                    seconds=25,
                )
        phase_log["native_dispatch"] = private_json(stage / "native-dispatch.json")
        require(
            phase_log["native_dispatch"] == {"task": native_task, "turn_id": turn},
            "LIVE_BUSINESS_CELERY_DISPATCH_MISMATCH",
        )
        _wait_worker_idle(stack, phase_log["celery_worker"])
        settled = True
        return native
    finally:
        try:
            owned = stack.owned("container", name)
            require(owned is not False, "LIVE_BUSINESS_PAID_WORKER_UNOWNED")
            if owned:
                stack.docker("LIVE_BUSINESS_REMOVE_PAID_WORKER", "rm", "-f", name, log_output=False)
            removed = stack.owned("container", name) is None
            require(removed, "LIVE_BUSINESS_PAID_WORKER_REMAINS")
        finally:
            _record_paid_cleanup(stage, key_path, phase_log, removed=removed)
            _checkpoint(report, output, report["stage"])
        if removed and ("container", name) in stack.resources:
            stack.resources.remove(("container", name))
            offline_demo.save(state)
        require(
            phase_log["temporary_key_removed"], "LIVE_BUSINESS_TEMPORARY_KEY_CLEANUP_UNVERIFIED"
        )
        require("wire" in phase_log, "LIVE_BUSINESS_WIRE_RECEIPT_UNVERIFIED")
        if (
            removed
            and (settled or phase not in report["queue"])
            and not phase_log["wire"]["delivery_unknown"]
        ):
            stack.docker("LIVE_BUSINESS_RESTORE_OFFLINE_WORKER", "start", offline, log_output=False)
            _owned_process(stack, "worker", "true false")
            phase_log["offline_worker_restored"] = True
        else:
            phase_log["offline_worker_restored"] = False
        _checkpoint(report, output, report["stage"])


def _assert_wire(report, phase):
    cleanup = report["paid_phases"][phase]
    require(
        cleanup["worker_removed"]
        and cleanup["temporary_key_removed"]
        and cleanup["offline_worker_restored"],
        "LIVE_BUSINESS_PAID_WORKER_CLEANUP_UNVERIFIED",
    )
    ledger = cleanup["wire"]
    admissions, responses = ledger["admissions"], ledger["responses"]
    count, cap, _, _ = WIRE_CAPS[phase]
    require(
        0 < len(admissions) == len(responses) <= count
        and not ledger["delivery_unknown"]
        and sum(row["wire_reserved_microusd"] for row in admissions) <= cap
        and all(
            admission["index"] == response["index"]
            and admission["request_sha256"] == response["request_sha256"]
            and response["http_status"] == 200
            and 0 < admission["reserved_microusd"] <= admission["wire_reserved_microusd"]
            and admission["kind"] == phase
            and all(
                admission[key] == report[key] for key in ("conversation_id", "case_id", "issue_id")
            )
            and admission["turn_id"] == report["paid_phases"][phase]["turn_id"]
            for admission, response in zip(admissions, responses, strict=True)
        ),
        "LIVE_BUSINESS_WIRE_READBACK_MISMATCH",
    )
    return admissions


def _wait_turn(stack, report):
    native = _wait_native(
        stack,
        report,
        lambda row: row["turns"] and (row["turns"][-1]["due"] or row["turns"][-1]["expired"]),
        seconds=25,
    )
    turn = native["turns"][-1]
    require(turn["state"] == "PENDING" and not turn["expired"], "LIVE_BUSINESS_TURN_NOT_ADMISSIBLE")
    return turn


def _boot(plan, report, output, seed):
    class LiveStack(P2Stack):
        def __init__(self, directory):
            super().__init__(directory)
            config = self.runtime / "local.json"
            values = private_json(config)
            values["app"].update(
                chat_analysis_enabled=True,
                chat_decisions_enabled=True,
                chat_investigation_enabled=False,
                chat_vision_enabled=False,
                model_enabled=False,
                embedding_enabled=False,
                chat_investigation_protocol="compact-v7",
                chat_investigation_issue_max_calls=ISSUE_CALLS,
                chat_investigation_issue_max_microusd=ISSUE_MICROUSD,
            )
            staged = config.with_suffix(".live-business")
            private_write(staged, json.dumps(values))
            staged.replace(config)
            self.seed = self.directory / "live-business-seed.json"
            private_write(self.seed, json.dumps(seed))

        def create(self, kind, suffix, *args):
            if kind == "network":
                args = (*args, "--subnet", plan["target"]["subnets"][suffix])
            return super().create(kind, suffix, *args)

        def run(self, image, suffix, network, options=(), command=()):
            if suffix == "init":
                options = (
                    *options,
                    "--mount",
                    f"type=bind,source={self.seed},target=/workspace/data/v0.1/agent/business.json,readonly",
                )
            return super().run(image, suffix, network, options, command)

    import verify_chat_p5_pg as pg

    require(digest(seed) == plan["fixture"]["seed_sha256"], "LIVE_BUSINESS_SEED_CHANGED")
    require(not STATE.exists(), "LIVE_BUSINESS_STATE_ALREADY_EXISTS")
    _checkpoint(report, output, "BOOT_OWNED_STACK")
    images = plan["target"]["images"]
    with patch.object(offline_demo, "FreshStack", LiveStack):
        offline_demo.start(
            SimpleNamespace(
                port=PORT,
                object_port=OBJECT_PORT,
                profile="test",
                backend_image=images["backend"],
                frontend_image=images["frontend"],
            )
        )
    state = private_json(STATE)
    stack = offline_demo.restore(state)
    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        network, identity = pg._topology(
            stack, {pg.BACKEND: images["backend"], pg.FRONTEND: images["frontend"]}
        )
    ingress = f"{stack.prefix}-ingress"
    report.update(
        owned_stack=stack.run_id,
        owned_url=URL,
        network=network,
        ingress=ingress,
        system_identifier=identity,
        offline_state_path=str(STATE),
    )
    _checkpoint(report, output, "PAUSE_OWNED_BEAT")
    beat = _owned_process(stack, "beat", "true false")
    stack.docker("LIVE_BUSINESS_PAUSE_BEAT", "pause", beat, log_output=False)
    _owned_process(stack, "beat", "true true")
    report["owned_beat_paused"] = True
    return stack, state


def _public_chat(clients, report, output, assets, originals):
    customer, support = clients["customer"], clients["support"]
    require(read(customer, "runtime")["mode"] == "offline", "LIVE_BUSINESS_API_NOT_OFFLINE")
    order = read(customer, "orders/" + ORDER)
    require(
        order["id"] == ORDER
        and order["status"] == "DELIVERED"
        and order["payment_status"] == "CONFIRMED"
        and order["paid_cents"] == CONTROL_CENTS
        and order["refunded_cents"] == 0
        and len(order["items"]) == 1
        and order["items"][0]["line_id"] == LINE
        and order["items"][0]["product_id"] == PRODUCT
        and order["items"][0]["quantity"] == 1,
        "LIVE_BUSINESS_SYNTHETIC_ORDER_SCOPE_CHANGED",
    )
    conversation, case = _start_case(
        customer,
        {"id": ORDER, "line_id": LINE},
        report,
        lambda stage: _checkpoint(report, output, stage),
    )
    ids = request_ids(report, "two_originals")
    _checkpoint(report, output, "CUSTOMER_TWO_ORIGINALS")
    data = message(
        customer,
        conversation,
        "",
        ids,
        [(asset["file"], originals[asset["id"]]) for asset in assets],
    )
    (uploaded,) = [row for row in data["messages"] if row["role"] == "customer"]
    attachments = uploaded["attachments"]
    require(
        len(attachments) == 2 and data["conversation"]["active_case_id"] == case,
        "LIVE_BUSINESS_CHAT_ATTACHMENTS_UNBOUND",
    )
    for asset, attachment in zip(assets, attachments, strict=True):
        fetched = call(customer, "GET", attachment["content_path"].removeprefix("/api/"), 200)
        require(
            attachment["sha256"] == asset["sha256"] and fetched.content == originals[asset["id"]],
            "LIVE_BUSINESS_CHAT_ORIGINAL_READBACK_CHANGED",
        )
    material_page = read(support, f"conversations/{conversation}/decision-materials")
    require(
        material_page["case_id"] == case
        and {row["attachment_id"] for row in material_page["items"]}
        == {row["id"] for row in attachments},
        "LIVE_BUSINESS_MATERIAL_SCOPE_CHANGED",
    )
    report["issue_id"] = str(UUID(material_page["issue_id"]))
    report["attachment_ids"] = [row["id"] for row in attachments]
    _checkpoint(report, output, "PUBLIC_ORIGINALS_VERIFIED")
    return conversation, case, attachments


def _verify_vision(native, report, plan):
    (wire,) = _assert_wire(report, "VISION")
    (attempt,) = native["vision"]
    expected = dict(
        zip(
            report["attachment_ids"],
            zip(
                plan["fixture"]["original_sha256s"],
                plan["fixture"]["sent_jpeg_sha256s"],
                strict=True,
            ),
            strict=True,
        )
    )
    require(
        native["turns"][-1]["state"] == "PUBLISHED"
        and native["counts"]["chat_model_attempts"] == 1
        and native["counts"]["chat_observations"] == 2
        and native["customer_messages"] == 2
        and not any(
            native["counts"][key]
            for key in (
                "agent_runs",
                "agent_model_attempts",
                "intake_model_audits",
                "business_executions",
            )
        )
        and attempt["turn_id"] == report["paid_phases"]["VISION"]["turn_id"]
        and attempt["state"] == "SUCCEEDED"
        and not attempt["budget_violation"]
        and 0 < attempt["estimated_cost_microusd"] <= plan["budget"]["vision"]["reserved_microusd"]
        and attempt["response_evidence"]["path"] == "DEFAULT_SYNC"
        and attempt["response_evidence"]["response"] == "SDK_RETURNED"
        and bool(attempt["response_evidence"].get("validated_result_sha256"))
        and wire["original_sha256s"] == plan["fixture"]["original_sha256s"]
        and wire["sent_sha256s"] == plan["fixture"]["sent_jpeg_sha256s"]
        and {
            row["attachment_id"]: (row["original_sha256"], row["sent_sha256"])
            for row in native["observations"]
        }
        == expected
        and all(
            row["attempt_id"] == attempt["id"]
            and row["turn_id"] == attempt["turn_id"]
            and row["transform_version"] == plan["fixture"]["vision_transform_version"]
            for row in native["observations"]
        ),
        "LIVE_BUSINESS_VISION_NATIVE_BINDING_CHANGED",
    )
    report["vision_attempt"] = attempt
    report["observations"] = native["observations"]


def _enable_investigation(stack):
    _owned_process(stack, "beat", "true true")
    config = stack.runtime / "local.json"
    values = private_json(config)
    require(values["test"]["chat_investigation_enabled"] is False, "LIVE_BUSINESS_RUNTIME_CHANGED")
    values["test"]["chat_investigation_enabled"] = True
    temporary = config.with_suffix(".text-stage")
    private_write(temporary, json.dumps(values))
    temporary.replace(config)
    api, worker = (_owned_process(stack, role, "true false") for role in ("api", "worker"))
    for name in (api, worker):
        stack.docker(
            "LIVE_BUSINESS_RESTART_OWNED_OFFLINE_SERVICE", "restart", name, log_output=False
        )
    stack.wait(
        "LIVE_BUSINESS_TEXT_API_READY",
        "exec",
        api,
        "python",
        "-c",
        "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/readyz',timeout=4).close()",
    )
    _wait_worker(stack, worker)


def _material_control(
    stack, support, report, output, case, conversation, attachments, assets, originals
):
    path = f"conversations/{conversation}/decision-materials"
    report["materials"] = []
    for attachment, asset, kind in zip(attachments, assets, ("ITEM", "PARCEL"), strict=True):
        page = read(support, path)
        require(
            page["case_id"] == case and page["issue_id"] == report["issue_id"],
            "LIVE_BUSINESS_MATERIAL_SCOPE_CHANGED",
        )
        (entry,) = [row for row in page["items"] if row["attachment_id"] == attachment["id"]]
        require(
            entry["sha256"] == asset["sha256"] and not entry["material_id"],
            "LIVE_BUSINESS_MATERIAL_ORIGINAL_CHANGED",
        )
        linked, _ = _mutation(
            support,
            report,
            output,
            "CONTROL_LINK_" + kind,
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
        material, source = linked["material"], linked["source"]
        fetched = call(support, "GET", f"cases/{case}/materials/{material['id']}/content", 200)
        require(
            source["issue_id"] == report["issue_id"]
            and source["attachment_id"] == attachment["id"]
            and source["message_id"] == entry["message_id"]
            and source["sha256"] == material["sha256"] == asset["sha256"]
            and material["kind"] == kind
            and fetched.content == originals[asset["id"]],
            "LIVE_BUSINESS_MATERIAL_LINK_READBACK_CHANGED",
        )
        report["materials"].append({**source, "material_id": material["id"], "kind": kind})
    before = read(support, f"cases/{case}")
    require(not before["reviews"], "LIVE_BUSINESS_PRIOR_REVIEW_UNEXPECTED")
    review, _ = _mutation(
        support,
        report,
        output,
        "CONTROL_MATERIAL_REVIEW",
        f"cases/{case}/material-reviews",
        201,
        {
            "expected_version": before["case"]["version"],
            "decision": "APPROVED",
            "notes": CONTROL + "两张合成素材用于测试流程，不作真人图片判断。",
        },
    )
    report["material_review_id"] = review["id"]
    native = _native(stack, report)
    require(
        native["customer_messages"] == 2
        and native["counts"]["chat_model_attempts"] == 1
        and len(native["reviews"]) == 1
        and native["reviews"][0]["id"] == review["id"]
        and native["reviews"][0]["no_observations"]
        and not native["counts"]["agent_runs"],
        "LIVE_BUSINESS_CONTROL_REVIEW_NOT_BOUND",
    )
    turn = _wait_turn(stack, report)
    require(turn["reason"] == "MATERIAL_REVIEW_CHANGED", "LIVE_BUSINESS_REVIEW_TURN_NOT_NATIVE")
    _dispatch(stack, "aftercare.chat_analysis", turn["id"], "REVIEW_ANALYSIS", report, output)
    ready = _wait_native(
        stack, report, lambda row: row["turns"][-1]["state"] == "PUBLISHED", seconds=35
    )
    require(
        ready["turns"][-1]["id"] == turn["id"]
        and ready["customer_messages"] == 2
        and ready["vision"] == native["vision"]
        and ready["observations"] == native["observations"]
        and not ready["counts"]["agent_runs"],
        "LIVE_BUSINESS_REVIEW_ANALYSIS_CHANGED_EVIDENCE",
    )
    return turn["id"]


def _verify_text(native, report):
    wire = _assert_wire(report, "TEXT")
    estimates = [
        Decimal(row["estimated_cost_usd"])
        for row in native["attempts"]
        if row["estimated_cost_usd"] is not None
    ]
    (run,) = native["runs"]
    attempts = native["attempts"]
    require(
        len(attempts) == len(wire) == len(estimates)
        and 0 < sum(estimates) <= MAX_TEXT_USD
        and _micro(sum(estimates)) + report["vision_attempt"]["estimated_cost_microusd"]
        <= ISSUE_MICROUSD
        and native["counts"]["agent_runs"] == 1
        and native["counts"]["agent_model_attempts"] == len(wire)
        and run["turn_id"] == report["paid_phases"]["TEXT"]["turn_id"]
        and run["projection"] == "PUBLISHED"
        and run["snapshot_id"]
        and {(row["run_id"], row["native_logical_id"], row["native_ordinal"]) for row in wire}
        == {(row["run_id"], row["logical_id"], row["ordinal"]) for row in attempts}
        and {row["run_id"] for row in wire} == {run["run_id"]}
        and all(row["status"] in {"SUCCEEDED", "FAILED"} for row in attempts)
        and all(
            (row["transport_evidence"] or {}).get("path") == "DEFAULT_SYNC"
            and (row["transport_evidence"] or {}).get("response") == "SDK_RETURNED"
            for row in attempts
        ),
        "LIVE_BUSINESS_TEXT_NATIVE_BINDING_CHANGED",
    )
    report["run"] = run
    report["text_attempts"] = attempts
    return run


def _model_candidate(native, snapshot):
    selected = [option["id"] for option in snapshot["candidate"]["options"]]
    return bool(selected) and any(
        event["run_id"] == snapshot["original_run_id"] and event["option_ids"] == selected
        for event in native["validated_result_events"]
    )


def _business_status(*, model_candidate, completed, agent_executable):
    return "PASSED" if model_candidate and completed and agent_executable else "PARTIAL"


def _execute(plan, report, output):
    require(
        all(
            _hash(ROOT / "scripts" / name) == expected for name, expected in plan["helpers"].items()
        ),
        "LIVE_BUSINESS_HELPER_CHANGED",
    )
    seed, assets, originals, photos = _inputs()
    stack, state = _boot(plan, report, output, seed)
    try:
        with ExitStack() as resources:
            clients = _actor_clients(resources, stack)
            conversation, case, attachments = _public_chat(
                clients, report, output, assets, originals
            )
            pending = _wait_turn(stack, report)
            require(
                [row["id"] for row in pending["attachments"]] == report["attachment_ids"],
                "LIVE_BUSINESS_VISION_TURN_ATTACHMENTS_CHANGED",
            )
            vision_native = _paid_phase(
                stack, state, plan, report, output, "VISION", pending["id"], photos
            )
            _verify_vision(vision_native, report, plan)
            _checkpoint(report, output, "ENABLE_TEXT_OWNED_OFFLINE_CONFIG")
            _enable_investigation(stack)
            turn = _material_control(
                stack,
                clients["support"],
                report,
                output,
                case,
                conversation,
                attachments,
                assets,
                originals,
            )
            text_native = _paid_phase(
                stack, state, plan, report, output, "TEXT", turn, photos, project=True
            )
            run = _verify_text(text_native, report)
            current = read(clients["support"], f"conversations/{conversation}/decisions")
            snapshot = current["snapshot"]
            require(
                current["issue_id"] == report["issue_id"]
                and snapshot["id"] == run["snapshot_id"]
                and snapshot["origin"] == "AGENT"
                and snapshot["original_run_id"] == run["run_id"]
                and snapshot["input"]["case_id"] == case
                and snapshot["input"]["line_ids"] == [LINE]
                and snapshot["input"]["order_id"] == ORDER
                and {
                    row["id"]
                    for row in snapshot["input"]["frozen_context"]["reviews"]
                    if row["effective"]
                }
                == {report["material_review_id"]},
                "LIVE_BUSINESS_ORIGINAL_CANDIDATE_UNBOUND",
            )
            report["model_validated_candidate_path"] = _model_candidate(text_native, snapshot)
            report["original_candidate"] = _ref(snapshot)
            report["original_candidate_actions"] = [
                row["action"] for row in snapshot["candidate"]["options"]
            ]
            _checkpoint(report, output, "REAL_TEXT_CANDIDATE_VERIFIED")
            if not report["model_validated_candidate_path"]:
                report["business_completion"] = "NOT_RUN_MODEL_FALLBACK"
                return "PARTIAL"
            completed = _complete_business(
                clients,
                case,
                conversation,
                snapshot,
                report,
                output,
                control_modify=plan["control_modify"],
            )
            final = _native(stack, report)
            report["final_native_counts"] = final["counts"]
            require(
                final["customer_messages"] == 2
                and final["vision"] == vision_native["vision"]
                and final["observations"] == vision_native["observations"]
                and final["attempts"] == text_native["attempts"]
                and all(
                    final["counts"][key] == int(completed)
                    for key in (
                        "business_proposals",
                        "business_approvals",
                        "business_executions",
                        "business_simulated_payments",
                    )
                ),
                "LIVE_BUSINESS_FINAL_NATIVE_READBACK_CHANGED",
            )
            report["business_completion"] = "SIMULATED_RECEIPT_VERIFIED" if completed else "NOT_RUN"
            return _business_status(
                model_candidate=report["model_validated_candidate_path"],
                completed=completed,
                agent_executable=report["agent_executable"],
            )
    except Exception:
        if report.get("case_id") and report.get("conversation_id"):
            try:
                report["failure_native_readback"] = _native(stack, report)
            except Exception as error:  # noqa: BLE001 - do not mask the original failure
                report["failure_readback_error"] = type(error).__name__
        raise


def _unknown(report, error):
    pending = any(row["state"] == "DISPATCH_PENDING" for row in report["mutations"].values())
    queue = any(row["state"] == "PUBLISH_PENDING" for row in report["queue"].values())
    wire = any(
        row.get("wire", {}).get("delivery_unknown") for row in report["paid_phases"].values()
    )
    unverified_paid_cleanup = any(
        phase in report["queue"]
        and (
            row.get("worker_removed") is not True
            or "wire" not in row
            or bool(row.get("wire_receipt_read_error"))
        )
        for phase, row in report["paid_phases"].items()
    )
    native = report.get("failure_native_readback", {})
    active = any(
        row["status"] in {"STARTED", "UNKNOWN"} for row in native.get("attempts", [])
    ) or any(row["state"] in {"STARTED", "UNKNOWN"} for row in native.get("vision", []))
    waiting = str(error) in {
        "LIVE_BUSINESS_NATIVE_OUTCOME_TIMEOUT",
        "LIVE_BUSINESS_WORKER_DRAIN_UNVERIFIED",
    } and bool(report["queue"])
    # A verified HTTP response with an unexpected status is retained as a failure,
    # while an unanswered mutating request or broker publish is never retried.
    early_or_replay = report["stage"] in {
        "CREATE_CONVERSATION",
        "CONFIRM_CONTEXT",
        "CUSTOMER_TEXT",
        "CUSTOMER_TWO_ORIGINALS",
        "SIMULATED_EXECUTION_REPLAY",
    }
    http_unknown = isinstance(error, httpx.TransportError) and (pending or early_or_replay)
    uncertain = bool(
        report["paid_phases"] and report.get("failure_readback_error") and report["queue"]
    )
    return bool(
        queue or wire or unverified_paid_cleanup or active or waiting or http_unknown or uncertain
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-receipt", required=True, type=Path)
    parser.add_argument("--control-modify", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires an exact --expected-plan-sha256 and a new --report")
    with (
        offline_demo.lifecycle_lock(STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", STATE),
    ):
        plan = _preflight(args.image_receipt, control_modify=args.control_modify)
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
                )
            )
            return 0
        require(checksum == args.expected_plan_sha256, "LIVE_BUSINESS_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "STARTING",
            "plan": plan,
            "plan_sha256": checksum,
            "review_provenance": "AUTOMATED_SYNTHETIC_CONTROL",
            "request_ids": {},
            "mutations": {},
            "queue": {},
            "paid_phases": {},
        }
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            report["status"] = _execute(plan, report, args.report)
        except Exception as error:  # noqa: BLE001 - preserve safe outcome without printing raw provider errors
            code = str(error)
            report.update(
                status="UNKNOWN" if _unknown(report, error) else "FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "LIVE_BUSINESS_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
