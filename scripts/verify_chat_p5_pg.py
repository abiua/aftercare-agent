"""Owned PostgreSQL/S3 P5 worker-core acceptance with a controlled transport."""

import argparse
import json
import os
import re
import stat
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/offline-browser-demo-p5-pg.json"
PORT, OBJECT_PORT = 5200, 58362
BACKEND = "aftercare-backend:chat-p5-pg-20261111"
FRONTEND = "aftercare-frontend:chat-p5-pg-20261111"
CAMPAIGN = "chat-p5-controlled-pg-v1"
CASES = ("DAMAGE-ORDINARY", "DAMAGE-TWO-LINE-PACKAGE-CONFLICT")
ARMS = {"strong_single": "SINGLE", "single_staged": "STAGED", "multi": "MULTI"}
FILES = (
    "scripts/verify_chat_p5_pg.py",
    "backend/tests/business/chat_eval_development_fixtures.py",
    "data/chat-eval-v1/development/cases.json",
    "data/v0.1/agent/business.json",
    "data/vision-v1/manifest.json",
    "data/vision-v1/synthetic-cracked-mug.png",
    "data/vision-v1/synthetic-torn-parcel.png",
)
BOUNDARY = ("CONTROLLED_WORKER_CORE", "NO_PROVIDER_CALLS", "NO_INDEPENDENT_LABELS")


def _check(condition, code):
    if not condition:
        raise ValueError(code)


def _safe_worker_code(value):
    if re.fullmatch(r"(?:P5_PG_|CHAT_EVAL_|DATABASE_)[A-Z0-9_]{1,80}", value):
        return value
    if value in {"ValueError", "AssertionError", "TypeError", "KeyError", "RuntimeError"}:
        return value
    return None


def _controlled_transport(calls, *, paired_recheck_fixture=False):
    from langchain_core.messages import AIMessage, ToolMessage

    complex_fact_drafts = 0

    def reply(payload):
        usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}
        if isinstance(payload, list):
            return AIMessage(content="", tool_calls=payload, usage_metadata=usage)
        return AIMessage(content=json.dumps(payload, ensure_ascii=False), usage_metadata=usage)

    def tool(name, arguments):
        return {"name": name, "id": name, "args": arguments}

    def claim(ident, evidence, value):
        return {
            "id": ident,
            "subject": ident,
            "predicate": "value",
            "state": "SUPPORTED",
            "supporting_refs": [evidence],
            "value": value,
        }

    def transport(spec, messages, _tools, _timeout, _output):
        nonlocal complex_fact_drafts
        role = messages[0].content.split("。", 1)[0].split()[-1]
        calls.append((spec.model, role))
        prompt = json.loads(messages[1].content)
        view = prompt["frozen_input"]
        upstream = prompt.get("upstream_claims", [])
        available = view.get("available_evidence", [])
        complex_case = (
            any(row[:2] == ["ORDER_LINE", "L02"] for row in available)
            or len(view.get("policy_context", {}).get("products", [])) == 2
            or any(claim_item["id"].endswith("/package") for claim_item in upstream)
        )
        line_id = "L02" if complex_case else "L01"
        fact_id = "package" if complex_case else "amount"
        actual = "TEST-PARCEL-B17" if complex_case else "6000"
        ref = "POL14:1.0"
        line_tool = tool(
            "read_order_lines", {"line_ids": ["L01", "L02"] if complex_case else [line_id]}
        )
        policy_tool = tool(
            "read_policy_clause",
            {"policy_id": "POL14", "version": "1.0", "product_id": f"DEV-MUG-{line_id[-1]}"},
        )
        evidence_option = {
            "id": "request-binding",
            "action": "REQUEST_EVIDENCE",
            "target_line_ids": ["L01", "L02"],
            "evidence_ids": [line_id],
            "missing_evidence": ["请提供受损商品与对应包裹标签的绑定证据。"],
            "rationale": "包裹编号核对完成后，仍无法确认照片中的受损商品归属。",
        }
        refund_option = {
            "id": "refund",
            "action": "REFUND",
            "lines": [{"line_id": "L01", "amount_cents": 6000}],
            "policy_ids": ["POL14"],
            "evidence_ids": ["L01", ref],
            "rationale": "订单唯一一件实付60元，客户确认该件破损并仅申请退该件。",
        }
        options = [evidence_option if complex_case else refund_option]
        if role == "strong_single":
            if not isinstance(messages[-1], ToolMessage):
                return reply([line_tool, policy_tool])
            return reply(
                {
                    "claims": [claim(fact_id, line_id, actual), claim("clause", ref, "适用")],
                    "options": options,
                }
            )
        if role == "fact":
            if "check_receipt" in view:
                return reply({"claims": [claim("package", "L02", "TEST-PARCEL-B17")]})
            if not isinstance(messages[-1], ToolMessage):
                return reply([line_tool])
            if not complex_case:
                return reply({"claims": [claim(fact_id, line_id, actual)]})
            # Manifest orders STAGED before MULTI. Only the latter receives an injected mismatch.
            if paired_recheck_fixture:
                value = "TEST-PARCEL-A17"
            else:
                complex_fact_drafts += 1
                value = "TEST-PARCEL-A17" if complex_fact_drafts == 2 else actual
            return reply(
                {
                    "claims": [
                        claim("unaffected-package", "L01", "TEST-PARCEL-A17"),
                        claim("package", "L02", value),
                    ]
                }
            )
        if role == "policy":
            if not isinstance(messages[-1], ToolMessage):
                return reply([policy_tool])
            return reply({"claims": [claim("clause", ref, "适用")]})
        _check(role == "verifier", "P5_PG_UNEXPECTED_FIXTURE_ROLE")
        fact = next(item for item in upstream if item["id"] == f"fact/{fact_id}")
        mismatch = complex_case and fact["value"] != actual
        assessments = [
            {
                "claim_id": item["id"],
                "contradicted_field": "value",
                "verdict": "CONTRADICTED" if mismatch and item is fact else "CONFIRMED",
                "evidence_refs": item["supporting_refs"],
            }
            for item in upstream
        ]
        if mismatch:
            return reply(
                {
                    "assessments": assessments,
                    "recheck": {
                        "target_role": "fact",
                        "claim_ids": ["fact/package"],
                        "tool_name": "read_order_lines",
                        "arguments": {"line_ids": ["L02"]},
                        "reason": "用绑定订单行原始记录重读L02包裹编号。",
                    },
                }
            )
        return reply({"assessments": assessments, "options": options})

    return transport


def _worker(staged, output, stack_id, system_id, *, recover=False):
    from dataclasses import asdict
    from decimal import Decimal
    from secrets import token_bytes
    from uuid import NAMESPACE_URL, uuid5

    from aftercare.business.rules import digest
    from aftercare.business.storage import S3ObjectStore
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.evaluation.chat.manifest import Manifest, bound_rows
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import ChatEvaluationRunner, _freeze_case
    from aftercare.evaluation.runner import _namespace
    from aftercare.intelligence.models import ModelRunner, Registry
    from sqlalchemy import text

    sys.path.insert(0, str(staged / "backend/tests/business"))
    from chat_eval_development_fixtures import load_development_cases, replay_development_case

    settings = load_settings("test")
    _check(
        os.environ.get("AFTERCARE_PROFILE") == settings.profile == "test"
        and settings.container
        and not any(
            (
                settings.model_enabled,
                settings.chat_vision_enabled,
                settings.embedding_enabled,
                settings.chat_analysis_enabled,
            )
        ),
        "P5_PG_REAL_OFFLINE_TEST_REQUIRED",
    )
    public = guarded_engine(settings, test_only=True)
    tables = (
        "agent_runs",
        "agent_model_attempts",
        "business_executions",
        "chat_model_attempts",
        "intake_model_audits",
        "checkpoints",
        "checkpoint_writes",
        "checkpoint_blobs",
        "checkpoint_migrations",
    )

    def physical_readback(rows, frozen_sources):
        result = []
        for row in rows:
            schema = _namespace(row["id"])
            frozen = frozen_sources[row["case_id"]]
            with public.connect() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                checkpoints = db.execute(
                    text(f'SELECT DISTINCT thread_id, checkpoint_id FROM "{schema}".checkpoints')
                ).all()
                _check(
                    {item.thread_id for item in checkpoints} == {row["thread_id"]}
                    and row["checkpoint_id"] in {item.checkpoint_id for item in checkpoints},
                    "P5_PG_CROSS_ARM_CHECKPOINT",
                )
                for table in ("checkpoint_migrations", "checkpoint_blobs", "checkpoint_writes"):
                    _check(
                        db.scalar(
                            text("SELECT to_regclass(:relation)"), {"relation": f"{schema}.{table}"}
                        ),
                        "P5_PG_ARM_CHECKPOINT_TABLES",
                    )
            readback = {}
            for category, objects in (
                ("originals", frozen.images),
                ("typed_materials", frozen.materials),
            ):
                readback[category] = []
                for metadata, key, expected in objects:
                    physical_key = f"evaluation/{schema}/{key}"
                    content = store.get(physical_key, metadata["size"])
                    _check(
                        content == expected and sha256(content).hexdigest() == metadata["sha256"],
                        "P5_PG_ARM_OBJECT_MISMATCH",
                    )
                    readback[category].append(
                        {"sha256": metadata["sha256"], "size": metadata["size"]}
                    )
            result.append({"row_id": row["id"], "schema": schema, **readback})
        return result

    def public_state():
        with public.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            actual = db.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))
            _check(actual == system_id, "P5_PG_SYSTEM_ID_MISMATCH")
            return {
                table: db.execute(
                    text(
                        f"SELECT count(*), md5(coalesce(string_agg(datum, ',' ORDER BY datum), '')) "
                        f"FROM (SELECT to_jsonb(t)::text AS datum FROM public.{table} t) rows"
                    )
                )
                .one()
                ._asdict()
                for table in tables
            }

    def recovery_sources(db, store, pack, attestation):
        schemas = {}
        for case in pack["cases"]:
            case_id = case["id"]
            if case_id in CASES:
                schema = _namespace(
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            digest(["p5-source-v1", CAMPAIGN, case_id, 1]),
                        )
                    )
                )
                schemas[schema] = {
                    "version": "p5-pg-owner-v1",
                    "attestation_sha256": digest(asdict(attestation)),
                    "campaign": CAMPAIGN,
                    "namespace": schema,
                    "object_prefix": f"evaluation/{schema}/",
                    "source_case": case_id,
                    "repetition": 1,
                    "source_sha256": digest([pack, case]),
                }
        expected = set(
            db.scalars(
                text(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname ~ '^eval_[a-f0-9]{32}$'"
                )
            )
        )
        _check(expected == schemas.keys(), "P5_PG_RECOVERY_UNEXPECTED_SCHEMA")
        for schema, claim in schemas.items():
            owner = db.execute(text(f'SELECT claim, ready FROM "{schema}".p5_owner')).one()
            _check(owner.claim == claim and owner.ready, "P5_PG_RECOVERY_OWNER_MISMATCH")
            object_claim = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()
            prefix = claim["object_prefix"]
            _check(
                store.get(prefix + ".p5-owner.json", len(object_claim)) == object_claim,
                "P5_PG_RECOVERY_OBJECT_OWNER_MISMATCH",
            )
            listed = store.client.list_objects_v2(Bucket=store.bucket, Prefix=prefix)
            _check(
                listed.get("KeyCount") == 5 and not listed.get("IsTruncated"),
                "P5_PG_RECOVERY_OBJECT_COUNT_MISMATCH",
            )
            for table in (
                "agent_runs",
                "agent_model_attempts",
                "chat_model_attempts",
                "business_executions",
                "chat_decision_snapshots",
            ):
                _check(
                    db.scalar(text(f'SELECT count(*) FROM "{schema}".{table}')) == 0,
                    "P5_PG_RECOVERY_SOURCE_NOT_PRISTINE",
                )
            _check(
                db.scalar(
                    text("SELECT to_regclass(:relation)"), {"relation": f"{schema}.checkpoints"}
                )
                is None,
                "P5_PG_RECOVERY_SOURCE_HAS_CHECKPOINTS",
            )
        listed = store.client.list_objects_v2(Bucket=store.bucket, Prefix="evaluation/")
        keys = [item["Key"] for item in listed.get("Contents", [])]
        sentinel = sentinel_key(attestation.stack_id)
        _check(
            not listed.get("IsTruncated")
            and len(keys) == 1 + 5 * len(schemas)
            and sentinel in keys
            and all(
                key == sentinel
                or any(key.startswith(claim["object_prefix"]) for claim in schemas.values())
                for key in keys
            ),
            "P5_PG_RECOVERY_UNEXPECTED_OBJECT_PREFIX",
        )
        return sorted(schemas)

    try:
        before = public_state()
        store = S3ObjectStore(settings)
        key = sentinel_key(stack_id)
        if recover:
            sentinel = store.get(key, 64)
            _check(len(sentinel) == 64, "P5_PG_RECOVERY_SENTINEL_SIZE")
        else:
            sentinel = token_bytes(64)
            response = store.client.list_objects_v2(Bucket=store.bucket, Prefix=key)
            _check(not response.get("Contents"), "P5_PG_SENTINEL_ALREADY_EXISTS")
            store.put(key, sentinel, "application/octet-stream")
            _check(store.get(key, len(sentinel)) == sentinel, "P5_PG_SENTINEL_MISMATCH")
        attestation = PgAttestation(
            stack_id=stack_id,
            system_identifier=system_id,
            bucket=store.bucket,
            sentinel_sha256=sha256(sentinel).hexdigest(),
            sentinel_size=len(sentinel),
        )
        pack = load_development_cases(staged)
        recovered_sources = None
        calls = []
        registry = Registry.model_validate(
            {
                "version": "chat-p5-pg-controlled",
                "allow_unverified": True,
                "models": [
                    {
                        "alias": "analysis",
                        "provider": "fixture",
                        "model": "controlled",
                        "endpoint": "http://127.0.0.1:1/v1",
                        "api_key_env": "UNUSED_FIXTURE",
                        "input_usd_per_million": "1",
                        "output_usd_per_million": "1",
                        "price_date": "2026-11-11",
                        "source": "synthetic",
                        "context_tokens": 32000,
                        "data_scopes": ["customer_conversation"],
                        "output_tokens": 1800,
                    }
                ],
            }
        )
        runner = ChatEvaluationRunner(
            ModelRunner(registry, transport=_controlled_transport(calls)),
            None,
            calls=8,
            budget=Decimal("0.10"),
        )
        manifest = Manifest(output / "manifest")

        def run_once(*, expecting_fresh, campaign=CAMPAIGN, selected=CASES, journal=manifest):
            nonlocal recovered_sources
            cases = []
            frozen_sources = {}
            with open_pg_runtime(settings, attestation, campaign) as runtime:
                if recover and campaign == CAMPAIGN and recovered_sources is None:
                    _check(
                        all(
                            before[table]["count"] == 0
                            for table in (
                                "agent_runs",
                                "agent_model_attempts",
                                "chat_model_attempts",
                                "intake_model_audits",
                                "business_executions",
                            )
                        ),
                        "P5_PG_RECOVERY_PUBLIC_NOT_PRISTINE",
                    )
                    with public.connect() as db, db.begin():
                        db.execute(text("SET TRANSACTION READ ONLY"))
                        recovered_sources = recovery_sources(db, store, pack, attestation)
                if campaign == CAMPAIGN and not calls:
                    try:
                        with open_pg_runtime(settings, attestation, campaign):
                            raise ValueError("P5_PG_COMPETING_CAMPAIGN_ALLOWED")
                    except ValueError as error:
                        _check(
                            str(error) == "CHAT_EVAL_PG_CAMPAIGN_BUSY", "P5_PG_LEASE_CHECK_FAILED"
                        )
                for case_id in selected:
                    definition = next(item for item in pack["cases"] if item["id"] == case_id)
                    with runtime.source(case_id, 1, digest([pack, definition])) as (
                        engine,
                        storage,
                        fresh,
                    ):
                        _check(fresh is expecting_fresh, "P5_PG_SOURCE_REPLAY_STATE")
                        case = replay_development_case(
                            engine,
                            storage,
                            runtime.settings,
                            staged,
                            case_id,
                            fresh=fresh,
                        )
                        cases.append(case)
                    frozen_sources[case_id] = _freeze_case(case)
                receipt = runtime.run(journal, runner, cases)
            return receipt, frozen_sources

        first, frozen_before = run_once(expecting_fresh=not recover)
        count = len(calls)
        resumed, frozen_after = run_once(expecting_fresh=False)
        _check(
            resumed == first and len(calls) == count,
            "P5_PG_RESUME_REPEAT_ATTEMPT",
        )
        _check(
            all(
                frozen_before[case_id].scope == frozen_after[case_id].scope
                and frozen_before[case_id].images == frozen_after[case_id].images
                and frozen_before[case_id].materials == frozen_after[case_id].materials
                for case_id in CASES
            ),
            "P5_PG_SOURCE_CHANGED",
        )
        _check(count == sum(len(row["attempts"]) for row in first["rows"]), "P5_PG_ATTEMPT_COUNT")
        _check(len(first["rows"]) == len(CASES) * len(ARMS), "P5_PG_ROW_COUNT")
        _check(
            all(
                "latency_issue" in row
                and row["latency_issue"] is None
                and all(
                    type(row.get(field)) is int and row[field] >= 0
                    for field in ("queued_ms", "worker_ms", "candidate_persisted_ms")
                )
                for row in first["rows"]
            ),
            "P5_PG_LATENCY_INVALID",
        )
        _check(all(row.get("route") == ARMS[row["arm"]] for row in first["rows"]), "P5_PG_ROUTE")
        _check(
            all(
                row.get("checkpoint_persisted")
                and row.get("checkpoint_id")
                and row.get("thread_id")
                and row.get("candidate_status") == "AVAILABLE"
                for row in first["rows"]
            ),
            "P5_PG_PERSISTENCE",
        )
        for case_id in CASES:
            rows = [row for row in first["rows"] if row["case_id"] == case_id]
            _check(len({row["input_sha256"] for row in rows}) == 1, "P5_PG_SCOPE")
        _check(
            len({row["thread_id"] for row in first["rows"]}) == len(first["rows"]), "P5_PG_THREADS"
        )
        complex_multi = next(
            row for row in first["rows"] if row["case_id"] == CASES[1] and row["arm"] == "multi"
        )
        _check(
            [item["claim_ids"] for item in complex_multi["rechecks"]] == [["fact/package"]],
            "P5_PG_CONTROLLED_RECHECK",
        )

        class ReadyInterruption(BaseException):
            pass

        class InterruptedManifest(Manifest):
            def mark_ready(self, row, namespace):
                super().mark_ready(row, namespace)
                if row["arm"] == "multi":
                    raise ReadyInterruption

        recovery_campaign = CAMPAIGN + "-recovery"
        interrupted = InterruptedManifest(output / "interruption-manifest")
        try:
            run_once(
                expecting_fresh=True,
                campaign=recovery_campaign,
                selected=CASES[:1],
                journal=interrupted,
            )
        except ReadyInterruption:
            pass
        else:
            raise ValueError("P5_PG_READY_INTERRUPTION_MISSING")
        baseline_calls = len(calls)
        registered = bound_rows(interrupted.read("plan.json"))
        target = registered[-1]
        _check(
            target["arm"] == "multi"
            and interrupted.started(target)
            and interrupted.ready(target)
            and interrupted.result(target) is None,
            "P5_PG_INTERRUPTED_JOURNAL_STATE",
        )
        recovery_journal = Manifest(interrupted.directory)
        recovered, _ = run_once(
            expecting_fresh=False,
            campaign=recovery_campaign,
            selected=CASES[:1],
            journal=recovery_journal,
        )
        repeated, _ = run_once(
            expecting_fresh=False,
            campaign=recovery_campaign,
            selected=CASES[:1],
            journal=recovery_journal,
        )
        interrupted_row = recovered["rows"][-1]
        _check(
            recovered == repeated
            and len(calls) == baseline_calls
            and interrupted_row["id"] == target["id"]
            and interrupted_row["status"] == "UNKNOWN"
            and interrupted_row["known_estimated_usd"] is None
            and not interrupted_row["attempts"],
            "P5_PG_INTERRUPTION_RESEND",
        )
        readback = physical_readback(first["rows"], frozen_before)
        _check(public_state() == before, "P5_PG_PUBLIC_ROWS_CHANGED")
        return {
            "boundary": list(BOUNDARY),
            "system_identifier": system_id,
            "object_sentinel_sha256": attestation.sentinel_sha256,
            "recovery_existing_sources": recovered_sources,
            "recovery_reused_existing_sentinel": recover,
            "public_counts_and_hashes_unchanged": True,
            "source_scope_original_and_typed_material_bytes_unchanged": True,
            "reopened_campaign_issued_no_new_controlled_attempts": True,
            "competing_campaign_lease_rejected": True,
            "controlled_interruption": {
                "injection_boundary": "AFTER_READY_BEFORE_AGENT_START",
                "plan_sha256": recovered["plan_sha256"],
                "row_id": interrupted_row["id"],
                "status": interrupted_row["status"],
                "known_estimated_usd": interrupted_row["known_estimated_usd"],
                "additional_attempts_on_two_reopens": 0,
            },
            "arm_checkpoint_and_original_readback": readback,
            "controlled_attempts": count,
            "receipt": first,
        }
    finally:
        public.dispose()


def _host_plan(images):
    from unittest.mock import patch

    import verify_chat_p3_offline as p3

    with patch.multiple(
        p3,
        STATE=STATE,
        PORT=PORT,
        OBJECT_PORT=OBJECT_PORT,
        IMAGES={tag: value.removeprefix("sha256:") for tag, value in images.items()},
    ):
        plan = p3._preflight()
    return {
        **plan,
        "scope": "P5_CONTROLLED_PG_S3_WORKER_CORE",
        "chat_analysis_enabled": False,
        "chat_investigation_enabled": True,
        "maximum_simulated_executions": 0,
        "profile": "test",
        "state_file": STATE.name,
        "campaign": CAMPAIGN,
        "independent_recovery_campaign": CAMPAIGN + "-recovery",
        "cases": list(CASES),
        "arms": list(ARMS),
        "boundary": list(BOUNDARY),
        "sources": {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES},
    }


def _failed_manifest_hash(path):
    from aftercare.business.rules import digest

    directory = path.lstat()
    _check(
        stat.S_ISDIR(directory.st_mode)
        and directory.st_uid == os.getuid()
        and directory.st_mode & 0o077 == 0
        and {item.name for item in path.iterdir()} == {"lock"},
        "P5_PG_RECOVERY_PRIOR_JOURNAL_NOT_EMPTY",
    )
    lock = path / "lock"
    entry = lock.lstat()
    _check(
        stat.S_ISREG(entry.st_mode) and entry.st_uid == os.getuid() and entry.st_mode & 0o077 == 0,
        "P5_PG_RECOVERY_PRIOR_JOURNAL_LOCK_REJECTED",
    )
    return digest(
        [
            [directory.st_dev, directory.st_ino, directory.st_mode, directory.st_mtime_ns],
            [
                "lock",
                entry.st_dev,
                entry.st_ino,
                entry.st_mode,
                entry.st_mtime_ns,
                sha256(lock.read_bytes()).hexdigest(),
            ],
        ]
    )


def _recover_plan(images, failed_report, verifier_image):
    import offline_demo
    from aftercare.business.rules import digest
    from verify_streaming_compatibility import _private_json

    failed = _private_json(failed_report)
    state = _private_json(STATE)
    _check(
        failed["status"] == "FAILED"
        and failed["stage"] == "CONTROLLED_RUN"
        and failed["failure_code"] == "P5_PG_CONTROLLED_RUN_FAILED"
        and failed["plan_sha256"] == digest(failed["plan"])
        and failed["plan"]["scope"] == "P5_CONTROLLED_PG_S3_WORKER_CORE"
        and failed["plan"]["campaign"] == CAMPAIGN
        and failed["plan"]["images"] == images
        and state["status"] == "READY"
        and state["profile"] == "test"
        and failed["owned_stack"] == state["run_id"]
        and failed["owned_url"] == state["url"] == f"http://127.0.0.1:{PORT}"
        and verifier_image != images[BACKEND],
        "P5_PG_RECOVERY_PRIOR_RUN_REJECTED",
    )
    stack = offline_demo.restore(state)
    old_manifest = stack.directory / "p5-pg-output" / "manifest"
    _check(
        Path(failed["private_manifest_directory"]) == old_manifest,
        "P5_PG_RECOVERY_PRIOR_JOURNAL_PATH",
    )
    prior_hash = _failed_manifest_hash(old_manifest)
    _check(
        not (stack.directory / "p5-pg-recovery-input").exists()
        and not (stack.directory / "p5-pg-recovery-output").exists(),
        "P5_PG_RECOVERY_ALREADY_PREPARED",
    )
    original_sources = failed["plan"]["sources"]
    _check(original_sources.keys() == set(FILES), "P5_PG_RECOVERY_SOURCE_SET")
    sources = {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    _check(
        all(sources[name] == original_sources[name] for name in FILES if name != FILES[0])
        and all(
            sha256((stack.directory / "p5-pg-input" / name).read_bytes()).hexdigest()
            == original_sources[name]
            for name in FILES
        ),
        "P5_PG_RECOVERY_SOURCE_CHANGED",
    )
    endpoint = stack.docker(
        "P5_PG_LOCAL_CONTEXT",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        log_output=False,
    ).stdout.strip()
    _check(
        endpoint.startswith("unix://")
        and (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")),
        "P5_PG_LOCAL_DOCKER_REQUIRED",
    )
    _, system_id = _topology(stack, images)
    observed = stack.docker(
        "P5_PG_RECOVERY_IMAGE",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        verifier_image,
        log_output=False,
    ).stdout.strip()
    _check(observed == verifier_image, "P5_PG_RECOVERY_IMAGE_MISMATCH")
    return {
        **failed["plan"],
        "scope": "P5_CONTROLLED_PG_S3_RECOVERY_FROM_PREPLAN_FAILURE",
        "sources": sources,
        "verifier_image": verifier_image,
        "recovery": {
            "failed_report": str(failed_report.resolve()),
            "failed_report_sha256": sha256(failed_report.read_bytes()).hexdigest(),
            "stack_id": stack.run_id,
            "system_identifier": system_id,
            "prior_manifest_directory": str(old_manifest),
            "prior_manifest_sha256": prior_hash,
        },
    }


def _topology(stack, images):
    from verify_chat_backup_restore import published_port
    from verify_fresh_bootstrap import IMAGES

    def info(kind, suffix, fields):
        name = f"aftercare-fresh-{stack.run_id}-{suffix}"
        _check(
            (kind, name) in stack.resources and stack.owned(kind, name),
            "P5_PG_RESOURCE_OWNERSHIP",
        )
        result = stack.docker(
            "P5_TOPOLOGY_INSPECT",
            kind,
            "inspect",
            "--format",
            fields,
            name,
            log_output=False,
        ).stdout.strip()
        return name, json.loads(result)

    def image_id(tag):
        return stack.docker(
            "P5_IMAGE_ID", "image", "inspect", "--format", "{{.Id}}", tag, log_output=False
        ).stdout.strip()

    network, internal = info("network", "net", "{{json .Internal}}")
    ingress, _ = info("network", "ingress", "{{json .Internal}}")
    pg_volume, _ = info("volume", "pg-data", "{{json .Name}}")
    objects_volume, _ = info("volume", "objects-data", "{{json .Name}}")
    _check(internal is True, "P5_PG_NETWORK_NOT_INTERNAL")
    expected = {
        "api": (images[BACKEND], {network}, None),
        "worker": (images[BACKEND], {network}, None),
        "beat": (images[BACKEND], {network}, None),
        "pg": (image_id(IMAGES["pg"]), {network}, pg_volume),
        "objects": (image_id(IMAGES["objects"]), {network, ingress}, objects_volume),
        "redis": (image_id(IMAGES["redis"]), {network}, None),
        "frontend": (images[FRONTEND], {network, ingress}, None),
    }
    names = {}
    for role, (expected_image, expected_networks, volume) in expected.items():
        name, actual_image = info("container", role, "{{json .Image}}")
        _, networks = info("container", role, "{{json .NetworkSettings.Networks}}")
        _, mounts = info("container", role, "{{json .Mounts}}")
        _check(
            actual_image == expected_image
            and networks.keys() == expected_networks
            and (volume is None or any(item.get("Name") == volume for item in mounts)),
            "P5_PG_TOPOLOGY_MISMATCH",
        )
        names[role] = name
    for suffix, net in (("net", network), ("ingress", ingress)):
        _, attached = info("network", suffix, "{{json .Containers}}")
        _check(
            {row["Name"] for row in attached.values()}
            == {names[role] for role, (_, networks, _) in expected.items() if net in networks},
            "P5_PG_NETWORK_MEMBERSHIP_MISMATCH",
        )
    _check(
        published_port(stack, names["frontend"], 8080) == PORT
        and published_port(stack, names["objects"], 8333) == OBJECT_PORT,
        "P5_PG_PORT_BINDING",
    )
    system_id = stack.docker(
        "P5_PG_SYSTEM_ID",
        "exec",
        names["pg"],
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U ac_test -d ac_test -XAt '
        '-v ON_ERROR_STOP=1 -c "SELECT system_identifier::text FROM pg_control_system()"',
        log_output=False,
    ).stdout.strip()
    _check(re.fullmatch(r"[0-9]{10,25}", system_id), "P5_PG_SYSTEM_ID_INVALID")
    return network, system_id


def _execute(plan, report):
    from types import SimpleNamespace
    from unittest.mock import patch

    import offline_demo
    from verify_chat_p3_offline import P3Stack
    from verify_streaming_compatibility import _private_json

    class Stack(P3Stack):
        def __init__(self, directory):
            super().__init__(directory)
            config_path = self.runtime / "local.json"
            config = _private_json(config_path)
            config["app"].update(chat_investigation_enabled=True, chat_analysis_enabled=False)
            config_path.write_text(json.dumps(config))

    images = plan["images"]
    recovering = plan.get("recovery")
    with patch.multiple(offline_demo, STATE=STATE, FreshStack=Stack):
        if not recovering:
            _check(not STATE.exists(), "P5_PG_STATE_ALREADY_EXISTS")
            offline_demo.start(
                SimpleNamespace(
                    port=PORT,
                    object_port=OBJECT_PORT,
                    profile="test",
                    backend_image=images[BACKEND],
                    frontend_image=images[FRONTEND],
                )
            )
        state = _private_json(STATE)
        stack = offline_demo.restore(state)
    _check(
        state["profile"] == "test" and state["url"] == f"http://127.0.0.1:{PORT}",
        "P5_PG_OWNED_STATE_MISMATCH",
    )
    network, system_id = _topology(stack, images)
    if recovering:
        _check(
            system_id == recovering["system_identifier"] and stack.run_id == recovering["stack_id"],
            "P5_PG_RECOVERY_STACK_CHANGED",
        )
    directory = "p5-pg-recovery" if recovering else "p5-pg"
    staged = stack.directory / f"{directory}-input"
    output = stack.directory / f"{directory}-output"
    staged.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    for name, expected in plan["sources"].items():
        source = (ROOT / name).read_bytes()
        _check(sha256(source).hexdigest() == expected, "P5_PG_FIXTURE_CHANGED")
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
        target.chmod(0o400)
    report.update(
        stage="CONTROLLED_RUN",
        owned_stack=state["run_id"],
        owned_url=state["url"],
        private_manifest_directory=str(output / "manifest"),
    )
    verifier = f"aftercare-fresh-{stack.run_id}-p5-pg-verifier"
    state["resources"].append(["container", verifier])
    with patch.object(offline_demo, "STATE", STATE):
        offline_demo.save(state)
    try:
        completed = stack.docker(
            "P5_PG_CONTROLLED_RUN",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            verifier,
            "--label",
            f"com.aftercare.fresh-bootstrap={stack.run_id}",
            "--network",
            network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={staged},target=/acceptance,readonly",
            "--mount",
            f"type=bind,source={output},target=/acceptance-output",
            "--entrypoint",
            "python",
            plan.get("verifier_image", images[BACKEND]),
            "/acceptance/scripts/verify_chat_p5_pg.py",
            "_worker",
            "/acceptance",
            "/acceptance-output",
            stack.run_id,
            system_id,
            *(("recover",) if recovering else ()),
            timeout=300,
            required=False,
            log_output=False,
        )
        if completed is not None and completed.returncode != 0:
            for line in reversed(completed.stderr.splitlines()):
                code = _safe_worker_code(line.strip())
                if code is not None:
                    report["worker_error_code"] = code
                    break
        _check(completed is not None and completed.returncode == 0, "P5_PG_CONTROLLED_RUN_FAILED")
        report["acceptance"] = json.loads(completed.stdout.strip().splitlines()[-1])
    finally:
        owned = stack.owned("container", verifier)
        _check(owned is not False, "P5_PG_VERIFIER_OWNER_MISMATCH")
        if owned:
            stack.docker("P5_REMOVE_VERIFIER", "rm", "-f", verifier)
        _check(stack.owned("container", verifier) is None, "P5_PG_VERIFIER_STILL_EXISTS")
        state["resources"].remove(["container", verifier])
        with patch.object(offline_demo, "STATE", STATE):
            offline_demo.save(state)


def main():
    from aftercare.business.rules import digest
    from offline_demo import lifecycle_lock
    from verify_fresh_bootstrap import VerificationError
    from verify_streaming_compatibility import _save

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--failed-report", type=Path)
    parser.add_argument("--verifier-image-id")
    args = parser.parse_args()
    if bool(args.failed_report) != bool(args.verifier_image_id):
        parser.error("recovery requires --failed-report and --verifier-image-id together")
    if args.verifier_image_id and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.verifier_image_id):
        parser.error("supply an immutable recovery verifier image ID")
    images = {BACKEND: args.backend_image_id, FRONTEND: args.frontend_image_id}
    if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in images.values()):
        parser.error("supply immutable image IDs")
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires the reviewed plan digest and a new report path")
    plan = (
        _recover_plan(images, args.failed_report, args.verifier_image_id)
        if args.failed_report
        else _host_plan(images)
    )
    checksum = digest(plan)
    if not args.execute:
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
            )
        )
        return 0
    _check(checksum == args.expected_plan_sha256, "P5_PG_PLAN_CHANGED")
    report = {"status": "IN_PROGRESS", "stage": "STARTING", "plan_sha256": checksum, "plan": plan}
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with lifecycle_lock(STATE):
            if args.failed_report:
                _check(
                    _recover_plan(images, args.failed_report, args.verifier_image_id) == plan,
                    "P5_PG_RECOVERY_PLAN_CHANGED",
                )
            try:
                _execute(plan, report)
            finally:
                if args.failed_report:
                    old_manifest = Path(plan["recovery"]["prior_manifest_directory"])
                    report["prior_manifest_unchanged"] = False
                    report["prior_manifest_unchanged"] = (
                        _failed_manifest_hash(old_manifest)
                        == plan["recovery"]["prior_manifest_sha256"]
                    )
                    _check(
                        report["prior_manifest_unchanged"], "P5_PG_RECOVERY_PRIOR_JOURNAL_CHANGED"
                    )
                    report["prior_failed_report_unchanged"] = (
                        sha256(args.failed_report.read_bytes()).hexdigest()
                        == plan["recovery"]["failed_report_sha256"]
                    )
                    _check(
                        report["prior_failed_report_unchanged"],
                        "P5_PG_RECOVERY_PRIOR_REPORT_CHANGED",
                    )
        report.update(status="PASSED", stage="VERIFIED")
    except (ValueError, VerificationError, OSError) as error:
        code = str(error)
        report.update(
            status="FAILED",
            failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
        )
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        try:
            result = _worker(
                Path(sys.argv[2]),
                Path(sys.argv[3]),
                sys.argv[4],
                sys.argv[5],
                recover=sys.argv[6:] == ["recover"],
            )
            print(json.dumps(result))
        except Exception as error:  # noqa: BLE001 - never emit DB/credential-bearing exception text
            code = _safe_worker_code(str(error)) or _safe_worker_code(type(error).__name__)
            print(code or "P5_PG_WORKER_UNCLASSIFIED", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())
