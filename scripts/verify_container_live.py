"""Check app Compose live wiring. Paid requests require --execute-paid.

Run from the repository root after indexing app policies:
    PYTHONPATH=backend/src PYTHONPYCACHEPREFIX="$HOME/.cache/aftercare-agent/pycache" \
      "$HOME/.cache/aftercare-agent/backend-venv/bin/python" scripts/verify_container_live.py \
      --output reports/deployment/app-live.json --execute-paid
"""

import argparse
import json
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from aftercare.auth import token_hash
from aftercare.business.models import Case, Execution
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from aftercare.dispatch import DispatchJob
from aftercare.intake_audit import IntakeModelAudit
from aftercare.intelligence.tables import AgentRun, ModelAttempt
from aftercare.queue import make_queue
from sqlalchemy import select
from sqlalchemy.orm import Session

BASE = "http://127.0.0.1:8020"
APP_REDIS = "redis://127.0.0.1:56379/0"
PREVIEW_TEXT = "商家此前答应返还款项，我想查那笔钱目前处理到哪一步了。"
WORKER_TEXT = "请只查询这笔订单已有的退款进度。若无系统记录请如实说明，不申请任何新操作。"
ALIASES = {"economy", "analysis"}
SAFE_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_token_details",
    "output_token_details",
)
CONTAINER_PROBE = """
import json, os, sys
from aftercare.config import load_settings
from aftercare.intelligence.models import Registry

settings = load_settings("app")
registry = Registry.read(settings.model_config_path)
aliases = {}
for model in registry.models:
    aliases[model.alias] = {
        "model": model.model,
        "provider": model.provider,
        "interface": model.interface,
        "compatibility": model.compatibility,
        "compatibility_version": model.compatibility_version,
        "price_date": model.price_date.isoformat() if model.price_date else None,
        "output_tokens": model.output_tokens,
        "context_tokens": model.context_tokens,
        "pricing_usd_per_million": {
            name: str(value) if value is not None else None
            for name, value in (
                ("input", model.input_usd_per_million),
                ("output", model.output_usd_per_million),
                ("cache_read", model.cache_read_usd_per_million),
                ("cache_write", model.cache_write_usd_per_million),
            )
        },
        "cache_write_ttl": model.cache_write_ttl,
    }
out = {
    "container": settings.container,
    "profile": settings.profile,
    "model_enabled": settings.model_enabled,
    "embedding_enabled": settings.embedding_enabled,
    "embedding_revision": settings.embedding_model_path.name if settings.embedding_model_path else None,
    "registry_version": registry.version,
    "max_attempts": registry.max_attempts,
    "max_run_attempts": registry.max_run_attempts,
    "max_run_usd": str(registry.max_run_usd),
    "max_usd": str(registry.max_usd),
    "timeout_seconds": registry.timeout_seconds,
    "deadline_seconds": registry.deadline_seconds,
    "native_streaming": registry.native_streaming,
    "provider_limits": {
        provider: limit.model_dump() for provider, limit in registry.provider_limits.items()
    },
    "aliases": aliases,
}
if sys.argv[1] == "search":
    from aftercare.runtime import open_runtime
    with open_runtime("app") as (_, _, business, agent):
        documents = business.policy_catalogue()
        result = agent.search.search_selected("查询退款账本和退款处理进度", documents)
        out["search"] = {
            "mode": result.mode,
            "encoder": agent.search.encoder,
            "catalogue_count": len(documents),
            "lexical_count": len(result.lexical),
            "semantic_count": len(result.semantic),
            "fused_count": len(result.documents),
            "blockers": result.blockers,
        }
print(json.dumps(out))
"""


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def docker(*args, payload=None, timeout=12):
    result = subprocess.run(
        ["docker", *args],
        input=payload,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    require(result.returncode == 0, "DOCKER_CONTAINER_PROBE_FAILED")
    return result.stdout.strip()


def docker_service(project, service):
    ids = docker(
        "ps",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).splitlines()
    require(len(ids) == 1, "EXPECTED_ONE_APP_SERVICE_CONTAINER")
    identity = docker(
        "inspect",
        "--format",
        "{{.Config.Hostname}} {{.Image}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
        ids[0],
    ).split()
    require(len(identity) == 3 and identity[2] == "healthy", "APP_SERVICE_NOT_HEALTHY")
    return {
        "container_id": ids[0],
        "hostname": identity[0],
        "image": identity[1],
        "health": identity[2],
    }


def runtime_config(container, *, search=False):
    output = docker(
        "exec",
        "-i",
        container["container_id"],
        "python",
        "-",
        "search" if search else "config",
        payload=CONTAINER_PROBE,
        timeout=120,
    )
    return json.loads(output.splitlines()[-1])


def client_for(token):
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        follow_redirects=False,
        timeout=25,
    )


def request(client, path, *, body=None, key=None, expected=200):
    response = (
        client.get("/api" + path)
        if body is None
        else client.post("/api" + path, json=body, headers={"Idempotency-Key": key or str(uuid4())})
    )
    require(
        response.status_code == expected, f"EXPECTED_HTTP_{expected}_GOT_{response.status_code}"
    )
    return response.json()


def authenticate(engine, client, credential, role):
    who = request(client, "/me")
    with Session(engine) as db:
        actor = db.scalar(select(User).where(User.token_hash == token_hash(credential)))
    require(
        actor is not None
        and actor.enabled
        and who["role"] == actor.role == role
        and who["user_id"] == actor.id
        and who["customer_id"] == actor.customer_id,
        "APP_HTTP_IDENTITY_MISMATCH",
    )
    return who


def assert_only_container_worker(hostname):
    workers = make_queue("app").control.inspect(timeout=2).ping()
    require(
        set(workers or {}) == {f"aftercare-worker@{hostname}"}, "APP_WORKER_ATTRIBUTION_AMBIGUOUS"
    )


def preflight(settings, customer, support, project, report):
    require(not settings.container and settings.redis_url == APP_REDIS, "HOST_APP_REDIS_REQUIRED")
    api = docker_service(project, "api-app")
    worker = docker_service(project, "worker-app")
    require(
        docker("port", api["container_id"], "8000/tcp") == "127.0.0.1:8020", "APP_API_PORT_MISMATCH"
    )
    assert_only_container_worker(worker["hostname"])
    require(request(support, "/readyz")["status"] == "ready", "APP_API_NOT_READY")
    runtime = request(support, "/runtime")
    require(runtime["mode"] == "live", "HTTP_APP_MODE_NOT_LIVE")
    require(request(customer, "/intake/capabilities")["model"] is True, "APP_INTAKE_MODEL_DISABLED")
    report["containers"] = {"api": api, "worker": worker}
    report["runtime"] = runtime

    api_config = runtime_config(api, search=True)
    worker_config = runtime_config(worker)
    hybrid = api_config.pop("search")
    report["configuration"] = api_config
    report["read_only_hybrid_probe"] = hybrid
    require(api_config == worker_config, "APP_CONTAINER_CONFIGURATION_MISMATCH")
    require(
        api_config["container"]
        and api_config["profile"] == "app"
        and api_config["model_enabled"]
        and api_config["embedding_enabled"]
        and api_config["embedding_revision"]
        and hybrid["mode"] == "HYBRID"
        and hybrid["semantic_count"] > 0
        and not hybrid["blockers"],
        "APP_HYBRID_NOT_READY",
    )
    runtime_aliases = {row["alias"]: row for row in runtime["models"]}
    require(
        set(api_config["aliases"]) == set(runtime_aliases) == ALIASES
        and all(
            row["available"]
            and row["configured"]
            and row["compatibility"] == "VERIFIED"
            and row["model"] == api_config["aliases"][alias]["model"]
            for alias, row in runtime_aliases.items()
        )
        and len({row["model"] for row in runtime_aliases.values()}) == 2
        and bool(api_config["provider_limits"]),
        "APP_LIVE_REGISTRY_NOT_READY",
    )
    require(
        api_config["max_run_attempts"] == 12
        and Decimal(api_config["max_run_usd"]) == Decimal("0.10")
        and 0 < Decimal(api_config["max_usd"]) <= Decimal("0.10")
        and api_config["max_attempts"] <= 12,
        "APP_LIVE_BUDGET_MISMATCH",
    )
    report["status"] = "PREFLIGHT_PASSED"
    return api_config


def page(client, resource):
    for offset in range(0, 3000, 300):
        rows = request(client, f"/{resource}?limit=300&offset={offset}")["items"]
        yield from rows
        if len(rows) < 300:
            return
    raise CheckFailed("APP_FIXTURE_SCAN_LIMIT")


def choose_status_order(engine, support, order_id):
    with Session(engine) as db:
        used = set(db.scalars(select(Case.order_id).where(Case.intent == "refund_status")))
        pending = set(
            db.scalars(
                select(Execution.order_id).where(Execution.status.not_in({"SUCCEEDED", "FAILED"}))
            )
        )
    for row in page(support, "orders"):
        if (
            (order_id is None or row["id"] == order_id)
            and row["id"] not in used | pending
            and row["status"] == "DELIVERED"
            and row["payment_status"] == "CONFIRMED"
        ):
            return row
    raise CheckFailed("SAFE_REFUND_STATUS_ORDER_NOT_FOUND")


def safe_usage(usage):
    if not usage:
        return None
    return {key: usage[key] for key in SAFE_USAGE_FIELDS if key in usage}


def intake_snapshot(engine, actor_id, key):
    with Session(engine) as db:
        row = db.get(IntakeModelAudit, (actor_id, key))
        if row is None:
            return None
        return {
            "status": row.status,
            "error": row.error_code,
            "metadata": row.metadata_json,
            "usage": safe_usage(row.usage),
            "reserved_cost_usd": row.reserved_cost_usd,
            "estimated_cost_usd": row.estimated_cost_usd,
            "result": row.result,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }


def usage_complete(row):
    usage = row.get("usage") or {}
    return all(
        type(usage.get(key)) is int for key in ("input_tokens", "output_tokens", "total_tokens")
    )


def verify_intake(engine, customer, actor, order, config, report):
    key = str(uuid4())
    body = {"message": PREVIEW_TEXT, "order_id": order["id"], "classifier": "model"}
    prior_cases = list(page(customer, "cases"))
    report.update(
        status="FAILED", request_key=key, order_id=order["id"], maximum_calls=1, maximum_usd="0.02"
    )
    preview = request(customer, "/intake/preview", body=body, key=key)
    first = intake_snapshot(engine, actor["user_id"], key)
    report.update(
        preview=preview["status"],
        classifier_source=preview["classifier_source"],
        intent=preview["intent"],
        audit=first,
    )
    require(first is not None, "INTAKE_PROVIDER_AUDIT_MISSING")
    replay = request(customer, "/intake/preview", body=body, key=key)
    second = intake_snapshot(engine, actor["user_id"], key)
    require(
        first == second and preview == replay and prior_cases == list(page(customer, "cases")),
        "INTAKE_REPLAY_OR_CASE_STATE_CHANGED",
    )
    report["idempotent_audit_unchanged"] = True
    require(
        first["status"] == "SUCCEEDED"
        and first["metadata"].get("ordinal") == 1
        and first["metadata"].get("alias") == "economy"
        and first["metadata"].get("model") == config["aliases"]["economy"]["model"]
        and first["metadata"].get("registry_version") == config["registry_version"]
        and first["started_at"]
        and first["finished_at"]
        and usage_complete(first)
        and first["estimated_cost_usd"] is not None
        and 0 < Decimal(first["reserved_cost_usd"]) <= Decimal("0.02")
        and Decimal(first["estimated_cost_usd"]) <= Decimal("0.02")
        and preview["classifier_source"] == "model"
        and preview["status"] == "READY"
        and preview["intent"] == "refund_status",
        "INTAKE_LIVE_CLASSIFICATION_NOT_VERIFIED",
    )
    report["status"] = "PASSED"
    print("Container API live preview and same-key audit replay verified.", flush=True)


def worker_audit(engine, run_id):
    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(ModelAttempt)
                .where(ModelAttempt.run_id == run_id)
                .order_by(ModelAttempt.started_at)
            )
        )
        job = db.scalar(
            select(DispatchJob).where(
                DispatchJob.kind == "agent",
                DispatchJob.target_id == run_id,
                DispatchJob.generation == 0,
            )
        )
        attempts = [
            {
                "logical_id": row.logical_id,
                "ordinal": row.ordinal,
                "status": row.status,
                "error": row.error_code,
                "usage": safe_usage(row.usage),
                "estimated_cost_usd": row.estimated_cost_usd,
                "metadata": row.metadata_json,
            }
            for row in rows
        ]
        return attempts, (
            {"id": job.id, "state": job.state, "claims": job.claims, "failures": job.failures}
            if job
            else None
        )


def verify_worker(engine, support, order, config, report):
    report.update(status="FAILED", order_id=order["id"], maximum_calls=12, maximum_usd="0.10")
    case = request(
        support,
        "/cases",
        expected=201,
        body={
            "order_id": order["id"],
            "intent": "refund_status",
            "description": WORKER_TEXT,
            "expected_version": order["version"],
        },
    )
    case_path = f"/cases/{UUID(case['id'])}"
    report["case_id"] = case["id"]
    before = request(support, case_path)
    eligibility = before["eligibility"]
    require(
        eligibility["status"] == "STATUS_ONLY"
        and not eligibility["actions"]
        and not eligibility["missing_materials"]
        and eligibility["policy_ids"]
        and not before["policy_resolution"]["blockers"]
        and not before["proposals"]
        and not before["approvals"]
        and all(row["status"] in {"SUCCEEDED", "FAILED"} for row in before["ledger"]),
        "APP_ORDER_NOT_SAFE_FOR_READ_ONLY_MODEL_RUN",
    )
    run = request(
        support,
        case_path + "/runs",
        expected=202,
        body={
            "expected_version": case["version"],
            "mode": "single",
            "strategy": "analysis",
        },
    )
    run_id = str(UUID(run["id"]))
    report["run_id"] = run_id
    path = f"{case_path}/runs/{run_id}"
    deadline = time.monotonic() + 155
    while time.monotonic() < deadline:
        run = request(support, path)
        attempts, job = worker_audit(engine, run_id)
        if (
            run["status"] not in {"QUEUED", "RUNNING"}
            and job
            and job["state"] in {"DONE", "FAILED"}
        ):
            break
        time.sleep(0.5)
    events = request(support, path + "/events")
    report.update(
        run_status=run["status"],
        run_reason=run["reason"],
        attempts=attempts,
        dispatch=job,
        retrieval=[event["payload"] for event in events if event["kind"] == "retrieval"],
        tools=[
            {"name": event["payload"]["name"], "status": event["payload"]["status"]}
            for event in events
            if event["kind"] == "tool"
        ],
        agent_roles=[
            {
                "role": event["payload"]["role"],
                "stage": event["payload"]["stage"],
                "source": event["payload"].get("source"),
            }
            for event in events
            if event["kind"] == "agent"
        ],
    )
    after = request(support, case_path)
    unchanged = all(
        after[key] == before[key] for key in ("case", "order", "ledger", "proposals", "approvals")
    )
    report["business_snapshot_unchanged"] = unchanged
    require(unchanged, "STATUS_RUN_CHANGED_BUSINESS_STATE")
    known = [
        Decimal(row["estimated_cost_usd"])
        for row in attempts
        if row["estimated_cost_usd"] is not None
    ]
    report["estimated_known_cost_usd"] = str(sum(known, Decimal(0)))
    report["unknown_cost_attempts"] = len(attempts) - len(known)
    output = run["output"] or {}
    require(
        job
        and job["state"] == "DONE"
        and job["claims"] == 1
        and 0 < len(attempts) <= 12
        and sum(known, Decimal(0)) <= Decimal("0.10")
        and any(
            row["status"] == "SUCCEEDED"
            and usage_complete(row)
            and row["estimated_cost_usd"] is not None
            and row["metadata"]["alias"] == "analysis"
            and row["metadata"]["model"] == config["aliases"]["analysis"]["model"]
            and row["metadata"]["registry_version"] == config["registry_version"]
            for row in attempts
        ),
        "CONTAINER_WORKER_MODEL_ATTEMPT_NOT_VERIFIED",
    )
    report["container_worker_model_verified"] = True
    require(
        run["status"] == "COMPLETED"
        and run["reason"] == "STATUS_ONLY"
        and output.get("action") is None
        and not output.get("lines")
        and any(row["mode"] == "HYBRID" and row["semantic"] for row in report["retrieval"])
        and any(
            row == {"name": "read_case_facts", "status": "SUCCEEDED"} for row in report["tools"]
        ),
        "CONTAINER_WORKER_STATUS_ONLY_OR_HYBRID_NOT_VERIFIED",
    )
    report["status"] = "PASSED"
    print(
        "Container worker live analysis, hybrid retrieval and unchanged ledger verified.",
        flush=True,
    )


def run_checks(args, report):
    settings = load_settings("app")
    engine = guarded_engine(settings)
    try:
        credentials = json.loads((ROOT / ".runtime/users-app.json").read_text())
        customer_token, support_token = (
            next(row["token"] for row in credentials if row["role"] == role)
            for role in ("customer", "support")
        )
        with client_for(customer_token) as customer, client_for(support_token) as support:
            actor = authenticate(engine, customer, customer_token, "customer")
            authenticate(engine, support, support_token, "support")
            config = preflight(settings, customer, support, args.compose_project, report)
            with Session(engine) as db:
                require(
                    db.scalar(
                        select(AgentRun.id)
                        .where(AgentRun.status.in_({"QUEUED", "RUNNING"}))
                        .limit(1)
                    )
                    is None,
                    "APP_ALREADY_HAS_ACTIVE_AGENT_WORK",
                )
            owned = next(page(customer, "orders"), None)
            require(
                owned is not None and owned["customer_id"] == actor["customer_id"],
                "OWNED_INTAKE_ORDER_NOT_FOUND",
            )
            rule_only = request(
                customer,
                "/intake/preview",
                body={
                    "message": PREVIEW_TEXT,
                    "order_id": owned["id"],
                    "classifier": "rules",
                },
            )
            require(
                rule_only["order"]
                and rule_only["order"]["id"] == owned["id"]
                and rule_only["intent"] is None
                and rule_only["intent_candidates"] == [],
                "INTAKE_FIXTURE_WOULD_BYPASS_MODEL",
            )
            order = choose_status_order(engine, support, args.worker_order_id)
            report["selected_orders"] = {"intake": owned["id"], "worker": order["id"]}
            print("App live readiness and isolated container worker verified.", flush=True)
            if not args.execute_paid:
                return
            verify_intake(engine, customer, actor, owned, config, report["intake"])
            verify_worker(engine, support, order, config, report["worker"])
            assert_only_container_worker(report["containers"]["worker"]["hostname"])
            report["status"] = "PASSED"
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New private evidence file")
    parser.add_argument("--compose-project", default="aftercare-v2")
    parser.add_argument("--worker-order-id", help="Optional existing safe simulated order")
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    report = {
        "status": "FAILED",
        "source": "app_compose_container_live_smoke",
        "execute_paid": args.execute_paid,
        "usage_scope": "provider_reported_usage_and_registry_cost_estimates_not_invoice",
        "provider_limits_scope": "app_database_runtime_scope_not_cross_profile",
        "intake": {"status": "NOT_RUN"},
        "worker": {"status": "NOT_RUN"},
    }
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new private evidence file.\n")
    with os.fdopen(descriptor, "w") as stream:
        try:
            run_checks(args, report)
        except CheckFailed as error:
            report.update(status="FAILED", failure=str(error))
        except Exception as error:  # noqa: BLE001 - never print credentials or provider responses
            report.update(status="FAILED", failure=type(error).__name__)
        finally:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] == "FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
