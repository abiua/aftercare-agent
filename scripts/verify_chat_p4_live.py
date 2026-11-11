"""Prepare or explicitly execute one bounded synthetic P4 text compatibility run."""

import argparse
import hashlib
import json
import os
import subprocess
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from shutil import copyfile
from unittest.mock import patch
from uuid import UUID

import httpx
import offline_demo
import verify_chat_p3_offline as p3
import verify_chat_p4_path as p4
from aftercare.intelligence import models
from sqlalchemy.engine import make_url
from verify_chat_p1_http import read
from verify_chat_p2_offline import _wait, owned_container
from verify_chat_p2_vision_worker import EMBEDDING, REVISION, WORKER, _one_file, _worker_options
from verify_fresh_bootstrap import LABEL, ROOT, VerificationError, require
from verify_streaming_compatibility import ProbeError, _configuration, _private_json, _save

GATE = Path(__file__).with_name("chat_p4_wire_gate") / "sitecustomize.py"
ADMISSION = ROOT / ".runtime" / "chat-p4-text-live-admission.json"
LIMIT, OUTPUT, CAP = 8, 900, Decimal("0.40")
URL = "http://127.0.0.1:5195"


def _uuid(value):
    return str(UUID(value))


def _stack():
    with patch.object(offline_demo, "STATE", p4.STATE):
        state = _private_json(p4.STATE)
        require(state["status"] == "READY" and state["url"] == URL, "P4_ISOLATED_OFFLINE_REQUIRED")
        require(offline_demo.status(state)["healthy_now"], "P4_OFFLINE_UNHEALTHY")
        stack = offline_demo.restore(state)
    stack.prefix = f"aftercare-fresh-{stack.run_id}"
    stack.label = f"{LABEL}={stack.run_id}"
    config = _private_json(stack.runtime / "local.json")["app"]
    require(
        config["chat_investigation_enabled"] and not config["chat_vision_enabled"],
        "P4_ISOLATED_CONFIG_INVALID",
    )
    stack.secrets = [
        make_url(config["database_url"]).password,
        config["object_access_key"],
        config["object_secret_key"],
    ]
    return state, stack, config


def _read_audit(stack, case_id, old_run_id, conversation_id, run_id=None):
    return stack.json_exec(
        "P4_LIVE_READ_ONLY",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from aftercare.business.models import Execution
from aftercare.config import load_settings
from aftercare.conversation_analysis.tables import ChatModelAttempt
from aftercare.conversations.tables import ChatMaterialSource
from aftercare.db import guarded_engine
from aftercare.decisions.contracts import DecisionScope
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from aftercare.investigation.input import build_input
engine = guarded_engine(load_settings("app"))
with Session(engine) as db, db.begin():
    db.execute(text("SET TRANSACTION READ ONLY"))
    old = db.get_one(AgentRun, {old_run_id!r})
    binding = db.scalar(select(RunEvent).where(RunEvent.run_id == old.id, RunEvent.kind == "decision_input")).payload
    scope = DecisionScope.model_validate(binding["scope"])
    assert scope.case_id == {case_id!r} and scope.conversation_id == {conversation_id!r}
    selected = {run_id!r}
    attempts = list(db.scalars(select(ModelAttempt).order_by(ModelAttempt.started_at)))
    print(json.dumps({{
        "relevant_input_sha256": build_input(scope).relevant_input_sha256,
        "scope_sha256": scope.input_sha256,
        "issue_id": scope.issue_id,
        "old_run_reason": old.reason,
        "case_runs": list(db.scalars(select(AgentRun.id).where(AgentRun.case_id == scope.case_id).order_by(AgentRun.created_at))),
        "active_count": len(list(db.scalars(select(AgentRun.id).where(AgentRun.status.in_(["QUEUED", "RUNNING", "WAITING_INPUT"]))))),
        "other_attempt_count": sum(row.run_id != selected for row in attempts),
        "vision_attempt_count": len(list(db.scalars(select(ChatModelAttempt.id)))),
        "execution_count": len(list(db.scalars(select(Execution.id)))),
        "originals": [dict(attachment_id=row.attachment_id, sha256=row.sha256, kind=row.kind) for row in db.scalars(
            select(ChatMaterialSource).where(ChatMaterialSource.conversation_id == scope.conversation_id).order_by(ChatMaterialSource.kind)
        )],
        "attempts": [
            dict(logical_id=row.logical_id, ordinal=row.ordinal, status=row.status,
                error_code=row.error_code, usage=row.usage, estimated_cost_usd=row.estimated_cost_usd,
                model=row.metadata_json.get("model"), alias=row.metadata_json.get("alias"),
                reserved_cost_usd=row.metadata_json.get("reserved_cost_usd"),
                transport=row.metadata_json.get("transport_evidence"))
            for row in attempts if row.run_id == selected
        ],
    }}))
engine.dispose()
""",
    )


def _plan(offline_path):
    state, stack, config = _stack()
    raw = offline_path.read_bytes()
    evidence = json.loads(raw)
    scenario = evidence["scenario"]
    require(
        evidence["status"] == "PASSED"
        and evidence["isolated_url"] == URL
        and scenario["stage"] == "verified"
        and scenario["staff_selected_path"] in {"SINGLE", "MULTI"},
        "P4_OFFLINE_EVIDENCE_REQUIRED",
    )
    case_id, conversation_id, old_run = map(
        _uuid, (scenario["case_id"], scenario["conversation_id"], scenario["staff_run_id"])
    )
    baseline = _read_audit(stack, case_id, old_run, conversation_id)
    require(
        not any(
            baseline[key]
            for key in (
                "active_count",
                "other_attempt_count",
                "vision_attempt_count",
                "execution_count",
            )
        )
        and len(baseline["originals"]) == 2
        and baseline["case_runs"] == [scenario["auto_run_id"], old_run],
        "P4_BASELINE_NO_LONGER_PRISTINE",
    )
    originals = [
        {key: item[key] for key in ("attachment_id", "sha256", "kind")}
        for item in scenario["materials"]
    ]
    require(baseline["originals"] == originals, "P4_ORIGINAL_PROVENANCE_CHANGED")
    spec = _configuration()["gpt-6.1-terra"]
    reserve = models._reserve_price(spec, spec.context_tokens, OUTPUT)
    require(
        spec.compatibility == "VERIFIED"
        and spec.output_tokens >= OUTPUT
        and spec.tool_calls
        and spec.cache_write_ttl != "12h"
        and reserve is not None
        and reserve * LIMIT <= CAP
        and EMBEDDING.is_dir(),
        "P4_PROVIDER_SCOPE_UNAVAILABLE",
    )
    image = evidence["plan"]["images"][p4.BACKEND]
    require(
        isinstance(image, str)
        and len(image) == 71
        and image.startswith("sha256:")
        and all(char in "abcdef0123456789" for char in image[7:]),
        "P4_IMAGE_PIN_REQUIRED",
    )
    for role in ("api", "worker"):
        actual = stack.docker(
            "P4_IMAGE_PIN",
            "container",
            "inspect",
            "--format",
            "{{.Image}}",
            owned_container(stack, role),
            log_output=False,
        ).stdout.strip()
        require(actual == image, "P4_IMAGE_PIN_MISMATCH")
    plan = {
        "scope": "ISOLATED_SYNTHETIC_P4_TEXT_AFTER_HUMAN_MATERIAL_REVIEW",
        "stack_id": stack.run_id,
        "backend_image_id": image,
        "url": URL,
        "case_id": case_id,
        "conversation_id": conversation_id,
        "old_run_id": old_run,
        "baseline": baseline,
        "source_offline_report_sha256": hashlib.sha256(raw).hexdigest(),
        "relevant_input_sha256": baseline["relevant_input_sha256"],
        "issue_id": baseline["issue_id"],
        "source_spec_sha256": models.digest(spec.model_dump(mode="json")),
        "maximum_provider_posts": LIMIT,
        "model": spec.model,
        "fixed_registry_alias": "analysis",
        "maximum_estimated_usd": str(CAP),
        "estimated_full_context_per_post_usd": str(reserve),
        "estimated_total_at_maximum_posts_usd": str(reserve * LIMIT),
        "output_tokens": OUTPUT,
        "issue_limit_microusd": config.get("chat_investigation_issue_max_microusd", 200000),
        "issue_limit_calls": config.get("chat_investigation_issue_max_calls", 16),
        "wire_guard_sha256": hashlib.sha256(GATE.read_bytes()).hexdigest(),
        "local_embedding_revision": REVISION,
    }
    return plan, spec, state, stack


def _exercise(stack, plan, report, report_path):
    credentials = {item["role"]: item for item in _private_json(stack.credentials_path)}
    with ExitStack() as managers:
        clients = {
            role: managers.enter_context(
                httpx.Client(
                    base_url=f"{URL}/api/",
                    headers={"Authorization": f"Bearer {credentials[role]['token']}"},
                    timeout=10,
                    trust_env=False,
                )
            )
            for role in ("customer", "support")
        }
        support, customer = clients["support"], clients["customer"]
        require(read(support, "runtime")["mode"] == "offline", "P4_API_MUST_REMAIN_OFFLINE")
        base = f"conversations/{plan['conversation_id']}/decisions"
        prior = read(support, base)["snapshot"]
        require(
            prior
            and prior["original_run_id"] == plan["old_run_id"]
            and all(
                item["action"] not in {"REFUND", "REPLACE"}
                for item in prior["candidate"]["options"]
            ),
            "P4_PRIOR_HANDOFF_REQUIRED",
        )
        require(
            p3._case(support, plan["case_id"])["eligibility"]["status"] == "ELIGIBLE",
            "P4_MATERIAL_REVIEW_REQUIRED",
        )
        report["request_ids"] = {}
        key = p3._key(report, "p4_single_compatibility_run")
        _save(report_path, report)
        result = p3._json(support, base + "/run", 202, key, {})
        run_id = report["run_id"] = _uuid(result["run_id"])
        _save(report_path, report)
        require(run_id != plan["old_run_id"], "P4_NEW_RUN_REQUIRED")
        run_path = f"cases/{plan['case_id']}/runs/{run_id}"
        settled = _wait(support, run_path, lambda row: row["status"] in p4.TERMINAL, seconds=150)
        report["run"] = {"status": settled["status"], "reason": settled["reason"]}
        trace = p4._events(support, plan["case_id"], run_id)
        report["tool_trace"] = [
            row["payload"] for row in trace if row["kind"] == "investigation_tool"
        ]
        report["route"] = p4._route(trace)
        if settled["status"] in {"COMPLETED", "NEEDS_REVIEW"}:
            detail = _wait(
                support,
                base,
                lambda row: (row.get("snapshot") or {}).get("original_run_id") == run_id,
                seconds=30,
            )
            snapshot = detail["snapshot"]
            report["candidate"] = {
                "id": snapshot["id"],
                "origin": snapshot["origin"],
                "options": snapshot["candidate"]["options"],
                "checks": snapshot["checks"],
            }
        require(read(customer, base + "/offer")["offer"] is None, "P4_CUSTOMER_OFFER_UNEXPECTED")
        require(
            not read(support, f"cases/{plan['case_id']}/executions")["items"],
            "P4_EXECUTION_UNEXPECTED",
        )


def _execute(state, stack, spec, plan, report, report_path):
    worker = owned_container(stack, "worker")
    net, egress = (f"{stack.prefix}-{name}" for name in ("net", "ingress"))
    require(
        all(stack.owned("network", name) is True for name in (net, egress)),
        "P4_NETWORK_OWNERSHIP_REQUIRED",
    )
    paid = f"{stack.prefix}-p4-paid-worker"
    runtime, wire, gate = (
        stack.directory / name for name in ("p4-paid-runtime", "p4-text-wire", "p4-text-python")
    )
    secret = runtime / "model-secret.json"
    require(
        not any(path.exists() for path in (runtime, wire, gate)), "P4_OWNED_PAID_RESOURCES_EXIST"
    )
    offline_stopped = False
    try:
        for directory in (runtime, wire, gate):
            directory.mkdir(mode=0o700)
        _one_file(wire / "lock", "")
        _one_file(wire / "plan.json", json.dumps(plan))
        copyfile(GATE, gate / "sitecustomize.py")
        require(
            hashlib.sha256((gate / "sitecustomize.py").read_bytes()).hexdigest()
            == plan["wire_guard_sha256"],
            "P4_WIRE_GATE_CHANGED",
        )
        model = spec.model_dump(mode="json")
        model.update(alias="analysis", data_scopes=["customer_conversation"], vision=False)
        for key in tuple(model):
            if key.startswith("vision_"):
                model[key] = None
        total = plan["estimated_total_at_maximum_posts_usd"]
        registry = models.Registry.model_validate(
            {
                "version": "ac-p4-isolated-synthetic-terra-text",
                "models": [model],
                "allowed_providers": ["openai"],
                "native_streaming": False,
                "max_attempts": LIMIT,
                "max_run_attempts": LIMIT,
                "max_usd": total,
                "max_run_usd": total,
            }
        )
        _one_file(runtime / "local.json", json.dumps(_private_json(stack.runtime / "local.json")))
        _one_file(runtime / "models.json", registry.model_dump_json())
        key = _private_json(ROOT / ".runtime" / "model-secret.json")["api_key"]
        require(isinstance(key, str) and bool(key.strip()), "P4_PROJECT_KEY_REQUIRED")
        stack.secrets.append(key)
        _one_file(secret, json.dumps({"api_key": key}))
        offline_stopped = True
        stack.docker("STOP_OWN_OFFLINE_WORKER", "stop", worker)
        state["resources"] = [*stack.resources, ("container", paid)]
        with patch.object(offline_demo, "STATE", p4.STATE):
            offline_demo.save(state)
        report["worker_swap"] = "OWNED_PAID_WORKER_REGISTERED"
        _save(report_path, report)
        options = (
            *_worker_options(runtime, "live-hybrid"),
            "--env",
            f"AFTERCARE_EMBEDDING_REVISION={REVISION}",
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
            "--env",
            "LANGSMITH_TRACING=false",
            "--env",
            "LANGCHAIN_TRACING_V2=false",
            "--env",
            "PYTHONPATH=/p4-gate",
            *[
                item
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                )
                for item in ("--env", f"{key}=")
            ],
            "--mount",
            f"type=bind,source={EMBEDDING},target=/workspace/models/{REVISION},readonly",
            "--mount",
            f"type=bind,source={gate},target=/p4-gate,readonly",
            "--mount",
            f"type=bind,source={wire},target=/p4-wire",
            "--entrypoint",
            "python",
        )
        stack.run(
            plan["backend_image_id"],
            "p4-paid-worker",
            net,
            options,
            (
                "-c",
                "import sitecustomize; assert sitecustomize.READY; from celery.__main__ import main; main()",
                *WORKER[1:],
            ),
        )
        stack.docker("CONNECT_OWN_PAID_EGRESS", "network", "connect", egress, paid)
        stack.wait(
            "P4_PAID_WORKER_READY",
            "exec",
            owned_container(stack, "api"),
            "celery",
            "-A",
            "aftercare.worker",
            "inspect",
            "--timeout",
            "2",
            "ping",
        )
        require(not list(wire.glob("admission-*.json")), "P4_PREEXISTING_PROVIDER_ADMISSION")
        _exercise(stack, plan, report, report_path)
    finally:
        try:
            ownership = stack.owned("container", paid)
            require(ownership is not False, "P4_PAID_WORKER_OWNERSHIP_CHANGED")
            if ownership:
                stack.docker("REMOVE_OWN_PAID_WORKER", "rm", "--force", paid)
            report["paid_worker_removed"] = stack.owned("container", paid) is None
        finally:
            secret.unlink(missing_ok=True)
            report["private_worker_key_removed"] = not secret.exists()
            if offline_stopped:
                stack.docker("RESTART_OWN_OFFLINE_WORKER", "start", worker)
                report["offline_worker_restarted"] = True
            if report.get("paid_worker_removed"):
                state["resources"] = [
                    entry for entry in stack.resources if entry != ("container", paid)
                ]
                with patch.object(offline_demo, "STATE", p4.STATE):
                    offline_demo.save(state)
            report["wire"] = [
                {
                    **_private_json(entry),
                    "response": _private_json(response)
                    if (response := wire / entry.name.replace("admission-", "response-")).exists()
                    else None,
                }
                for entry in sorted(wire.glob("admission-*.json"))
            ]
            report["audit"] = _read_audit(
                stack,
                plan["case_id"],
                plan["old_run_id"],
                plan["conversation_id"],
                report.get("run_id"),
            )
    rows, admissions = report["audit"]["attempts"], report["wire"]
    require(
        report["paid_worker_removed"]
        and report["private_worker_key_removed"]
        and report["offline_worker_restarted"]
        and 0 < len(rows) == len(admissions) <= LIMIT
        and all(row["response"] and row["response"]["http_status"] == 200 for row in admissions)
        and all(
            row["status"] == "SUCCEEDED"
            and row["alias"] == "analysis"
            and row["model"] == plan["model"]
            and row["usage"]
            and row["estimated_cost_usd"] is not None
            and row["transport"]["path"] == "DEFAULT_SYNC"
            for row in rows
        )
        and report["tool_trace"]
        and report.get("candidate")
        and not any(
            report["audit"][key]
            for key in ("other_attempt_count", "vision_attempt_count", "execution_count")
        ),
        "P4_LIVE_COMPATIBILITY_INCOMPLETE",
    )
    estimated = sum(Decimal(row["estimated_cost_usd"]) for row in rows)
    report["estimated_usage_cost_usd"] = str(estimated)
    require(
        estimated <= Decimal(plan["estimated_total_at_maximum_posts_usd"]),
        "P4_ACTUAL_ESTIMATE_EXCEEDS_RESERVATION",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute and (args.report is None or not args.expected_plan_sha256):
        parser.error("--execute requires --expected-plan-sha256 and a new --report")
    # Validate the private state before creating its sibling lock, then read it
    # again under the lock as part of the plan.
    _private_json(p4.STATE)
    with offline_demo.lifecycle_lock(p4.STATE, shared=not args.execute):
        return _run(args)


def _run(args):
    require(not ADMISSION.exists(), "P4_PROTOCOL_ALREADY_ADMITTED")
    plan, spec, state, stack = _plan(args.offline_report)
    signature = models.digest(plan)
    if not args.execute:
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": signature, "plan": plan}, indent=2
            )
        )
        return 0
    require(signature == args.expected_plan_sha256, "P4_APPROVED_PLAN_CHANGED")
    report = {"status": "IN_PROGRESS", "plan_sha256": signature, "plan": plan}
    _one_file(args.report, json.dumps(report))
    try:
        _one_file(ADMISSION, json.dumps({"plan_sha256": signature, "stack_id": stack.run_id}))
        parent = os.open(ADMISSION.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        _execute(state, stack, spec, plan, report, args.report)
        report["status"] = "PASSED"
    except VerificationError as error:
        report.update(status="FAILED", failure_code=str(error))
    except httpx.TimeoutException:
        report.update(status="FAILED", failure_code="HTTP_TIMEOUT_OUTCOME_UNKNOWN")
    except (
        ProbeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        httpx.HTTPError,
        subprocess.TimeoutExpired,
    ):
        report.update(status="FAILED", failure_code="P4_LIVE_EXECUTION_INCOMPLETE")
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        VerificationError,
        ProbeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ):
        print("P4_LIVE_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
