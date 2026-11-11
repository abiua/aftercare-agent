"""Admit one exact two-image P2 attempt on the owned 5202 test stack."""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
from time import monotonic, sleep
from unittest.mock import patch
from uuid import UUID

import httpx
import offline_demo
from aftercare.business.rules import digest
from aftercare.intelligence import models, vision
from verify_chat_backup_restore import private_json
from verify_chat_p1_http import call, message, read, request_ids
from verify_chat_p2_offline import _cards, owned_container
from verify_chat_p2_vision_worker import CAP, OUTPUT
from verify_chat_p5_multimodal_source import (
    BACKEND,
    FRONTEND,
    GATE,
    OBJECT_PORT,
    PORT,
    ROOT,
    STATE,
    URL,
    _inputs,
)
from verify_fresh_bootstrap import VerificationError, private_write, require
from verify_material_review_migration import _docker
from verify_streaming_compatibility import _configuration, _save

SCOPE = "P2_ISOLATED_P5_EXACT_TWO_IMAGE_V1"
PRIVATE_STAGE = "multimodal-vision-v1"
MOUNT = Path("/vision-wire")


def _sha(path):
    return sha256(path.read_bytes()).hexdigest()


def _owner_client(stack):
    (owner,) = [
        row
        for row in private_json(stack.credentials_path)
        if row["role"] == "customer" and row["customer_id"] == "C001"
    ]
    return httpx.Client(
        base_url=URL + "/api/",
        trust_env=False,
        timeout=12,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + owner["token"]},
    )


def _native(stack, api, plan, *, expire=False):
    identifiers = {
        key: plan[key] for key in ("conversation_id", "case_id", "issue_id", "system_identifier")
    }
    return stack.json_exec(
        "MULTIMODAL_P2_NATIVE_READBACK",
        api,
        f"P={identifiers!r}\nEXPIRE={expire!r}\n"
        + """
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.conversations.analysis import scheduler
settings=load_settings('test')
assert settings.container and settings.chat_analysis_enabled
assert not any((settings.model_enabled,settings.chat_vision_enabled,settings.chat_investigation_enabled,settings.embedding_enabled))
engine=guarded_engine(settings,test_only=True)
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()')) == P['system_identifier']
    scope=db.execute(text(
        "SELECT active_case_id,analysis_issue_id,active_order_id,status FROM public.chat_conversations WHERE id=:conversation_id"
    ),P).one()
    assert tuple(scope)[:3]==(P['case_id'],P['issue_id'],'O9902')
    assert scope.status in ('OPEN','HUMAN_HANDOFF')
    turns=[dict(row) for row in db.execute(text(
        'SELECT id,state,reason,attachments,deadline::text,quiet_until::text, deadline <= now() AS expired '
        'FROM public.chat_analysis_turns WHERE conversation_id=:conversation_id ORDER BY generation'
    ),P).mappings()]
    attempts=[dict(row) for row in db.execute(text(
        'SELECT a.id AS attempt_id,t.id AS turn_id,t.state AS turn_state,a.state,a.error_code,'
        'a.reserved_microusd,a.estimated_cost_microusd,a.budget_violation,a.response_evidence '
        'FROM public.chat_model_attempts a JOIN public.chat_analysis_turns t ON t.id=a.turn_id '
        'WHERE t.conversation_id=:conversation_id'
    ),P).mappings()]
    observations=[dict(row) for row in db.execute(text(
        'SELECT o.id,o.turn_id,o.attachment_id,o.attempt_id,o.original_sha256,o.sent_sha256,'
        'o.transform_version,o.findings,o.certainty FROM public.chat_observations o '
        'JOIN public.chat_analysis_turns t ON t.id=o.turn_id WHERE t.conversation_id=:conversation_id'
    ),P).mappings()]
    tables=('chat_model_attempts','chat_observations','chat_decision_snapshots','agent_runs',
            'agent_model_attempts','intake_model_audits','business_approvals','business_executions',
            'business_simulated_payments')
    counts={name:db.scalar(text('SELECT count(*) FROM public.'+name)) for name in tables}
if EXPIRE:
    latest=turns[-1]
    assert latest['state']=='PENDING' and latest['expired'] and not any(counts.values())
    assert scheduler(engine,settings).claim(latest['id']) is None
print(json.dumps(dict(conversation_status=scope.status,turns=turns,attempts=attempts,observations=observations,counts=counts)))
engine.dispose()
""",
    )


def _bound_turn(turn, plan):
    return [row["id"] for row in turn["attachments"]] == plan["attachment_ids"] and [
        row["sha256"] for row in turn["attachments"]
    ] == plan["original_image_sha256s"]


def _plan(offline_path):
    import verify_chat_p5_pg as pg

    offline = private_json(offline_path)
    original = offline["plan"]
    state = private_json(STATE)
    require(
        offline["status"] == "OFFLINE_ORIGINALS_READY"
        and offline["plan_sha256"] == digest(original)
        and original["protocol"] == "p5-v7-live-source-offline-preparation-v1"
        and original["images"] == {"backend": BACKEND, "frontend": FRONTEND}
        and original["maximum_provider_calls"] == 0
        and original["profile"] == state["profile"] == "test"
        and state["status"] == "READY"
        and state["run_id"] == offline["owned_stack"]
        and state["url"] == offline["owned_url"] == URL
        and original["object_port"] == OBJECT_PORT
        and offline["beat_paused_for_pending_p2"]
        and _sha(GATE) == original["vision_gate_sha256"],
        "P5_MULTIMODAL_VISION_OFFLINE_REPORT_MISMATCH",
    )
    link = offline["reconciled_from"]
    failed_path = Path(link["path"])
    failed = private_json(failed_path)
    require(
        link["http_upload_replayed"] is False
        and _sha(failed_path) == link["sha256"]
        and failed["status"] == "FAILED"
        and all(
            failed[key] == offline[key]
            for key in (
                "plan",
                "plan_sha256",
                "owned_stack",
                "owned_url",
                "system_identifier",
                "conversation_id",
                "case_id",
                "request_ids",
            )
        ),
        "P5_MULTIMODAL_VISION_ORIGINAL_FAILURE_CHANGED",
    )
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "P5_MULTIMODAL_VISION_LOCAL_DOCKER_REQUIRED",
    )
    stack = offline_demo.restore(state)
    require(not (stack.directory / PRIVATE_STAGE).exists(), "P5_MULTIMODAL_VISION_ALREADY_PREPARED")
    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        network, system_id = pg._topology(stack, {pg.BACKEND: BACKEND, pg.FRONTEND: FRONTEND})
    beat = owned_container(stack, "beat")
    require(
        system_id == offline["system_identifier"]
        and stack.docker(
            "P2_BEAT_PAUSE", "inspect", "--format", "{{.State.Paused}}", beat, log_output=False
        ).stdout.strip()
        == "true",
        "P5_MULTIMODAL_VISION_TOPOLOGY_CHANGED",
    )
    _, assets, _, photos = _inputs()
    require(
        [row["sha256"] for row in assets] == original["original_image_sha256s"]
        and [photo.sent_sha256 for photo in photos] == original["sent_sha256s"]
        and original["transform_version"] == vision.TRANSFORM_VERSION,
        "P5_MULTIMODAL_VISION_INPUT_CHANGED",
    )
    spec = _configuration()["gpt-6.1-terra"]
    require(
        spec.alias == "economy"
        and spec.output_tokens == OUTPUT
        and spec.cache_write_usd_per_million is not None
        and spec.cache_write_ttl != "12h",
        "P5_MULTIMODAL_VISION_MODEL_NOT_ADMITTED",
    )
    prompt, _ = models._estimate_input(vision._messages(photos), [], vision=True)
    allowance = (spec.context_tokens - OUTPUT - prompt) // 2
    reserve = models._reserve_price(spec, spec.context_tokens, OUTPUT)
    require(
        allowance > 0 and reserve is not None and 0 < reserve <= CAP,
        "P5_MULTIMODAL_VISION_ESTIMATED_RESERVATION_REJECTED",
    )
    microusd = int((reserve * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    data = spec.model_dump(mode="json")
    data.update(
        vision=True,
        vision_checked_at="2026-11-11",
        vision_compatibility_version="p2-exact-two-images-v1",
        vision_input_tokens_per_image=allowance,
        data_scopes=["customer_attachment"],
    )
    registry = models.Registry.model_validate(
        {
            "version": "p2-multimodal-test-only-v1",
            "max_attempts": 1,
            "max_run_attempts": 1,
            "max_usd": str(Decimal(microusd) / 1_000_000),
            "max_run_usd": str(Decimal(microusd) / 1_000_000),
            "native_streaming": False,
            "models": [data],
        }
    )
    plan = {
        "scope": SCOPE,
        "execution_boundary": "OWNED_TEST_NATIVE_P2_HANDLER",
        "offline_report_sha256": _sha(offline_path),
        "original_failed_report_sha256": link["sha256"],
        "owned_stack": stack.run_id,
        "owned_url": URL,
        "system_identifier": system_id,
        "network": network,
        **{
            key: offline[key]
            for key in ("conversation_id", "case_id", "issue_id", "attachment_ids")
        },
        **{
            key: original[key]
            for key in ("original_image_sha256s", "sent_sha256s", "transform_version")
        },
        "offline_turn_id": offline["offline_readback"]["turn_id"],
        "model": spec.model,
        "output_tokens": OUTPUT,
        "max_posts": 1,
        "automatic_retries": 0,
        "registry_sha256": digest(registry.model_dump(mode="json")),
        "source_spec_sha256": digest(spec.model_dump(mode="json")),
        "maximum_reservation_microusd": microusd,
        "maximum_estimate_cap_usd": str(CAP),
        "estimated_full_registry_reserve_usd": str(reserve),
        "gate_sha256": _sha(GATE),
        "script_sha256": _sha(Path(__file__)),
        "vision_prompt_version": vision.PROMPT_VERSION,
    }
    require(
        all(
            str(UUID(ident)) == ident
            for ident in (
                plan["conversation_id"],
                plan["case_id"],
                plan["issue_id"],
                *plan["attachment_ids"],
                plan["offline_turn_id"],
            )
        ),
        "P5_MULTIMODAL_VISION_INVALID_SCOPE_IDS",
    )
    native = _native(stack, owned_container(stack, "api"), plan)
    turn = native["turns"][-1]
    require(
        native["conversation_status"] == "OPEN"
        and not any(native["counts"].values())
        and turn["id"] == plan["offline_turn_id"]
        and turn["state"] == "PENDING"
        and _bound_turn(turn, plan),
        "P5_MULTIMODAL_VISION_SOURCE_NOT_PRISTINE",
    )
    return plan, registry, stack, state


def _worker(turn_id):
    import sitecustomize
    from aftercare.business.service import BusinessService
    from aftercare.config import load_settings
    from aftercare.conversations.analysis import process
    from aftercare.conversations.service import ConversationService
    from aftercare.db import guarded_engine
    from sqlalchemy import text

    require(sitecustomize.READY, "P5_MULTIMODAL_VISION_GATE_REQUIRED")
    plan = private_json(MOUNT / "plan.json")
    require(
        plan["scope"] == SCOPE
        and _sha(Path(__file__)) == plan["script_sha256"]
        and _sha(Path(sitecustomize.__file__)) == plan["gate_sha256"],
        "P5_MULTIMODAL_VISION_WORKER_BINDING_INVALID",
    )
    wait = monotonic() + 45
    while not (MOUNT / "release.json").exists() and monotonic() < wait:
        sleep(0.1)
    release = private_json(MOUNT / "release.json")
    require(
        release == {"turn_id": turn_id, "plan_sha256": digest(plan)},
        "P5_MULTIMODAL_VISION_RELEASE_MISMATCH",
    )
    settings = load_settings("test")
    require(
        settings.container
        and settings.chat_analysis_enabled
        and not any(
            (
                settings.model_enabled,
                settings.embedding_enabled,
                settings.chat_vision_enabled,
                settings.chat_investigation_enabled,
            )
        ),
        "P5_MULTIMODAL_VISION_TEST_OFFLINE_REQUIRED",
    )
    current = settings.model_copy(
        update={
            "chat_vision_enabled": True,
            "chat_vision_alias": "economy",
            "chat_analysis_max_calls": 1,
            "chat_analysis_max_microusd": plan["maximum_reservation_microusd"],
        }
    )
    engine = guarded_engine(settings, test_only=True)
    try:
        with engine.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            identity = db.execute(
                text(
                    "SELECT current_database(),current_user,session_user,"
                    "current_setting('cluster_name'),system_identifier::text FROM pg_control_system()"
                )
            ).one()
            require(
                tuple(identity)
                == (
                    "ac_test",
                    "ac_test",
                    "ac_test",
                    "aftercare-v2-test",
                    plan["system_identifier"],
                ),
                "P5_MULTIMODAL_VISION_WORKER_DB_MISMATCH",
            )
            turn = (
                db.execute(
                    text(
                        "SELECT t.attachments,t.deadline > now() AS valid FROM public.chat_analysis_turns t "
                        "JOIN public.chat_conversations c ON c.id=t.conversation_id WHERE t.id=:id "
                        "AND t.state='PENDING' AND t.quiet_until<=now() AND c.id=:conversation "
                        "AND c.analysis_issue_id=:issue AND c.active_case_id=:case AND c.revision=t.revision "
                        "AND NOT EXISTS (SELECT 1 FROM public.chat_model_attempts)"
                    ),
                    {
                        "id": turn_id,
                        "conversation": plan["conversation_id"],
                        "issue": plan["issue_id"],
                        "case": plan["case_id"],
                    },
                )
                .mappings()
                .one()
            )
            require(
                turn["valid"] and _bound_turn(turn, plan),
                "P5_MULTIMODAL_VISION_WORKER_TURN_MISMATCH",
            )
        registry = models.Registry.read(MOUNT / "models.json")
        require(
            digest(registry.model_dump(mode="json")) == plan["registry_sha256"],
            "P5_MULTIMODAL_VISION_REGISTRY_CHANGED",
        )
        key = private_json(MOUNT / "model-secret.json")["api_key"]
        require(isinstance(key, str) and bool(key.strip()), "P5_MULTIMODAL_VISION_KEY_MISSING")
        os.environ["AFTERCARE_OPENAI_KEY"] = key
        process(
            ConversationService(BusinessService(engine, current)),
            turn_id,
            current,
            models.ModelRunner(registry),
        )
    finally:
        os.environ.pop("AFTERCARE_OPENAI_KEY", None)
        engine.dispose()


def _fresh_turn(stack, api, plan, report, output):
    before = _native(stack, api, plan)
    old = before["turns"][-1]
    require(
        before["conversation_status"] == "OPEN"
        and old["id"] == plan["offline_turn_id"]
        and old["state"] == "PENDING"
        and _bound_turn(old, plan)
        and not any(before["counts"].values()),
        "P5_MULTIMODAL_VISION_SOURCE_NOT_PRISTINE",
    )
    if not old["expired"]:
        require(
            (datetime.fromisoformat(old["deadline"]) - datetime.now(UTC)).total_seconds() >= 35,
            "P5_MULTIMODAL_VISION_TURN_EXPIRING",
        )
        return old
    _native(stack, api, plan, expire=True)
    expired = _native(stack, api, plan)
    receipt = expired["turns"][-1]
    require(
        receipt["id"] == old["id"]
        and receipt["state"] == "FAILED"
        and receipt["reason"] == "ANALYSIS_EXPIRED"
        and not any(expired["counts"].values()),
        "P5_MULTIMODAL_VISION_EXPIRY_NOT_RECONCILED",
    )
    report["expired_turn"] = {"turn_id": old["id"], "reason": receipt["reason"], "attempt_count": 0}
    ids = request_ids(report, "vision_same_issue_refresh")
    _save(output, report)
    with _owner_client(stack) as owner:
        require(read(owner, "me")["customer_id"] == "C001", "P5_MULTIMODAL_VISION_OWNER_MISMATCH")
        refreshed = message(
            owner, plan["conversation_id"], "请查看本事项已上传的杯子和包装照片。", ids
        )
        require(
            refreshed["conversation"]["active_case_id"] == plan["case_id"],
            "P5_MULTIMODAL_VISION_REFRESH_CASE_CHANGED",
        )
    native = _native(stack, api, plan)
    turn = native["turns"][-1]
    require(
        turn["id"] != old["id"]
        and turn["state"] == "PENDING"
        and not turn["expired"]
        and _bound_turn(turn, plan)
        and not any(native["counts"].values()),
        "P5_MULTIMODAL_VISION_REFRESH_TURN_MISMATCH",
    )
    return turn


def _wait_quiet(turn):
    seconds = (datetime.fromisoformat(turn["quiet_until"]) - datetime.now(UTC)).total_seconds()
    require(seconds < 16, "P5_MULTIMODAL_VISION_QUIET_WAIT_TOO_LONG")
    if seconds > 0:
        sleep(seconds + 0.2)


def _audit(stack, api, plan, stage, report, turn):
    native = _native(stack, api, plan)
    report["audit"] = {
        "attempts": native["attempts"],
        "observations": native["observations"],
        "all_chat_attempts": native["counts"]["chat_model_attempts"],
        "counts": native["counts"],
    }
    for key, name in (("wire", "admission.json"), ("wire_response", "response.json")):
        if (stage / name).exists():
            report[key] = private_json(stage / name)
    wire = report.get("wire")
    response = report.get("wire_response")
    attempts = native["attempts"]
    observations = native["observations"]
    if not wire or not response or len(attempts) != 1 or len(observations) != 2:
        return False
    attempt = attempts[0]
    expected = list(
        zip(
            plan["attachment_ids"],
            plan["original_image_sha256s"],
            plan["sent_sha256s"],
            strict=True,
        )
    )
    by_attachment = {row["attachment_id"]: row for row in observations}
    require(
        set(by_attachment) == set(plan["attachment_ids"]),
        "P5_MULTIMODAL_VISION_NATIVE_ATTACHMENT_MISMATCH",
    )
    require(
        wire["sent_sha256s"] == plan["sent_sha256s"]
        and wire["model"] == plan["model"]
        and wire["retry_index"] == 0
        and wire["output_tokens"] == plan["output_tokens"]
        and response["http_status"] == 200
        and attempt["state"] == "SUCCEEDED"
        and attempt["turn_state"] == "PUBLISHED"
        and attempt["turn_id"] == turn["id"]
        and not attempt["budget_violation"]
        and 0 < attempt["reserved_microusd"] <= plan["maximum_reservation_microusd"]
        and attempt["estimated_cost_microusd"] is not None
        and 0 <= attempt["estimated_cost_microusd"] <= plan["maximum_reservation_microusd"]
        and attempt["response_evidence"]["path"] == "DEFAULT_SYNC"
        and attempt["response_evidence"]["response"] == "SDK_RETURNED"
        and attempt["response_evidence"].get("validated_result_sha256")
        and native["counts"]["chat_model_attempts"] == 1
        and native["counts"]["chat_observations"] == 2
        and not any(
            value
            for key, value in native["counts"].items()
            if key not in {"chat_model_attempts", "chat_observations"}
        )
        and all(
            (
                by_attachment[ident]["original_sha256"],
                by_attachment[ident]["sent_sha256"],
                by_attachment[ident]["attempt_id"],
                by_attachment[ident]["turn_id"],
                by_attachment[ident]["transform_version"],
            )
            == (original, sent, attempt["attempt_id"], turn["id"], plan["transform_version"])
            for ident, original, sent in expected
        ),
        "P5_MULTIMODAL_VISION_NATIVE_WIRE_MISMATCH",
    )
    with _owner_client(stack) as owner:
        public = read(owner, f"conversations/{plan['conversation_id']}")
        cards = _cards(public, "photo_observation")
        require(
            len(cards) == 2
            and all(row["data"]["issue_id"] == plan["issue_id"] for row in cards)
            and [row["data"]["attachment_refs"] for row in cards]
            == [[ident] for ident in plan["attachment_ids"]],
            "P5_MULTIMODAL_VISION_PUBLIC_CARD_MISMATCH",
        )
        for ident, original, _ in expected:
            path = f"conversations/{plan['conversation_id']}/attachments/{ident}/content"
            require(
                sha256(call(owner, "GET", path, 200).content).hexdigest() == original,
                "P5_MULTIMODAL_VISION_ORIGINAL_CHANGED",
            )
    report["ordered_observations"] = [
        {
            key: by_attachment[ident][key]
            for key in ("id", "attempt_id", "attachment_id", "original_sha256", "sent_sha256")
        }
        for ident, _, _ in expected
    ]
    return True


def _collect_wire(stage, report):
    errors = {}
    for key, name in (("wire", "admission.json"), ("wire_response", "response.json")):
        try:
            report[key] = private_json(stage / name)
        except FileNotFoundError:
            pass
        except Exception as error:  # noqa: BLE001 - preserve other receipts and bound the error
            errors[key] = type(error).__name__
    report["wire_receipt_read_errors"] = errors


def _delivery_unknown(report):
    audit = report.get("audit")
    attempts = audit.get("attempts", []) if audit is not None else []
    return (
        ("wire" in report and "wire_response" not in report)
        or any(attempt["state"] in {"STARTED", "UNKNOWN"} for attempt in attempts)
        or (
            report.get("delivery_possible") is True
            and (
                not report.get("paid_worker_removed")
                or audit is None
                or bool(report.get("wire_receipt_read_errors"))
            )
        )
    )


def _execute(stack, state, plan, registry, report, output):
    api = owned_container(stack, "api")
    stage = stack.directory / PRIVATE_STAGE
    stage.mkdir(mode=0o700)
    report["private_stage"] = str(stage)
    _save(output, report)
    gate = stage / "gate"
    gate.mkdir(mode=0o700)
    copyfile(GATE, gate / "sitecustomize.py")
    require(
        _sha(gate / "sitecustomize.py") == plan["gate_sha256"], "P5_MULTIMODAL_VISION_GATE_CHANGED"
    )
    private_write(stage / "plan.json", json.dumps(plan))
    private_write(stage / "models.json", registry.model_dump_json())
    key_path = stage / "model-secret.json"
    name = f"{stack.prefix}-multimodal-p2-verifier"
    turn = None
    require(stack.owned("container", name) is None, "P5_MULTIMODAL_VISION_CONTAINER_PRESENT")
    removed = False
    try:
        turn = _fresh_turn(stack, api, plan, report, output)
        report["turn_id"] = turn["id"]
        _save(output, report)
        key = private_json(ROOT / ".runtime/model-secret.json")["api_key"]
        require(isinstance(key, str) and bool(key.strip()), "P5_MULTIMODAL_VISION_KEY_MISSING")
        private_write(key_path, json.dumps({"api_key": key}))
        stack.secrets.append(key)
        resources = stack.resources
        resources.append(("container", name))
        state["resources"] = resources
        offline_demo.save(state)
        proxy = tuple(
            arg
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "LANGSMITH_TRACING",
                "LANGCHAIN_TRACING_V2",
            )
            for arg in ("--env", f"{key}=")
        )
        stack.docker(
            "P5_MULTIMODAL_VISION_START",
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            stack.label,
            "--network",
            plan["network"],
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--env",
            "PYTHONPATH=/vision-wire/gate:/workspace/scripts",
            *proxy,
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={stage},target=/vision-wire",
            "--mount",
            f"type=bind,source={ROOT / 'scripts'},target=/workspace/scripts,readonly",
            "--entrypoint",
            "python",
            BACKEND,
            "/workspace/scripts/verify_chat_p5_multimodal_vision.py",
            "_worker",
            turn["id"],
            timeout=12,
            log_output=False,
        )
        require(stack.owned("container", name) is True, "P5_MULTIMODAL_VISION_CONTAINER_UNOWNED")
        ingress = f"{stack.prefix}-ingress"
        require(stack.owned("network", ingress) is True, "P5_MULTIMODAL_VISION_INGRESS_UNOWNED")
        stack.docker(
            "P5_MULTIMODAL_VISION_CONNECT_EGRESS",
            "network",
            "connect",
            ingress,
            name,
            timeout=10,
            log_output=False,
        )
        _wait_quiet(turn)
        private_write(
            stage / "release.tmp", json.dumps({"turn_id": turn["id"], "plan_sha256": digest(plan)})
        )
        report["delivery_possible"] = True
        _save(output, report)
        (stage / "release.tmp").replace(stage / "release.json")
        result = stack.docker(
            "P5_MULTIMODAL_VISION_WAIT", "wait", name, timeout=70, log_output=False
        )
        report["worker_exit"] = int(result.stdout.strip())
    finally:
        try:
            try:
                owned = stack.owned("container", name)
                require(owned is not False, "P5_MULTIMODAL_VISION_CONTAINER_UNOWNED")
                if owned:
                    stack.docker("P5_MULTIMODAL_VISION_CLEANUP", "rm", "-f", name, log_output=False)
                require(
                    stack.owned("container", name) is None, "P5_MULTIMODAL_VISION_CONTAINER_REMAINS"
                )
                removed = True
                report["paid_worker_removed"] = True
            finally:
                key_path.unlink(missing_ok=True)
                report["private_worker_key_removed"] = not key_path.exists()
                if removed and ("container", name) in stack.resources:
                    stack.resources.remove(("container", name))
                    offline_demo.save(state)
        finally:
            _collect_wire(stage, report)
    valid = _audit(stack, api, plan, stage, report, turn)
    import verify_chat_p5_pg as pg

    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        require(
            pg._topology(stack, {pg.BACKEND: BACKEND, pg.FRONTEND: FRONTEND})
            == (plan["network"], plan["system_identifier"]),
            "P5_MULTIMODAL_VISION_POST_TOPOLOGY_CHANGED",
        )
    require(valid and report.get("worker_exit") == 0, "P5_MULTIMODAL_VISION_P2_INCOMPLETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    with (
        offline_demo.lifecycle_lock(STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", STATE),
    ):
        plan, registry, stack, state = _plan(args.offline_report)
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}, indent=2
                )
            )
            return 0
        require(args.expected_plan_sha256 == checksum, "P5_MULTIMODAL_VISION_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "plan": plan,
            "plan_sha256": checksum,
            "request_ids": {},
            **{
                key: plan[key]
                for key in ("owned_stack", "owned_url", "conversation_id", "case_id", "issue_id")
            },
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _execute(stack, state, plan, registry, report, args.report)
            report["status"] = "PASSED"
        except Exception as error:  # noqa: BLE001 - bound error and retain delivery uncertainty
            code = str(error)
            if "audit" not in report and "turn_id" in report:
                try:
                    native = _native(stack, owned_container(stack, "api"), plan)
                    report["audit"] = {
                        key: native[key] for key in ("attempts", "observations", "counts")
                    }
                except Exception as audit_error:  # noqa: BLE001 - keep the original failure and omit private output
                    report["audit_failure_type"] = type(audit_error).__name__
            report.update(
                status="UNKNOWN" if _delivery_unknown(report) else "FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "_worker":
            _worker(sys.argv[2])
        else:
            raise SystemExit(main())
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(
            code if re.fullmatch(r"[A-Z0-9_]+", code) else "P5_MULTIMODAL_VISION_BLOCKED",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
