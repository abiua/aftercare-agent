"""Review and execute the bounded, one-case P5 PostgreSQL text-only pilot."""

import argparse
import json
import os
import re
import stat
import sys
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/offline-browser-demo-p5-pg.json"
CAMPAIGN = "chat-p5-paid-terra-ordinary-v1"
SOURCE_CAMPAIGN = "chat-p5-controlled-pg-v1"
CASE = "DAMAGE-ORDINARY"
MODEL = "gpt-6.1-terra"
PROTOCOL = "p5-paid-text-v1"
GATE = ROOT / "scripts/chat_p4_wire_gate"
CALLS, ROW_USD = 8, Decimal("0.40")
TOTAL_CALLS, TOTAL_USD = 24, Decimal("1.20")


def _check(condition, code):
    if not condition:
        raise ValueError(code)


def _private(path):
    info = path.lstat()
    _check(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "P5_PAID_PRIVATE_FILE_REQUIRED",
    )
    return json.loads(path.read_bytes())


def _safe(error):
    value = str(error)
    return (
        value
        if re.fullmatch(r"(?:P5_|CHAT_EVAL_|DATABASE_)[A-Z0-9_]{1,80}", value)
        else type(error).__name__
    )


def _ids(campaign, arms):
    from uuid import NAMESPACE_URL, uuid5

    from aftercare.business.rules import digest
    from aftercare.evaluation.chat.manifest import PROTOCOL as EVAL_PROTOCOL

    return [
        {
            "id": str(uuid5(NAMESPACE_URL, digest([EVAL_PROTOCOL, campaign, 1, CASE, arm]))),
            "arm": arm,
        }
        for arm in arms
    ]


def _plan(controlled_path, image):
    import offline_demo
    from aftercare.business.rules import digest
    from aftercare.evaluation.chat.manifest import ARMS
    from aftercare.intelligence import models
    from aftercare.investigation.issue_budget import to_microusd
    from aftercare.investigation.prompt_evidence import PROMPT_VERSION
    from verify_chat_p2_offline import owned_container
    from verify_chat_p5_pg import _topology
    from verify_streaming_compatibility import _configuration

    baseline = _private(controlled_path)
    state = _private(STATE)
    evidence = baseline["acceptance"]
    _check(
        baseline["status"] == "PASSED"
        and baseline["plan_sha256"] == digest(baseline["plan"])
        and baseline["owned_stack"] == state["run_id"]
        and baseline["owned_url"] == state["url"] == "http://127.0.0.1:5200"
        and state["profile"] == "test"
        and state["status"] == "READY"
        and "CONTROLLED_WORKER_CORE" in evidence["boundary"],
        "P5_PAID_CONTROLLED_SOURCE_REQUIRED",
    )
    stack = offline_demo.restore(state)
    _check(
        Path(baseline["private_manifest_directory"])
        == stack.directory / "p5-pg-recovery-output/manifest"
        and not (stack.directory / CAMPAIGN).exists(),
        "P5_PAID_SOURCE_OR_ADMISSION_EXISTS",
    )
    source_plan_path = Path(baseline["private_manifest_directory"]) / "plan.json"
    source_plan = _private(source_plan_path)
    _check(
        digest(source_plan) == evidence["receipt"]["plan_sha256"]
        and source_plan["campaign"] == SOURCE_CAMPAIGN
        and source_plan["controls"]["prompt"] == PROMPT_VERSION == "compact-v5",
        "P5_PAID_SOURCE_PLAN_MISMATCH",
    )
    (source,) = (
        row for row in source_plan["cases"] if row["case_id"] == CASE and row["repetition"] == 1
    )
    scope = source["scope"]
    _check(
        digest(scope) == source["scope_sha256"]
        and re.fullmatch(r"eval_[a-f0-9]{32}", source["source_namespace"])
        and len(source["images"]) == len(source["material_objects"]) == 2,
        "P5_PAID_SOURCE_BINDING_INVALID",
    )
    spec = _configuration()[MODEL]
    _check(
        spec.compatibility == "VERIFIED"
        and spec.tool_calls
        and spec.context_tokens == 32000
        and spec.output_tokens == 900
        and spec.cache_write_ttl != "12h"
        and models.P4_OUTPUT_RESERVE == 1800,
        "P5_PAID_REGISTERED_MODEL_MISMATCH",
    )
    derived = models.ModelSpec.model_validate(
        {
            **spec.model_dump(mode="json"),
            "alias": "analysis",
            "data_scopes": ["customer_conversation"],
            "vision": False,
            "vision_checked_at": None,
            "vision_compatibility_version": None,
            "vision_input_tokens_per_image": None,
            "output_tokens": 1800,
        }
    )
    reserve = models._reserve_price(derived, derived.context_tokens, 1800)
    _check(
        reserve is not None
        and reserve > 0
        and reserve * CALLS <= ROW_USD
        and reserve * TOTAL_CALLS <= TOTAL_USD,
        "P5_PAID_RESERVATION_CAP",
    )
    reservation_microusd = to_microusd(reserve)
    _check(
        reservation_microusd * CALLS <= 400_000 and reservation_microusd * TOTAL_CALLS <= 1_200_000,
        "P5_PAID_ROUNDED_RESERVATION_CAP",
    )
    registry = models.Registry.model_validate(
        {
            "version": PROTOCOL,
            "models": [derived.model_dump(mode="json")],
            "allowed_providers": ["openai"],
            "native_streaming": False,
            "max_attempts": CALLS,
            "max_run_attempts": CALLS,
            "max_usd": str(ROW_USD),
            "max_run_usd": str(ROW_USD),
        }
    )
    endpoint = stack.docker(
        "P5_PAID_LOCAL_CONTEXT",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        log_output=False,
    ).stdout.strip()
    _check(
        endpoint.startswith("unix://")
        and (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")),
        "P5_PAID_LOCAL_DOCKER_REQUIRED",
    )
    _, system_id = _topology(stack, baseline["plan"]["images"])
    for role in ("api", "worker", "beat"):
        mark = stack.docker(
            "P5_PAID_OFFLINE_ROLE",
            "container",
            "inspect",
            "--format",
            '{{range .Config.Env}}{{if eq . "AFTERCARE_CONTAINER_MODEL_MODE=offline"}}yes{{end}}{{end}}',
            owned_container(stack, role),
            log_output=False,
        ).stdout.strip()
        _check(mark == "yes", "P5_PAID_BACKGROUND_MODEL_ENABLED")
    observed_image = stack.docker(
        "P5_PAID_IMAGE",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image,
        log_output=False,
    ).stdout.strip()
    _check(
        observed_image == image
        and system_id == evidence["system_identifier"]
        and offline_demo.status(state)["healthy_now"],
        "P5_PAID_STACK_NOT_READY",
    )
    return {
        "protocol": PROTOCOL,
        "campaign": CAMPAIGN,
        "profile": "test",
        "boundary": "REAL_PROVIDER_TEXT_ONLY_ONE_DEVELOPMENT_CASE",
        "independent_judgment": False,
        "vision": False,
        "actual_billing_known": False,
        "attempt_binding": "ROW_ONLY_NOT_MATCHED_TO_LOGICAL_ATTEMPT",
        "egress_scope": "OWNED_EGRESS_NETWORK_WITH_SDK_HTTP_URL_GATE",
        "interrupted_host_cleanup": "RECONCILE_JOURNALED_CONTAINER_AND_LOCAL_KEY_COPY_BEFORE_ANY_NEW_ADMISSION",
        "stack_id": stack.run_id,
        "system_identifier": system_id,
        "stack_images": baseline["plan"]["images"],
        "verifier_image": image,
        "sentinel_sha256": evidence["object_sentinel_sha256"],
        "controlled_report_sha256": sha256(controlled_path.read_bytes()).hexdigest(),
        "controlled_manifest_sha256": sha256(source_plan_path.read_bytes()).hexdigest(),
        "source": source,
        "scope_sha256": source["scope_sha256"],
        "conversation_id": scope["conversation_id"],
        "case_id": scope["case_id"],
        "issue_id": scope["issue_id"],
        "relevant_input_sha256": source["relevant_input_sha256"],
        "policy_catalogue_sha256": source["catalogue_sha256"],
        "prompt_version": PROMPT_VERSION,
        "latency_version": "runtime-utc-v1",
        "wire_rows": _ids(CAMPAIGN, ARMS),
        "model": MODEL,
        "output_tokens": 1800,
        "registry": registry.model_dump(mode="json"),
        "registry_sha256": digest(registry.model_dump(mode="json")),
        "deadline_seconds_per_row": registry.deadline_seconds,
        "maximum_row_posts": CALLS,
        "maximum_provider_posts": TOTAL_CALLS,
        "maximum_row_usd": str(ROW_USD),
        "maximum_total_usd": str(TOTAL_USD),
        "per_post_reserved_microusd": reservation_microusd,
        "reserved_at_full_post_cap_usd": str(reserve * TOTAL_CALLS),
        "files": {
            "verifier": sha256(Path(__file__).read_bytes()).hexdigest(),
            "sitecustomize.py": sha256((GATE / "sitecustomize.py").read_bytes()).hexdigest(),
            "p5_policy.py": sha256((GATE / "p5_policy.py").read_bytes()).hexdigest(),
        },
    }


def _validated_phase_results(events):
    claim_roles = {
        "single": {"strong_single"},
        "staged-fact": {"fact"},
        "staged-policy": {"policy"},
        "fact": {"fact"},
        "policy": {"policy"},
        "recheck": {"fact", "policy"},
    }
    completed = []
    for event in events:
        payload = event["payload"]
        if event["kind"] == "investigation_claims":
            phase = payload.get("phase_id")
            if payload.get("role") in claim_roles.get(phase, ()) and isinstance(
                payload.get("claims"), list
            ):
                completed.append(phase)
        elif event["kind"] == "investigation_assessment":
            round_index = payload.get("recheck_count")
            if (
                type(round_index) is int
                and round_index in (0, 1)
                and isinstance(payload.get("assessments"), list)
            ):
                completed.append(f"verify-{round_index}")
    return completed


def _worker(expected_digest):
    from datetime import datetime
    from time import monotonic, sleep

    import sitecustomize
    from aftercare.auth import UserContext
    from aftercare.business.rules import digest
    from aftercare.business.service import BusinessService
    from aftercare.business.storage import S3ObjectStore
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.evaluation.chat.manifest import Manifest
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import ChatEvaluationRunner, FrozenCase, _populate
    from aftercare.evaluation.runner import _namespace
    from aftercare.intelligence.models import ModelRunner, Registry
    from sqlalchemy import text

    _check(
        sitecustomize.READY
        and sitecustomize.PROTOCOL == PROTOCOL
        and callable(getattr(ChatEvaluationRunner, "_admitted_paid_text", None)),
        "P5_PAID_GATE_OR_IMAGE_NOT_READY",
    )
    wire = sitecustomize.STATE
    plan = sitecustomize.PLAN
    _check(
        digest(plan) == expected_digest
        and plan["campaign"] == CAMPAIGN
        and plan["model"] == MODEL
        and plan["stack_id"] == os.environ.get("P5_PAID_STACK_ID")
        and digest(plan["registry"]) == plan["registry_sha256"]
        and not list(wire.glob("admission-*.json")),
        "P5_PAID_WORKER_PLAN_MISMATCH",
    )
    deadline = monotonic() + 40
    while not (wire / "admitted").is_file() and monotonic() < deadline:
        sleep(0.2)
    _check(
        (wire / "admitted").is_file() and (wire / "admitted").read_text() == expected_digest,
        "P5_PAID_NETWORK_ADMISSION_REQUIRED",
    )
    settings = load_settings("test").model_copy(
        update={
            "chat_investigation_issue_max_calls": CALLS,
            "chat_investigation_issue_max_microusd": 400_000,
        }
    )
    _check(
        settings.container
        and not any(
            (
                settings.model_enabled,
                settings.chat_vision_enabled,
                settings.embedding_enabled,
                settings.chat_analysis_enabled,
            )
        ),
        "P5_PAID_OFFLINE_HOST_SETTINGS_REQUIRED",
    )
    source_record = plan["source"]
    scope = source_record["scope"]
    public = guarded_engine(settings, test_only=True)
    store = S3ObjectStore(settings)
    audited = (
        "agent_model_attempts",
        "chat_model_attempts",
        "intake_model_audits",
        "business_executions",
    )

    def public_counts(db):
        return {table: db.scalar(text(f"SELECT count(*) FROM public.{table}")) for table in audited}

    try:
        with public.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            identity = db.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))
            public_before = public_counts(db)
            old_exists = db.scalar(
                text("SELECT 1 FROM pg_namespace WHERE nspname=:namespace"),
                {"namespace": source_record["source_namespace"]},
            )
        _check(
            identity == plan["system_identifier"] and old_exists, "P5_PAID_OWNED_SOURCE_REQUIRED"
        )
        sentinel = store.get(sentinel_key(plan["stack_id"]), 64)
        _check(sha256(sentinel).hexdigest() == plan["sentinel_sha256"], "P5_PAID_SENTINEL_CHANGED")
        stamp = PgAttestation(plan["stack_id"], identity, store.bucket, plan["sentinel_sha256"], 64)
        actor = UserContext(user_id="support", role="support")
        frozen_time = datetime.fromisoformat(source_record["decision_at"])

        def case_for(engine, storage, bound_settings):
            return FrozenCase(
                CASE,
                1,
                BusinessService(engine, bound_settings, storage=storage, clock=lambda: frozen_time),
                actor,
                scope["conversation_id"],
                scope["issue_id"],
                frozen_time,
                source_record["provenance"],
            )

        with open_pg_runtime(settings, stamp, SOURCE_CAMPAIGN) as original:
            with original.source(
                CASE, 1, source_record["source_sha256"], require_existing=True
            ) as (
                engine,
                storage,
                fresh,
            ):
                _check(not fresh, "P5_PAID_CONTROLLED_SOURCE_MISSING")
                controlled_case = case_for(engine, storage, original.settings)
            frozen = original.freeze(controlled_case)
            _check(
                frozen.record["scope_sha256"] == plan["scope_sha256"]
                and frozen.record["relevant_input_sha256"] == plan["relevant_input_sha256"]
                and frozen.record["paired_sha256"] == source_record["paired_sha256"],
                "P5_PAID_FROZEN_SOURCE_CHANGED",
            )
            with open_pg_runtime(settings, stamp, CAMPAIGN) as paid:
                with paid.source(
                    CASE, 1, digest([expected_digest, source_record["source_sha256"]])
                ) as (engine, storage, fresh):
                    _check(fresh, "P5_PAID_CAMPAIGN_ALREADY_STARTED")
                    _populate(frozen, engine, storage)
                    paid_case = case_for(engine, storage, paid.settings)
                verified = paid.freeze(paid_case)
                _check(
                    verified.scope == frozen.scope
                    and verified.images == frozen.images
                    and verified.materials == frozen.materials,
                    "P5_PAID_CLONE_MISMATCH",
                )
                key = _private(Path("/p5-secret/model-secret.json"))["api_key"]
                _check(isinstance(key, str) and bool(key.strip()), "P5_PAID_PROJECT_KEY_REQUIRED")
                os.environ["AFTERCARE_OPENAI_KEY"] = key
                try:
                    runner = ChatEvaluationRunner._admitted_paid_text(
                        ModelRunner(Registry.model_validate(plan["registry"])),
                        approval_sha256=expected_digest,
                        scope_sha256=plan["scope_sha256"],
                    )
                    receipt = paid.run(Manifest(Path("/p5-output/manifest")), runner, [paid_case])
                finally:
                    os.environ.pop("AFTERCARE_OPENAI_KEY", None)
            original_after = original.freeze(controlled_case)
            _check(
                original_after.scope == frozen.scope
                and original_after.images == frozen.images
                and original_after.materials == frozen.materials,
                "P5_PAID_CONTROLLED_SOURCE_CHANGED",
            )
        with public.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            _check(public_counts(db) == public_before, "P5_PAID_PUBLIC_SCOPE_CHANGED")
            for row in receipt["rows"]:
                schema = _namespace(row["id"])
                row["validated_phase_results"] = []
                if db.scalar(text("SELECT to_regnamespace(:name)"), {"name": schema}) is None:
                    _check(not row["attempts"], "P5_PAID_ATTEMPT_SCHEMA_MISSING")
                    continue
                if row.get("run_id") is not None:
                    events = db.execute(
                        text(
                            f'SELECT kind, payload FROM "{schema}".agent_run_events '
                            "WHERE run_id=:run_id AND kind IN "
                            "('investigation_claims', 'investigation_assessment') ORDER BY seq"
                        ),
                        {"run_id": row["run_id"]},
                    ).mappings()
                    row["validated_phase_results"] = _validated_phase_results(events)
                for table in ("chat_model_attempts", "business_executions"):
                    relation = f"{schema}.{table}"
                    if db.scalar(text("SELECT to_regclass(:name)"), {"name": relation}) is not None:
                        _check(
                            db.scalar(text(f'SELECT count(*) FROM "{schema}".{table}')) == 0,
                            "P5_PAID_VISION_OR_EXECUTION_UNEXPECTED",
                        )
        return {
            **receipt,
            "public_attempts_and_execution_unchanged": True,
            "source_scope_and_objects_unchanged": True,
            "paid_arm_vision_and_execution_rows": 0,
        }
    finally:
        public.dispose()


def _wire_report(directory, rows):
    admissions = [_private(path) for path in sorted(directory.glob("admission-*.json"))]
    for item in admissions:
        path = directory / f"response-{item['call_index']:02}.json"
        item["response"] = _private(path) if path.is_file() else None
        _check(
            item["response"] is None
            or (
                item["response"]["row_id"] == item["row_id"]
                and item["response"]["call_index"] == item["call_index"]
            ),
            "P5_PAID_WIRE_RESPONSE_BINDING",
        )
    _check(
        len(admissions) <= TOTAL_CALLS
        and sum(item["reserved_microusd"] for item in admissions) <= 1_200_000,
        "P5_PAID_WIRE_TOTAL_CAP",
    )
    row_ids = {row["id"] for row in rows}
    _check(
        not rows or all(item["row_id"] in row_ids for item in admissions),
        "P5_PAID_WIRE_ROW_MISSING",
    )
    groups = []
    for row in rows:
        attempts = row["attempts"]
        _check(
            len(attempts) <= CALLS
            and all(
                item["model"] == MODEL
                and item["alias"] == "analysis"
                and item["transport_evidence"]["path"] == "DEFAULT_SYNC"
                for item in attempts
            ),
            "P5_PAID_ATTEMPT_BINDING",
        )
        posts = [item for item in admissions if item["row_id"] == row["id"]]
        reserved = sum(item["reserved_microusd"] for item in posts)
        known = [
            Decimal(item["estimated_cost_usd"])
            for item in attempts
            if item["estimated_cost_usd"] is not None
        ]
        _check(
            len(posts) <= CALLS and reserved <= 400_000 and sum(known, Decimal(0)) <= ROW_USD,
            "P5_PAID_ROW_COST_CAP",
        )
        groups.append(
            {
                "row_id": row["id"],
                "arm": row["arm"],
                "pg_attempt_count": len(attempts),
                "wire_post_count": len(posts),
                "wire_responses": sum(item["response"] is not None for item in posts),
                "attempt_binding": "UNMATCHED_PER_ATTEMPT",
                "row_count_agrees": len(attempts) == len(posts),
            }
        )
    ledger_attempts = [item for row in rows for item in row["attempts"]]
    costs = [
        Decimal(item["estimated_cost_usd"])
        for item in ledger_attempts
        if item["estimated_cost_usd"] is not None
    ]
    return {
        "admissions": admissions,
        "rows": groups,
        "known_estimated_usd": str(sum(costs, Decimal(0))) if costs else None,
        "unknown_estimated_cost_attempts": len(ledger_attempts) - len(costs),
        "reserved_wire_microusd": sum(item["reserved_microusd"] for item in admissions),
        "unknown_wire_responses": sum(item["response"] is None for item in admissions),
        "actual_billed_usd": None,
    }


def _workflow(row, required):
    native_required = {"verify-0" if phase == "verify" else phase for phase in required}
    phases = set(row.get("phases", ()))
    if row["arm"] == "multi" and phases & {"recheck", "verify-1"}:
        native_required.update(("recheck", "verify-1"))
    latest = {}
    for attempt in row["attempts"]:
        logical = re.fullmatch(
            r"p4:compact-v5:([a-z0-9-]+):([0-9]+)", attempt.get("logical_id", "")
        )
        ordinal = attempt.get("ordinal")
        if logical is None or logical[1] not in phases or type(ordinal) is not int or ordinal < 1:
            continue
        key = (int(logical[2]), ordinal)
        if logical[1] not in latest or key > latest[logical[1]][0]:
            latest[logical[1]] = (key, attempt["status"])
    successful = {phase for phase, (_, status) in latest.items() if status == "SUCCEEDED"}
    missing = sorted(native_required - successful)
    validated = row.get("validated_phase_results", [])
    missing_results = sorted(native_required - set(validated))
    row_valid = row.get("input_comparable") is True and (row.get("status"), row.get("reason")) in {
        ("COMPLETED", "ELIGIBLE"),
        ("NEEDS_REVIEW", "HUMAN_REVIEW"),
    }
    return {
        "workflow_completion": "COMPLETE"
        if row_valid and not missing and not missing_results
        else "INCOMPLETE",
        "required_native_phases": sorted(native_required),
        "missing_native_phase_success": missing,
        "missing_validated_phase_results": missing_results,
        "row_valid": row_valid,
    }


def _outcomes(rows, wire):
    evidence = {row["row_id"]: row for row in wire["rows"]}
    outcomes = []
    required_phases = {
        "strong_single": {"single"},
        "single_staged": {"staged-fact", "staged-policy", "verify-0"},
        "multi": {"fact", "policy", "verify"},
    }
    observed_success = False
    for row in rows:
        attempts = evidence[row["id"]]
        posts = [item for item in wire["admissions"] if item["row_id"] == row["id"]]
        statuses = [
            item["response"]["http_status"] for item in posts if item["response"] is not None
        ]
        has_http_ok = 200 in statuses
        observed_success |= has_http_ok
        if not posts:
            compatibility = "NOT_RUN"
        elif not statuses:
            compatibility = "UNKNOWN"
        elif not has_http_ok:
            compatibility = "FAILED" if len(statuses) == len(posts) else "PARTIAL"
        elif (
            len(statuses) == len(posts)
            and all(status == 200 for status in statuses)
            and attempts["row_count_agrees"]
            and all(
                item["status"] in {"SUCCEEDED", "FAILED"}
                and item.get("usage")
                and item.get("estimated_cost_usd") is not None
                for item in row["attempts"]
            )
            and required_phases[row["arm"]] <= {item["phase_family"] for item in posts}
        ):
            compatibility = "VERIFIED"
        else:
            compatibility = "PARTIAL"
        outcomes.append(
            {
                "row_id": row["id"],
                "arm": row["arm"],
                "provider_compatibility": compatibility,
                "candidate_status": row["candidate_status"],
                "business_rule_checks": row.get("program_checks", {}),
                **_workflow(row, required_phases[row["arm"]]),
            }
        )
    results = {item["provider_compatibility"] for item in outcomes}
    all_candidates = all(item["candidate_status"] == "AVAILABLE" for item in outcomes)
    any_candidate = any(item["candidate_status"] == "AVAILABLE" for item in outcomes)
    completed = sum(item["workflow_completion"] == "COMPLETE" for item in outcomes)
    if results == {"VERIFIED"} and all_candidates and completed == len(outcomes):
        status = "PASSED"
    elif observed_success or "UNKNOWN" in results or "PARTIAL" in results:
        status = "PARTIAL"
    else:
        status = "FAILED"
    return {
        "status": status,
        "provider_compatibility": next(iter(results)) if len(results) == 1 else "PARTIAL",
        "workflow_completion": "COMPLETE"
        if completed == len(outcomes)
        else "PARTIAL"
        if completed
        else "INCOMPLETE",
        "candidate_availability": "ALL"
        if all_candidates
        else "PARTIAL"
        if any_candidate
        else "NONE",
        "arm_outcomes": outcomes,
    }


def _execute(plan, checksum, report):
    from unittest.mock import patch

    import offline_demo
    from verify_chat_p2_vision_worker import _one_file
    from verify_chat_p5_pg import _topology
    from verify_fresh_bootstrap import LABEL

    state = _private(STATE)
    stack = offline_demo.restore(state)
    net, system = _topology(stack, plan["stack_images"])
    ingress = f"aftercare-fresh-{stack.run_id}-ingress"
    _check(
        stack.run_id == plan["stack_id"]
        and system == plan["system_identifier"]
        and stack.owned("network", ingress),
        "P5_PAID_TOPOLOGY_CHANGED",
    )
    root = stack.directory / CAMPAIGN
    root.mkdir(mode=0o700)
    wire, gate, secret, output, code = (
        root / name for name in ("wire", "gate", "secret", "output", "code")
    )
    for directory in (wire, gate, secret, output, code):
        directory.mkdir(mode=0o700)
    _one_file(wire / "lock", "")
    _one_file(wire / "plan.json", json.dumps(plan, ensure_ascii=False))
    for name in ("sitecustomize.py", "p5_policy.py"):
        content = (GATE / name).read_bytes()
        _check(sha256(content).hexdigest() == plan["files"][name], "P5_PAID_GATE_SOURCE_CHANGED")
        _one_file(gate / name, content.decode())
    verifier = Path(__file__).read_bytes()
    _check(sha256(verifier).hexdigest() == plan["files"]["verifier"], "P5_PAID_VERIFIER_CHANGED")
    _one_file(code / "verify_chat_p5_paid.py", verifier.decode())
    target = f"aftercare-fresh-{stack.run_id}-p5-paid-text"
    secret_path = secret / "model-secret.json"
    journaled = False
    rows = []
    report["private_run_directory"] = str(root)
    try:
        api_key = _private(ROOT / ".runtime/model-secret.json")["api_key"]
        _check(isinstance(api_key, str) and bool(api_key.strip()), "P5_PAID_PROJECT_KEY_REQUIRED")
        stack.secrets.append(api_key)
        _one_file(secret_path, json.dumps({"api_key": api_key}))
        state["resources"].append(["container", target])
        with patch.object(offline_demo, "STATE", STATE):
            offline_demo.save(state)
        journaled = True
        mounts = (
            (stack.runtime, "/workspace/.runtime", True),
            (wire, "/p4-wire", False),
            (gate, "/p4-gate", True),
            (secret, "/p5-secret", True),
            (output, "/p5-output", False),
            (code, "/p5-script", True),
        )
        options = [
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--env",
            f"P5_PAID_STACK_ID={stack.run_id}",
            "--env",
            "PYTHONPATH=/p4-gate",
            "--env",
            "LANGSMITH_TRACING=false",
            "--env",
            "LANGCHAIN_TRACING_V2=false",
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
        ]
        for variable in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            options.extend(("--env", variable + "="))
        for source, destination, readonly in mounts:
            options.extend(
                (
                    "--mount",
                    f"type=bind,source={source},target={destination}"
                    + (",readonly" if readonly else ""),
                )
            )
        stack.docker(
            "P5_PAID_START",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            target,
            "--label",
            f"{LABEL}={stack.run_id}",
            "--network",
            net,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            *options,
            "--entrypoint",
            "python",
            plan["verifier_image"],
            "/p5-script/verify_chat_p5_paid.py",
            "_worker",
            checksum,
            log_output=False,
        )
        _check(stack.owned("container", target) is True, "P5_PAID_VERIFIER_OWNER_MISMATCH")
        stack.docker("P5_PAID_EGRESS", "network", "connect", ingress, target)
        details = stack.docker(
            "P5_PAID_NETWORK_CHECK",
            "container",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            target,
            log_output=False,
        )
        _check(json.loads(details.stdout).keys() == {net, ingress}, "P5_PAID_NETWORK_BINDING")
        _one_file(wire / "admitted", checksum)
        report["stage"] = "PROVIDER_PILOT"
        waited = stack.docker(
            "P5_PAID_WAIT", "wait", target, timeout=600, required=False, log_output=False
        )
        if waited is None or waited.returncode != 0 or waited.stdout.strip() != "0":
            failure = output / "error.json"
            if failure.is_file():
                report["worker_error_code"] = _private(failure)["code"]
            raise ValueError("P5_PAID_WORKER_FAILED_OR_UNKNOWN")
        result = _private(output / "result.json")
        rows = result["rows"]
        _check(
            [{key: row[key] for key in ("id", "arm")} for row in rows] == plan["wire_rows"]
            and all(row["input_sha256"] == plan["scope_sha256"] for row in rows),
            "P5_PAID_PERSISTED_ROWS_MISMATCH",
        )
        report["receipt"] = result
    finally:
        try:
            if journaled:
                owned = stack.owned("container", target)
                _check(owned is not False, "P5_PAID_VERIFIER_OWNER_MISMATCH")
                if owned:
                    stack.docker("P5_PAID_REMOVE", "rm", "-f", target, log_output=False)
                _check(stack.owned("container", target) is None, "P5_PAID_VERIFIER_NOT_REMOVED")
                state["resources"].remove(["container", target])
                with patch.object(offline_demo, "STATE", STATE):
                    offline_demo.save(state)
                report["paid_verifier_removed"] = True
        finally:
            secret_path.unlink(missing_ok=True)
            report["temporary_local_key_copy_removed"] = not secret_path.exists()
            report["wire"] = _wire_report(wire, rows)
    _check(
        report["paid_verifier_removed"]
        and report["temporary_local_key_copy_removed"]
        and len(report["wire"]["rows"]) == len(plan["wire_rows"]),
        "P5_PAID_TRANSPORT_EVIDENCE_INCOMPLETE",
    )


def main():
    from aftercare.business.rules import digest
    from offline_demo import lifecycle_lock
    from verify_streaming_compatibility import _save

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controlled-report", type=Path, required=True)
    parser.add_argument("--verifier-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.verifier_image_id):
        parser.error("supply the complete immutable verifier image ID")
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires the reviewed plan digest and a new report")
    plan = _plan(args.controlled_report, args.verifier_image_id)
    checksum = digest(plan)
    if not args.execute:
        private = {"source", "registry"}
        visible = {key: value for key, value in plan.items() if key not in private}
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": visible}, indent=2
            )
        )
        return 0
    _check(checksum == args.expected_plan_sha256, "P5_PAID_PLAN_CHANGED")
    report = {
        "status": "IN_PROGRESS",
        "stage": "PREFLIGHT",
        "plan_sha256": checksum,
        "execution_harness_verified": False,
        "provider_compatibility": "NOT_VERIFIED",
        "candidate_availability": "UNKNOWN",
        "workflow_completion": "NOT_VERIFIED",
        "business_quality": "NOT_MEASURED",
        "credential_handling": "TEMPORARY_LOCAL_COPY_OF_EXISTING_PROVIDER_KEY",
    }
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with lifecycle_lock(STATE):
            _check(
                _plan(args.controlled_report, args.verifier_image_id) == plan,
                "P5_PAID_PLAN_CHANGED",
            )
            _execute(plan, checksum, report)
        report.update(stage="RECORDED", execution_harness_verified=True)
        report.update(_outcomes(report["receipt"]["rows"], report["wire"]))
    except Exception as error:  # noqa: BLE001 - never print credentials or arbitrary exception text
        report.update(status="FAILED", failure_code=_safe(error))
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


def _worker_main(expected_digest):
    import sitecustomize

    output = Path("/p5-output")
    try:
        receipt = _worker(expected_digest)
        sitecustomize._write(output / "result.json", receipt)
        return 0
    except Exception as error:  # noqa: BLE001 - never print credentials or arbitrary exception text
        sitecustomize._write(output / "error.json", {"code": _safe(error)})
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        raise SystemExit(_worker_main(sys.argv[2]))
    raise SystemExit(main())
