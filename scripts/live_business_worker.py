"""Dedicated owned-test Celery consumer for one scope-bound live business phase."""

import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from aftercare import runtime
from aftercare.business.models import Case, Material, MaterialReview, Order
from aftercare.business.rules import digest
from aftercare.config import load_settings
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, ChatModelAttempt, Observation
from aftercare.conversations.tables import ChatMaterialSource, Conversation, InvestigationRequest
from aftercare.db import User, guarded_engine
from aftercare.decisions.contracts import DecisionScope
from aftercare.evaluation.service import require_current_code
from aftercare.intelligence.decision_candidate import load_binding
from aftercare.intelligence.models import (
    P4_INPUT_LIMIT,
    ModelFailure,
    ModelRunner,
    Registry,
    _reserve_price,
)
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from aftercare.investigation.input import build_input
from aftercare.investigation.protocol import HUMAN_PAYMENT_V7, protocol_for_binding
from aftercare.queue import make_queue
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

WIRE = Path("/live-business-wire")
PROTOCOL = "p5-live-business-wire-v1"
SCOPED_PROTOCOL = "p5-live-business-wire-v2"
EXPANDED_PROTOCOL = "p5-live-business-wire-v3"
SCOPED_PROTOCOLS = {SCOPED_PROTOCOL, EXPANDED_PROTOCOL}
CONTROL_FIELD = "material_review_control"
TASKS = {"VISION": "aftercare.chat_analysis", "TEXT": "aftercare.chat_investigation_start"}
CAPS = {"VISION": (1, 900, 50_000), "TEXT": (8, 1800, 400_000)}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _private(path):
    metadata = path.lstat()
    _require(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & 0o077,
        "LIVE_BUSINESS_WORKER_PRIVATE_FILE_REQUIRED",
    )
    return path.read_bytes()


def _journal(name, payload):
    fd = os.open(WIRE / name, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(WIRE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _preflight(gate):
    _require(
        getattr(ModelRunner, "_logical_attempt_limit", None) is not None,
        "LIVE_BUSINESS_WORKER_LOGICAL_LIMIT_UNSUPPORTED",
    )
    plan_bytes = _private(WIRE / "plan.json")
    plan = json.loads(plan_bytes)
    plan_sha = sha256(plan_bytes).hexdigest()
    kind = plan["kind"]
    _require(
        plan["protocol"] in {PROTOCOL, *SCOPED_PROTOCOLS}
        and getattr(gate, "PLAN", None) == plan
        and kind in CAPS
        and os.environ.get("P5_LIVE_BUSINESS_PLAN_SHA256") == plan_sha
        and os.environ.get("AFTERCARE_CONTAINER") == "1"
        and os.environ.get("AFTERCARE_PROFILE") == "test"
        and os.environ.get("AFTERCARE_CONTAINER_MODEL_MODE") == "offline"
        and getattr(gate, "READY", False) is True
        and sha256(Path(gate.__file__).read_bytes()).hexdigest() == plan["gate_script_sha256"]
        and sha256(Path(__file__).read_bytes()).hexdigest() == plan["worker_script_sha256"],
        "LIVE_BUSINESS_WORKER_PLAN_REJECTED",
    )
    _require(
        all(
            str(UUID(plan[key])) == plan[key]
            for key in (
                "conversation_id",
                "case_id",
                "issue_id",
                "turn_id",
            )
        )
        and bool(re.fullmatch(r"[0-9]{10,25}", plan["system_identifier"]))
        and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", plan["campaign_id"])),
        "LIVE_BUSINESS_WORKER_SCOPE_REJECTED",
    )
    expanded = plan["protocol"] == EXPANDED_PROTOCOL
    calls, output, cap = (8, 4096, 600_000) if expanded and kind == "TEXT" else CAPS[kind]
    registry = Registry.model_validate_json(_private(WIRE / "registry.json"))
    _require(
        len(registry.models) == 1
        and digest(registry.model_dump(mode="json")) == plan["registry_sha256"]
        and not registry.native_streaming
        and registry.max_attempts == registry.max_run_attempts == calls
        and plan["maximum_provider_posts"] == calls
        and plan["maximum_reservation_microusd"] == cap
        and plan["output_tokens"] == output
        and registry.max_usd is not None
        and registry.max_run_usd is not None
        and 0 < registry.max_usd <= Decimal(cap) / 1_000_000
        and 0 < registry.max_run_usd <= Decimal(cap) / 1_000_000,
        "LIVE_BUSINESS_WORKER_REGISTRY_REJECTED",
    )
    spec = registry.models[0]
    _require(
        spec.model == plan["model"] == "gpt-6.1-terra"
        and spec.provider == "openai"
        and spec.interface == "responses"
        and spec.endpoint.rstrip("/") == "https://api.openai.com/v1"
        and spec.api_key_env == "AFTERCARE_OPENAI_KEY"
        and spec.output_tokens == output
        and spec.cache_write_ttl != "12h"
        and spec.alias == ("economy" if kind == "VISION" else "analysis")
        and spec.vision is (kind == "VISION")
        and set(spec.data_scopes)
        == {"customer_attachment" if kind == "VISION" else "customer_conversation"},
        "LIVE_BUSINESS_WORKER_MODEL_REJECTED",
    )
    if expanded:
        token_basis = spec.context_tokens if kind == "VISION" else P4_INPUT_LIMIT
        cost = _reserve_price(spec, token_basis, output)
        _require(
            cost is not None
            and plan["per_post_reserved_microusd"]
            == int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
            and calls * plan["per_post_reserved_microusd"] <= cap,
            "LIVE_BUSINESS_WORKER_WIRE_RESERVE_REJECTED",
        )
    settings = load_settings("test")
    _require(
        settings.container
        and settings.profile == "test"
        and settings.chat_analysis_enabled
        and settings.chat_decisions_enabled
        and settings.chat_investigation_enabled is (kind == "TEXT")
        and settings.chat_investigation_protocol == "compact-v7"
        and (
            kind != "TEXT" or getattr(settings, "chat_investigation_output_reserve", 1800) == output
        )
        and settings.chat_investigation_issue_max_calls == 16
        and settings.chat_investigation_issue_max_microusd == 200_000
        and not any(
            (settings.model_enabled, settings.embedding_enabled, settings.chat_vision_enabled)
        ),
        "LIVE_BUSINESS_WORKER_OFFLINE_TEST_REQUIRED",
    )
    return plan, plan_sha, registry, settings


def _identity(db, plan):
    observed = db.execute(
        text(
            "SELECT current_database(), current_user, session_user, current_setting('cluster_name'), "
            "system_identifier::text FROM pg_control_system()"
        )
    ).one()
    _require(
        tuple(observed)
        == ("ac_test", "ac_test", "ac_test", "aftercare-v2-test", plan["system_identifier"]),
        "LIVE_BUSINESS_WORKER_DATABASE_REJECTED",
    )


def _scope(db, plan, *, status):
    turn = db.get_one(ChatAnalysisTurn, plan["turn_id"])
    conversation = db.get_one(Conversation, plan["conversation_id"])
    case = db.get_one(Case, plan["case_id"])
    owner = db.get_one(User, turn.actor_id)
    scope = plan.get("case_scope") or {
        "customer_id": "C001",
        "order_id": "O9902",
        "line_ids": ["L01"],
    }
    if plan["protocol"] in SCOPED_PROTOCOLS:
        order = db.get_one(Order, scope["order_id"])
        _require(
            order.customer_id == scope["customer_id"]
            and order.version == scope["order_version"]
            and case.intent == "damaged"
            and not conversation.require_new_case,
            "LIVE_BUSINESS_WORKER_NATIVE_ORDER_SCOPE_REJECTED",
        )
    latest = db.scalar(
        select(ChatAnalysisTurn.id)
        .where(ChatAnalysisTurn.conversation_id == conversation.id)
        .order_by(ChatAnalysisTurn.generation.desc())
        .limit(1)
    )
    _require(
        (conversation.active_case_id, conversation.analysis_issue_id, conversation.status)
        == (case.id, plan["issue_id"], "OPEN")
        and (case.decision_conversation_id, case.decision_issue_id)
        == (conversation.id, plan["issue_id"])
        and conversation.active_order_id == case.order_id == scope["order_id"]
        and conversation.line_ids == case.line_ids == scope["line_ids"]
        and conversation.customer_id == scope["customer_id"]
        and owner.enabled
        and owner.role == "customer"
        and owner.customer_id == case.customer_id == turn.customer_id == conversation.customer_id
        and turn.conversation_id == conversation.id
        and turn.revision == conversation.revision
        and latest == turn.id
        and turn.state == status
        and db.scalar(select(func.count()).select_from(Conversation)) == 1,
        "LIVE_BUSINESS_WORKER_NATIVE_SCOPE_REJECTED",
    )
    return turn


def _expected_control_observation(control):
    return {
        **{key: control[key] for key in ("material_id", "finding", "line_id", "affected_quantity")},
        "source": "HUMAN_MATERIAL_REVIEW",
    }


def _validate_control_review(db, plan, turn, review):
    if CONTROL_FIELD not in plan:
        _require(review.observations is None, "LIVE_BUSINESS_WORKER_TEXT_SOURCE_NOT_READY")
        return
    control = plan[CONTROL_FIELD]
    material = db.get_one(Material, control["material_id"])
    link = db.get_one(ChatMaterialSource, (plan["case_id"], control["attachment_id"]))
    _require(
        review.id == control["review_id"]
        and review.case_id == plan["case_id"]
        and review.decision == "APPROVED"
        and review.observations == [_expected_control_observation(control)]
        and control["material_id"] in review.material_ids
        and (material.case_id, material.kind, material.sha256)
        == (plan["case_id"], "ITEM", control["original_sha256"])
        and (link.material_id, link.conversation_id, link.issue_id, link.kind, link.sha256)
        == (
            material.id,
            plan["conversation_id"],
            plan["issue_id"],
            "ITEM",
            control["original_sha256"],
        )
        and bool(turn.attachments)
        and (
            turn.attachments[0]["id"],
            turn.attachments[0]["message_id"],
            turn.attachments[0]["sha256"],
        )
        == (control["attachment_id"], link.message_id, control["original_sha256"]),
        "LIVE_BUSINESS_WORKER_CONTROL_REVIEW_REJECTED",
    )


def _validate_frozen_control_review(plan, scope):
    if CONTROL_FIELD not in plan:
        return
    control = plan[CONTROL_FIELD]
    frozen = scope.frozen_context
    reviews = [item for item in frozen["reviews"] if item.get("effective") is True]
    attachments = {row["id"]: row for row in frozen["attachments"]}
    attachment = attachments.get(control["attachment_id"])
    _require(
        len(reviews) == 1
        and reviews[0]["id"] == control["review_id"]
        and reviews[0]["decision"] == "APPROVED"
        and attachment is not None
        and attachment["sha256"] == control["original_sha256"]
        and reviews[0].get("observations")
        == [
            {
                **_expected_control_observation(control),
                "material_sha256": control["original_sha256"],
                "original_attachments": [
                    {
                        "attachment_id": control["attachment_id"],
                        "message_id": attachment["message_id"],
                        "sha256": control["original_sha256"],
                    }
                ],
            }
        ],
        "LIVE_BUSINESS_WORKER_FROZEN_CONTROL_REVIEW_REJECTED",
    )


def _text_scope(db, plan, audit):
    turn = _scope(db, plan, status="PUBLISHED")
    run = db.get_one(AgentRun, audit.run_id)
    request = db.get_one(InvestigationRequest, turn.id)
    binding = load_binding(db, run)
    scope = DecisionScope.model_validate(binding["scope"])
    _require(
        run.status == "RUNNING"
        and run.cycle == audit.cycle
        and run.owner_id == request.actor_id == turn.actor_id
        and run.case_id == request.case_id == scope.case_id == plan["case_id"]
        and request.conversation_id == scope.conversation_id == plan["conversation_id"]
        and request.issue_id == scope.issue_id == plan["issue_id"]
        and request.state == "PENDING"
        and request.scope == binding["scope"]
        and binding["decision_turn_id"] == turn.id
        and binding["investigation_arm"] == "auto"
        and protocol_for_binding(binding, scope) == HUMAN_PAYMENT_V7,
        "LIVE_BUSINESS_WORKER_NATIVE_RUN_REJECTED",
    )
    if plan["protocol"] in SCOPED_PROTOCOLS:
        planned = plan["case_scope"]
        routes = list(
            db.scalars(
                select(RunEvent.payload).where(
                    RunEvent.run_id == run.id,
                    RunEvent.kind == "investigation_route",
                )
            )
        )
        _require(
            len(routes) == 1
            and routes[0].get("selected_path") == plan["expected_route"]
            and routes[0].get("arm") == "auto"
            and routes[0].get("prompt_version") == HUMAN_PAYMENT_V7.prompt_version
            and routes[0].get("evidence_projection") == HUMAN_PAYMENT_V7.evidence_projection
            and scope.order_id == planned["order_id"]
            and scope.customer_id == planned["customer_id"]
            and list(scope.line_ids) == planned["line_ids"]
            and scope.business_versions.get("order") == planned["order_version"],
            "LIVE_BUSINESS_WORKER_NATIVE_ROUTE_REJECTED",
        )
    output_reserve = binding.get("investigation_output_reserve", 1800)
    _require(
        type(output_reserve) is int
        and output_reserve == (4096 if plan["protocol"] == EXPANDED_PROTOCOL else 1800),
        "LIVE_BUSINESS_WORKER_NATIVE_OUTPUT_RESERVE_REJECTED",
    )
    _validate_frozen_control_review(plan, scope)
    value = build_input(scope, evidence_projection=HUMAN_PAYMENT_V7.evidence_projection)
    return {
        "run_id": run.id,
        **({CONTROL_FIELD: plan[CONTROL_FIELD]} if CONTROL_FIELD in plan else {}),
        **(
            {"native_output_tokens": output_reserve}
            if plan["protocol"] == EXPANDED_PROTOCOL
            else {}
        ),
        "scope_sha256": digest(scope.model_dump(mode="json")),
        "relevant_input_sha256": value.relevant_input_sha256,
        "policy_catalogue_sha256": value.policy_context["catalogue_sha256"],
        "role_evidence": {
            role: [
                [row["source_kind"], row["source_id"], row["version_or_sha256"]]
                for row in value.role_view(role)["available_evidence"]
            ]
            for role in ("strong_single", "fact", "policy", "verifier")
        },
    }


def _attempt_binding(plan, audit, logical_id, ordinal, reserve):
    micro = int((Decimal(reserve) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    binding = {
        **{key: plan[key] for key in ("kind", "conversation_id", "case_id", "issue_id", "turn_id")},
        "native_logical_id": logical_id,
        "native_ordinal": ordinal,
        "reserved_microusd": micro,
    }
    if plan["protocol"] in SCOPED_PROTOCOLS:
        binding.update(case_scope=plan["case_scope"], expected_route=plan["expected_route"])
    engine = audit.service.engine if plan["kind"] == "VISION" else audit.engine
    with Session(engine) as db, db.begin():
        db.execute(text("SET TRANSACTION READ ONLY"))
        _identity(db, plan)
        if plan["kind"] == "VISION":
            turn = _scope(db, plan, status="RUNNING")
            attempt = db.get_one(ChatModelAttempt, audit.attempt_id)
            photos = turn.vision_input["photos"]
            _require(
                audit.lease.turn_id == turn.id == attempt.turn_id
                and attempt.state == "STARTED"
                and attempt.reserved_microusd == micro
                and attempt.model_evidence["logical_id"] == logical_id
                and attempt.model_evidence["transport_evidence"]["path"] == "DEFAULT_SYNC"
                and turn.vision_input["logical_id"] == logical_id
                and [row["original_sha256"] for row in photos] == plan["original_sha256s"]
                and [row["sent_sha256"] for row in photos] == plan["sent_sha256s"],
                "LIVE_BUSINESS_WORKER_NATIVE_VISION_ATTEMPT_REJECTED",
            )
            binding.update({key: plan[key] for key in ("original_sha256s", "sent_sha256s")})
        else:
            binding.update(_text_scope(db, plan, audit))
            attempt = db.get_one(ModelAttempt, (audit.run_id, audit.cycle, logical_id, ordinal))
            _require(
                attempt.status == "STARTED"
                and Decimal(attempt.metadata_json["reserved_cost_usd"]) == reserve
                and attempt.metadata_json["transport_evidence"]["path"] == "DEFAULT_SYNC"
                and (
                    plan["protocol"] != EXPANDED_PROTOCOL
                    or attempt.metadata_json["transport_evidence"].get("max_output_tokens")
                    == plan["output_tokens"]
                ),
                "LIVE_BUSINESS_WORKER_NATIVE_TEXT_ATTEMPT_REJECTED",
            )
    return binding


class _GateAudit:
    def __init__(self, audit, gate, plan, stack):
        self.audit, self.gate, self.plan, self.stack = audit, gate, plan, stack

    def __getattr__(self, name):
        return getattr(self.audit, name)

    def begin_attempt(self, logical_id, ordinal, spec, reserve, registry, **kwargs):
        if ordinal != 1 or reserve is None:
            raise ModelFailure("LIVE_BUSINESS_WORKER_RETRY_OR_UNPRICED_ATTEMPT")
        if self.plan.get(
            "protocol"
        ) in SCOPED_PROTOCOLS and not self.gate.policy.admitted_logical_phase(
            self.plan, logical_id
        ):
            raise ModelFailure("LIVE_BUSINESS_WORKER_UNPLANNED_NATIVE_PHASE")
        remaining = self.audit.begin_attempt(logical_id, ordinal, spec, reserve, registry, **kwargs)
        self.stack.enter_context(
            self.gate.attempt(_attempt_binding(self.plan, self.audit, logical_id, ordinal, reserve))
        )
        return remaining


class ScopedModelRunner(ModelRunner):
    _logical_attempt_limit = 1

    def __init__(self, registry, gate, plan):
        super().__init__(registry)
        self.gate, self.plan = gate, plan

    def call(self, audit, *args, **kwargs):
        vision = self.plan["kind"] == "VISION"
        _require(
            kwargs.get("vision", False) is vision
            and kwargs.get("data_scope")
            == ("customer_attachment" if vision else "customer_conversation"),
            "LIVE_BUSINESS_WORKER_MODEL_SCOPE_REJECTED",
        )
        with ExitStack() as stack:
            return super().call(_GateAudit(audit, self.gate, self.plan, stack), *args, **kwargs)


def _scope_readback(engine, plan):
    with Session(engine) as db, db.begin():
        db.execute(text("SET TRANSACTION READ ONLY"))
        _identity(db, plan)
        vision = plan["kind"] == "VISION"
        turn = _scope(db, plan, status="PENDING" if vision else "PUBLISHED")
        if vision:
            _require(
                [row["sha256"] for row in turn.attachments] == plan["original_sha256s"]
                and not db.scalar(select(ChatModelAttempt.id).limit(1)),
                "LIVE_BUSINESS_WORKER_VISION_TURN_NOT_PRISTINE",
            )
        else:
            _require(
                not db.scalar(select(AgentRun.id).limit(1)),
                "LIVE_BUSINESS_WORKER_TEXT_RUN_ALREADY_EXISTS",
            )
            (vision_attempt,) = db.scalars(select(ChatModelAttempt)).all()
            observations = list(db.scalars(select(Observation)))
            case = db.get_one(Case, plan["case_id"])
            review = db.scalar(
                select(MaterialReview).where(
                    MaterialReview.case_id == case.id,
                    MaterialReview.materials_version == case.materials_version,
                )
            )
            originals = {row["id"]: row["sha256"] for row in turn.attachments}
            _require(
                vision_attempt.conversation_id == plan["conversation_id"]
                and vision_attempt.state == "SUCCEEDED"
                and bool((vision_attempt.response_evidence or {}).get("validated_result_sha256"))
                and len(observations) == len(originals) == 2
                and all(row.attempt_id == vision_attempt.id for row in observations)
                and {row.attachment_id: row.original_sha256 for row in observations} == originals
                and review is not None,
                "LIVE_BUSINESS_WORKER_TEXT_SOURCE_NOT_READY",
            )
            _validate_control_review(db, plan, turn, review)


def _install(app, plan, model, settings):
    selected = TASKS[plan["kind"]]
    task = app.tasks[selected]
    native = task.run
    open_runtime = runtime.open_runtime

    @contextmanager
    def scoped_runtime(profile, dispatch=None):
        _require(profile == "test" and dispatch is None, "LIVE_BUSINESS_WORKER_WRONG_RUNTIME")
        with open_runtime(profile) as (current, engine, business, agent):
            _require(current == settings, "LIVE_BUSINESS_WORKER_SETTINGS_CHANGED")
            _scope_readback(engine, plan)
            if plan["kind"] == "VISION":
                current = current.model_copy(
                    update={
                        "chat_vision_enabled": True,
                        "chat_vision_alias": "economy",
                        "chat_analysis_max_calls": 1,
                        "chat_analysis_max_microusd": CAPS["VISION"][2],
                    }
                )
                business.settings = current
            agent.model = model
            yield current, engine, business, agent

    def scoped_task(turn_id):
        if turn_id != plan["turn_id"]:
            return
        try:
            _journal("native-dispatch.json", {"task": selected, "turn_id": turn_id})
        except FileExistsError:
            return
        with patch.object(runtime, "open_runtime", scoped_runtime):
            return native(turn_id)

    def deferred(*_args, **_kwargs):
        # This short-lived consumer must never resume an unrelated or already
        # dispatched model job. The owned offline worker is restored afterwards.
        return None

    for name in {*TASKS.values(), "aftercare.process", "aftercare.evaluate"} - {selected}:
        app.tasks[name].run = deferred
    task.run = scoped_task


def main():
    import sitecustomize

    require_current_code()
    plan, plan_sha, registry, settings = _preflight(sitecustomize)
    engine = guarded_engine(settings, test_only=True)
    try:
        _scope_readback(engine, plan)
    finally:
        engine.dispose()
    key = json.loads(_private(WIRE / "model-secret.json"))["api_key"]
    _require(
        isinstance(key, str) and bool(key.strip()) and not os.environ.get("AFTERCARE_OPENAI_KEY"),
        "LIVE_BUSINESS_WORKER_CREDENTIAL_REJECTED",
    )
    _journal("worker-claim.json", {"plan_sha256": plan_sha, "kind": plan["kind"]})
    os.environ["AFTERCARE_OPENAI_KEY"] = key
    try:
        app = make_queue("test")
        _install(app, plan, ScopedModelRunner(registry, sitecustomize, plan), settings)
        app.worker_main(
            [
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--queues=aftercare-test",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
            ]
        )
    finally:
        os.environ.pop("AFTERCARE_OPENAI_KEY", None)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - bound startup errors without printing credentials
        code = str(error)
        print(
            code if re.fullmatch(r"LIVE_BUSINESS_WORKER_[A-Z0-9_]+", code) else type(error).__name__
        )
        raise SystemExit(1) from None
