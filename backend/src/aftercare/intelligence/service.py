"""Durable run lifecycle. Workflow output is an unauthorised draft."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from langsmith import tracing_context
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.refund_lookup import resolve_refund_text, targeted_refund
from aftercare.business.rules import digest
from aftercare.db import User
from aftercare.decisions.contracts import DecisionScope
from aftercare.intelligence.decision_candidate import (
    DECISION_INPUT_EVENT,
    DECISION_READY_EVENT,
    auto_turn_current,
    load_binding,
    make_binding,
)
from aftercare.intelligence.graph import GraphContext, build_graph
from aftercare.intelligence.model_limits import ProviderLimiter
from aftercare.intelligence.models import (
    P4_OUTPUT_RESERVE,
    ModelFailure,
    ModelRunner,
    Registry,
    _reserve_price,
    finish_transport_evidence,
)
from aftercare.intelligence.policies import MODEL_ID, PolicySearch, bge_encoder, document_key
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent, RunMutation
from aftercare.investigation.catalogue import CATALOGUE_VERSION
from aftercare.investigation.graph import ARMS, CHAT_GRAPH_PROFILE, build_investigation_graph
from aftercare.investigation.input import InputUnavailable, build_input
from aftercare.investigation.issue_budget import (
    IssueBudget,
    IssueBudgetDenied,
    admit_issue,
    lock_issue,
    to_microusd,
)
from aftercare.investigation.orchestration import (
    INVESTIGATION_INPUT_TOKEN_LIMIT,
    InvestigationContext,
)
from aftercare.investigation.prompt_evidence import PRIVATE_RECEIPT_EVENT, PROMPT_VERSION
from aftercare.investigation.routing import route_input, route_unavailable
from aftercare.investigation.tools import InvestigationTools

EVENT_PAGE_SIZE = 200
CONTEXT_BINDING_VERSION = "agent-context-v1"
REFUND_CONTEXT_BINDING_VERSION = "agent-context-v2-refund"
CONTEXT_FIELDS = (
    "case",
    "order",
    "policies",
    "policy_resolution",
    "materials",
    "reviews",
    "ledger",
    "versions",
    "eligibility",
    "facts",
)


def _context_digest(facts):
    # BusinessService.context supplies persisted facts and resolved policies, not signed URLs.
    snapshot = {key: facts.get(key) for key in CONTEXT_FIELDS}
    lookup = facts.get("refund_lookup")
    if targeted_refund(lookup):
        snapshot["refund_lookup"] = lookup
    return digest(snapshot)


def _with_refund_lookup(facts, message):
    if facts["case"]["intent"] != "refund_status":
        return facts
    lookup = resolve_refund_text(
        facts["ledger"],
        message,
        facts["case"].get("description", ""),
    )
    return {**facts, "refund_lookup": lookup}


class RunError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class QueueFenceLost(RuntimeError):
    """This worker may persist incurred usage, but no longer owns the workflow."""

    code = "QUEUE_LEASE_LOST"

    def __init__(self):
        super().__init__(self.code)


def _queue_owned(session, admission):
    return admission is None or admission(session)


def _require_queue(session, admission):
    if not _queue_owned(session, admission):
        raise QueueFenceLost()


class _FencedCheckpointSaver(BaseCheckpointSaver):
    """Hold the queue fence during each short checkpoint write transaction."""

    def __init__(self, saver, guard):
        super().__init__(serde=saver.serde)
        self.saver, self.guard = saver, guard

    @property
    def config_specs(self):
        return self.saver.config_specs

    def get_tuple(self, config):
        return self.saver.get_tuple(config)

    def list(self, config, **kwargs):
        return self.saver.list(config, **kwargs)

    def get_next_version(self, current, channel):
        return self.saver.get_next_version(current, channel)

    def put(self, *args, **kwargs):
        with self.guard():
            return self.saver.put(*args, **kwargs)

    def put_writes(self, *args, **kwargs):
        with self.guard():
            return self.saver.put_writes(*args, **kwargs)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value):
    return value.isoformat() if value else None


def _utc(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _seconds(deadline) -> float:
    if deadline is None:
        return 0
    return (_utc(deadline) - _now()).total_seconds()


def _identity(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


def _lock(session, case_id: str):
    if session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _identity(f"agent-case:{case_id}")}
        )


@contextmanager
def _lease(engine, run_id):
    if engine.dialect.name != "postgresql":
        yield True
        return
    key = _identity(f"agent-run:{run_id}")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        held = conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            yield held
        finally:
            if held:
                conn.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def _private_graph(function, *args, **kwargs):
    def invoke():
        with tracing_context(enabled=False, parent=False):
            return function(*args, **kwargs)

    return Context().run(invoke)


def _public_output(output):
    if output and output.get("graph_profile") == CHAT_GRAPH_PROFILE:
        return {
            key: value
            for key, value in output.items()
            if not key.startswith("investigation_") and key not in {"action", "lines", "policy_ids"}
        }
    return output


def _event_payload(row, user):
    if row.kind in {DECISION_INPUT_EVENT, DECISION_READY_EVENT, PRIVATE_RECEIPT_EVENT}:
        return {}
    if row.kind.startswith("investigation_") and user.role not in {"support", "tester"}:
        return {}
    if row.kind == "status" and "output" in row.payload:
        return {**row.payload, "output": _public_output(row.payload["output"])}
    return row.payload


def _serialize(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "thread_id": run.thread_id,
        "case_id": run.case_id,
        "status": run.status,
        "mode": run.mode,
        "strategy": run.strategy,
        "case_version": run.case_version,
        "output": _public_output(run.output),
        "reason": run.reason,
        "cycle": run.cycle,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _event(session, run, kind, payload):
    run.event_seq += 1
    run.updated_at = _now()
    session.add(
        RunEvent(
            run_id=run.id,
            seq=run.event_seq,
            kind=kind,
            payload=payload,
            created_at=run.updated_at,
        )
    )


def _mark(session, run, status, reason=None, output=None):
    run.status, run.reason, run.output = status, reason, output
    _event(session, run, "status", {"status": status, "reason": reason, "output": output})


class RunAudit:
    def __init__(
        self,
        engine,
        run: AgentRun,
        attempt_admission=None,
        provider_limiter=None,
        *,
        queue_admission=None,
        attempt_continuation=None,
        spend_admission=None,
        spend_scope=None,
    ):
        self.engine, self.run_id, self.cycle = engine, run.id, run.cycle
        self.spend_admission = spend_admission
        self.spend_scope = spend_scope
        self.deadline = run.deadline
        self.attempt_admission = attempt_admission
        self.attempt_continuation = attempt_continuation
        self.queue_admission = queue_admission
        self.provider_limiter = (
            provider_limiter if provider_limiter is not None else ProviderLimiter(engine)
        )

    def claim_provider(self, spec, registry):
        limit = registry.provider_limits.get(spec.provider)
        if limit is None:
            return None
        if self.queue_admission is not None:
            with Session(self.engine) as session, session.begin():
                _require_queue(session, self.queue_admission)
        # Provider read timeout can outlast the logical deadline by one blocking read.
        lease_seconds = registry.deadline_seconds + 2 * registry.timeout_seconds + 5
        return self.provider_limiter.claim(limit, lease_seconds)

    def release_provider(self, slot, *, attempted, uncertain=False, cooldown_seconds=0):
        if slot is not None:
            self.provider_limiter.release(
                slot,
                attempted=attempted,
                uncertain=uncertain,
                cooldown_seconds=cooldown_seconds,
            )

    def close_draft(self, kind, payload):
        # A terminal discard remains visible on a user-cancelled run while the job owns it.
        with Session(self.engine) as session, session.begin():
            _require_queue(session, self.queue_admission)
            run = session.get(AgentRun, self.run_id, with_for_update=True)
            if run.cycle == self.cycle:
                if kind == "model_draft_committed" and run.status != "RUNNING":
                    kind, payload = "model_draft_discarded", {**payload, "reason": "RUN_INACTIVE"}
                _event(session, run, kind, payload)

    def _attempts(self, *, whole_run=False):
        rows = select(ModelAttempt).where(ModelAttempt.run_id == self.run_id)
        return rows if whole_run else rows.where(ModelAttempt.cycle == self.cycle)

    def _active(self, session):
        _require_queue(session, self.queue_admission)
        run = session.get(AgentRun, self.run_id, with_for_update=True)
        if run.status != "RUNNING" or run.cycle != self.cycle:
            raise ModelFailure("RUN_INACTIVE")
        actor = session.get(User, run.actor["user_id"])
        if (
            actor is None
            or not actor.enabled
            or actor.role != run.actor["role"]
            or actor.customer_id != run.actor.get("customer_id")
        ):
            raise ModelFailure("RUN_ACTOR_REVOKED")
        return run

    def event(self, kind, payload):
        with Session(self.engine) as session, session.begin():
            run = self._active(session)
            _event(session, run, kind, payload)

    def budget(self, registry):
        with Session(self.engine) as session:
            attempts = list(session.scalars(self._attempts(whole_run=True)))
        return {
            "attempts_used": sum(attempt.cycle == self.cycle for attempt in attempts),
            "attempts_limit": registry.max_attempts,
            "run_attempts_used": len(attempts),
            "run_attempts_limit": registry.max_run_attempts,
            "cycle_cost_limit_usd": str(registry.max_usd) if registry.max_usd is not None else None,
            "run_cost_limit_usd": str(registry.max_run_usd)
            if registry.max_run_usd is not None
            else None,
            "remaining_seconds": max(0, _seconds(self.deadline)),
        }

    def cooldown_aliases(self, seconds: float) -> set[str]:
        if seconds <= 0:
            return set()
        with Session(self.engine) as session:
            limited = session.scalars(
                self._attempts().where(
                    ModelAttempt.error_code == "MODEL_RATE_LIMIT",
                    ModelAttempt.finished_at > _now() - timedelta(seconds=seconds),
                )
            )
            return {item.metadata_json["alias"] for item in limited}

    def replay(self, logical_id: str, *, expected_output_reserve=None) -> dict | None:
        with Session(self.engine) as session, session.begin():
            run = self._active(session)
            attempts = list(
                session.scalars(
                    self._attempts()
                    .where(
                        ModelAttempt.logical_id == logical_id,
                    )
                    .order_by(ModelAttempt.ordinal)
                )
            )
            if not attempts:
                return None
            last = attempts[-1]
            if last.status == "SUCCEEDED":
                evidence = last.metadata_json.get("transport_evidence", {})
                if (
                    expected_output_reserve is not None
                    and evidence.get("max_output_tokens") != expected_output_reserve
                ):
                    raise ModelFailure("REPLAY_OUTPUT_BUDGET_CHANGED")
                return last.result
            if last.status == "STARTED":
                last.status, last.error_code = "UNKNOWN", "INTERRUPTED_MODEL_CALL"
                last.finished_at = _now()
                _event(session, run, "attempt", self._audit_payload(last))
                if last.metadata_json.get("native_streaming"):
                    _event(
                        session,
                        run,
                        "model_draft_discarded",
                        {
                            "logical_id": logical_id,
                            "ordinal": last.ordinal,
                            "alias": last.metadata_json["alias"],
                            "reason": last.error_code,
                        },
                    )
            failure = last.error_code or "INTERRUPTED_MODEL_CALL"
        raise ModelFailure(failure)

    def _confirm(self, admission):
        with Session(self.engine) as session, session.begin():
            self._active(session)
            if admission is not None and not admission(session):
                raise ModelFailure("EXPERIMENT_CANCELLED")

    def confirm_admission(self):
        self._confirm(self.attempt_admission)

    def confirm_inflight_admission(self):
        admission = self.attempt_continuation
        self._confirm(admission if admission is not None else self.attempt_admission)

    def begin_attempt(
        self,
        logical_id,
        ordinal,
        spec,
        reserve,
        registry,
        *,
        transport_evidence=None,
        base_registry_digest=None,
        protected_attempts=0,
        protected_cost=None,
    ):
        with Session(self.engine) as session, session.begin():
            if self.spend_scope is not None:
                lock_issue(session, *self.spend_scope)
            run = self._active(session)
            whole_run = registry.max_run_attempts is not None or registry.max_run_usd is not None
            attempts = list(session.scalars(self._attempts(whole_run=whole_run)))
            current = [attempt for attempt in attempts if attempt.cycle == self.cycle]
            remaining = _seconds(run.deadline)
            if remaining <= 0 or len(current) + protected_attempts >= registry.max_attempts:
                raise ModelFailure("MODEL_BUDGET_EXHAUSTED")
            if (
                registry.max_run_attempts is not None
                and len(attempts) + protected_attempts >= registry.max_run_attempts
            ):
                raise ModelFailure("MODEL_RUN_BUDGET_EXHAUSTED")
            admission_reserve = (
                reserve + protected_cost
                if reserve is not None and protected_cost is not None
                else reserve
            )
            self._check_cost(
                current, admission_reserve, registry.max_usd, "MODEL_COST_BUDGET_EXHAUSTED"
            )
            self._check_cost(
                attempts, admission_reserve, registry.max_run_usd, "MODEL_RUN_COST_BUDGET_EXHAUSTED"
            )
            if self.attempt_admission is not None and not self.attempt_admission(session):
                raise ModelFailure("EXPERIMENT_CANCELLED")
            if self.spend_admission is not None:
                self.spend_admission(session, admission_reserve, protected_attempts)
            attempt = ModelAttempt(
                run_id=run.id,
                cycle=run.cycle,
                logical_id=logical_id,
                ordinal=ordinal,
                metadata_json={
                    **(
                        {"base_registry_digest": base_registry_digest}
                        if base_registry_digest is not None
                        else {}
                    ),
                    **(
                        {"run_id": run.id, "transport_evidence": transport_evidence}
                        if transport_evidence is not None
                        else {}
                    ),
                    "alias": spec.alias,
                    "provider": spec.provider,
                    "model": spec.model,
                    "compatibility": spec.compatibility,
                    "compatibility_version": spec.compatibility_version,
                    "price_date": spec.price_date.isoformat() if spec.price_date else None,
                    "registry_version": registry.version,
                    "native_streaming": registry.native_streaming,
                    "cache_write_ttl": spec.cache_write_ttl,
                    "pricing_usd_per_million": {
                        name: str(value) if value is not None else None
                        for name, value in (
                            ("input", spec.input_usd_per_million),
                            ("output", spec.output_usd_per_million),
                            ("cache_read", spec.cache_read_usd_per_million),
                            ("cache_write", spec.cache_write_usd_per_million),
                        )
                    },
                    "reserved_cost_usd": str(reserve) if reserve is not None else None,
                },
                status="STARTED",
                started_at=_now(),
            )
            session.add(attempt)
            _event(session, run, "attempt", self._audit_payload(attempt))
        return remaining

    @staticmethod
    def _check_cost(attempts, reserve, limit, code):
        if limit is None:
            return
        charges = [
            attempt.estimated_cost_usd or attempt.metadata_json.get("reserved_cost_usd")
            for attempt in attempts
        ]
        if reserve is None or any(charge is None for charge in charges):
            raise ModelFailure("MODEL_COST_UNKNOWN")
        if sum(Decimal(charge) for charge in charges) + reserve > limit:
            raise ModelFailure(code)

    @staticmethod
    def _audit_payload(attempt):
        return {
            "cycle": attempt.cycle,
            "logical_id": attempt.logical_id,
            "ordinal": attempt.ordinal,
            **attempt.metadata_json,
            "status": attempt.status,
            "usage": attempt.usage,
            "estimated_cost_usd": attempt.estimated_cost_usd,
            "error": attempt.error_code,
            "elapsed_ms": round(
                (_utc(attempt.finished_at) - _utc(attempt.started_at)).total_seconds() * 1000
            )
            if attempt.finished_at
            else None,
        }

    def finish_attempt(
        self, logical_id, ordinal, status, code, usage, price, result, *, transport_evidence=None
    ):
        with Session(self.engine) as session, session.begin():
            owned = _queue_owned(session, self.queue_admission)
            run = session.get(AgentRun, self.run_id, with_for_update=True) if owned else None
            attempt = session.get(
                ModelAttempt,
                (self.run_id, self.cycle, logical_id, ordinal),
                with_for_update=True,
            )
            if attempt.status not in {"STARTED", "UNKNOWN"}:
                return
            # Preserve provider usage already incurred. This is evidence only, not a run update.
            attempt.status, attempt.error_code, attempt.result = status, code, result
            attempt.usage, attempt.finished_at = usage, _now()
            attempt.estimated_cost_usd = str(price) if price is not None else None
            attempt.metadata_json = finish_transport_evidence(
                attempt.metadata_json, transport_evidence
            )
            if run is not None and run.cycle == self.cycle:
                _event(session, run, "attempt", self._audit_payload(attempt))


class AgentService:
    def __init__(
        self,
        engine,
        settings,
        business,
        checkpointer,
        dispatch=None,
        *,
        model=None,
        search=None,
        attempt_admission=None,
        attempt_continuation=None,
        queue_admission=None,
        provider_limiter=None,
        intent_probe=None,
    ):
        self.engine, self.settings, self.business = engine, settings, business
        self.dispatch = dispatch
        self.attempt_admission = attempt_admission
        self.attempt_continuation = attempt_continuation
        self.queue_admission = queue_admission
        self.provider_limiter = provider_limiter
        self.intent_probe = intent_probe
        saver = (
            _FencedCheckpointSaver(checkpointer, self._checkpoint_guard)
            if checkpointer is not None
            else None
        )
        self.graphs = (
            {mode: build_graph(mode, saver) for mode in ("single", "multi")}
            if checkpointer is not None
            else {}
        )
        self.investigation_enabled = getattr(settings, "chat_investigation_enabled", False)
        self.investigation_issue_budget = IssueBudget.from_settings(settings)
        if self.investigation_enabled and saver is not None:
            self.graphs[CHAT_GRAPH_PROFILE] = build_investigation_graph(saver)
        self.model = model
        if model is None and settings.model_enabled:
            self.model = ModelRunner(Registry.read(settings.model_config_path))
        if search is None:
            model_path = getattr(settings, "embedding_model_path", None)
            if getattr(settings, "embedding_enabled", False) and model_path is None:
                raise ValueError("Set embedding_model_path to the installed BGE model snapshot")
            encode = bge_encoder(str(model_path), None) if model_path else None
            encoder = f"{MODEL_ID}@{model_path.name}" if model_path else MODEL_ID
            search = PolicySearch(
                engine,
                encode=encode if getattr(settings, "embedding_enabled", False) else None,
                encoder=encoder,
            )
        self.search = search

    @contextmanager
    def _checkpoint_guard(self):
        if self.queue_admission is None:
            yield
            return
        with Session(self.engine) as session, session.begin():
            _require_queue(session, self.queue_admission)
            yield

    def index_policies(self):
        catalogue = getattr(self.business, "all_policy_versions", self.business.policy_catalogue)
        return self.search.index(catalogue())

    def _context(self, case_id, user):
        return self.business.context(case_id, user)

    @staticmethod
    def _check_version(facts, expected_version):
        if facts["case"]["version"] != expected_version:
            raise RunError("CASE_VERSION_CONFLICT", "工单版本已变化，请刷新后继续。")

    @staticmethod
    def _can_mutate(user, run=None):
        if user.role not in {"customer", "support", "tester"} or (
            run and user.role != "support" and run.owner_id != user.user_id
        ):
            raise RunError("RUN_FORBIDDEN", "当前账户无运行操作权限。", 403)

    @staticmethod
    def _get(session, case_id, run_id, *, lock=False):
        run = session.get(AgentRun, run_id, with_for_update=lock)
        if run is None or run.case_id != case_id:
            raise RunError("RUN_NOT_FOUND", "找不到该工单的运行记录。", 404)
        return run

    @staticmethod
    def _mutation(session, case_id, user, key, operation, payload):
        digest = hashlib.sha256(
            json.dumps(
                [operation, payload],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = session.get(RunMutation, (case_id, user.user_id, key))
        if existing is not None and existing.digest != digest:
            raise RunError("IDEMPOTENCY_CONFLICT", "请求标识已被其他操作使用。")
        return existing, digest

    def _enqueue(self, run_id):
        if self.dispatch is not None:
            try:
                self.dispatch(run_id)
            except (ConnectionError, OSError):
                # QUEUED is committed; queue publisher may retry the same durable run.
                return
        else:
            self.process(run_id)

    def start(
        self,
        case_id,
        user,
        *,
        expected_version,
        idempotency_key,
        mode,
        strategy,
        message,
        decision_scope=None,
        decision_previous=None,
        graph_profile=None,
        investigation_arm="auto",
        decision_turn_id=None,
    ):
        self._can_mutate(user)
        facts = self._context(case_id, user)
        if decision_previous is not None and decision_scope is None:
            raise RunError("DECISION_RUN_BINDING_INVALID", "会话候选运行缺少冻结输入。")
        if graph_profile is not None and (
            graph_profile != CHAT_GRAPH_PROFILE
            or not self.investigation_enabled
            or decision_scope is None
            or investigation_arm not in ARMS
            or (
                decision_turn_id is not None
                and (user.role != "customer" or investigation_arm != "auto")
            )
        ):
            raise RunError("INVESTIGATION_PROFILE_UNAVAILABLE", "会话调查模式不可用。")
        if graph_profile is None and (decision_turn_id is not None or investigation_arm != "auto"):
            raise RunError("INVESTIGATION_PROFILE_UNAVAILABLE", "会话调查模式不可用。")
        profile = (
            {
                "graph_profile": graph_profile,
                "investigation_arm": investigation_arm,
                "decision_turn_id": decision_turn_id,
            }
            if graph_profile is not None
            else {}
        )
        decision_request = (
            {
                "scope": decision_scope.model_dump(mode="json"),
                **profile,
                "previous": decision_previous.model_dump(mode="json")
                if decision_previous is not None
                else None,
            }
            if decision_scope is not None
            else None
        )
        with Session(self.engine) as session, session.begin():
            _lock(session, case_id)
            previous, digest = self._mutation(
                session,
                case_id,
                user,
                idempotency_key,
                "start",
                {
                    "expected_version": expected_version,
                    "mode": mode,
                    "strategy": strategy,
                    "message": message,
                    **(
                        {"decision_input": decision_request} if decision_request is not None else {}
                    ),
                },
            )
            if previous:
                return _serialize(self._get(session, case_id, previous.run_id))
            self._check_version(facts, expected_version)
            binding = (
                make_binding(
                    decision_scope,
                    decision_previous,
                    facts,
                    user,
                    message,
                    _context_digest(_with_refund_lookup(facts, message)),
                )
                if decision_scope is not None
                else None
            )
            if binding is not None:
                binding.update(profile)
                if graph_profile:
                    binding["investigation_prompt_version"] = PROMPT_VERSION
                    binding["investigation_policy_catalogue_version"] = CATALOGUE_VERSION
                if decision_turn_id is not None and not auto_turn_current(
                    session, decision_scope, user.user_id, decision_turn_id
                ):
                    raise RunError("INVESTIGATION_INPUT_STALE", "会话输入已更新。")
            active = session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.case_id == case_id,
                    AgentRun.status.in_(["QUEUED", "RUNNING", "WAITING_INPUT"]),
                )
                .with_for_update()
            )
            if decision_turn_id is not None:
                self._check_investigation_spend(session, decision_scope)
            if active:
                previous_binding = load_binding(session, active) if decision_turn_id else None
                previous_turn = (previous_binding or {}).get("decision_turn_id")
                if (
                    previous_turn is None
                    or previous_turn == decision_turn_id
                    or previous_binding["scope"]["issue_id"] != decision_scope.issue_id
                    or auto_turn_current(session, decision_scope, active.owner_id, previous_turn)
                ):
                    raise RunError("CASE_RUN_ACTIVE", "该工单已有进行中的运行。")
                _mark(session, active, "CANCELED", "INVESTIGATION_INPUT_SUPERSEDED")
            run_id = str(uuid4())
            run = AgentRun(
                id=run_id,
                thread_id=f"{case_id}:{run_id}",
                case_id=case_id,
                owner_id=user.user_id,
                actor=user.model_dump(mode="json"),
                status="QUEUED",
                mode=mode,
                strategy=strategy,
                case_version=expected_version,
                message=message,
                cycle=0,
                event_seq=0,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(run)
            _event(session, run, "status", {"status": "QUEUED", "cycle": 0})
            if binding is not None:
                _event(session, run, DECISION_INPUT_EVENT, binding)
            session.add(
                RunMutation(
                    case_id=case_id,
                    actor_id=user.user_id,
                    key=idempotency_key,
                    digest=digest,
                    run_id=run_id,
                )
            )
        self._enqueue(run_id)
        return self.get(case_id, run_id, user)

    @staticmethod
    def _check_investigation_turn(session, scope, actor_id, turn_id):
        if not auto_turn_current(session, scope, actor_id, turn_id):
            raise ModelFailure("INVESTIGATION_INPUT_STALE")

    @staticmethod
    def _check_investigation_spend(session, scope):
        uncertain = session.scalar(
            select(ModelAttempt.run_id)
            .join(AgentRun, AgentRun.id == ModelAttempt.run_id)
            .join(RunEvent, RunEvent.run_id == AgentRun.id)
            .where(
                AgentRun.case_id == scope.case_id,
                RunEvent.kind == DECISION_INPUT_EVENT,
                RunEvent.payload["graph_profile"].as_string() == CHAT_GRAPH_PROFILE,
                RunEvent.payload["scope"]["issue_id"].as_string() == scope.issue_id,
                (
                    ModelAttempt.status.in_(["STARTED", "UNKNOWN"])
                    | ModelAttempt.estimated_cost_usd.is_(None)
                ),
            )
            .limit(1)
        )
        if uncertain:
            raise RunError("INVESTIGATION_SPEND_UNCERTAIN", "上次调查的模型费用仍待核对。")

    def transition(self, case_id, run_id, user, *, expected_version, idempotency_key, operation):
        facts = self._context(case_id, user)
        with Session(self.engine) as session, session.begin():
            _lock(session, case_id)
            run = self._get(session, case_id, run_id, lock=True)
            self._can_mutate(user, run)
            previous, digest = self._mutation(
                session,
                case_id,
                user,
                idempotency_key,
                operation,
                {
                    "expected_version": expected_version,
                    "run_id": run_id,
                },
            )
            if previous:
                return _serialize(run)
            self._check_version(facts, expected_version)
            if operation == "resume":
                if load_binding(session, run) is not None:
                    raise RunError(
                        "DECISION_RUN_REQUIRES_NEW_INPUT", "请从当前会话重新登记调查输入。"
                    )
                if run.status != "WAITING_INPUT" or expected_version <= run.case_version:
                    raise RunError("RUN_NOT_RESUMABLE", "请先补充材料并刷新工单后继续。")
                run.cycle += 1
                run.case_version, run.deadline = expected_version, None
                run.actor = user.model_dump(mode="json")
                _mark(session, run, "QUEUED")
            else:
                if run.status not in {"WAITING_INPUT", "QUEUED", "RUNNING"}:
                    raise RunError("RUN_NOT_CANCELABLE", "该运行已结束。")
                _mark(session, run, "CANCELED", "USER_CANCELED")
            session.add(
                RunMutation(
                    case_id=case_id,
                    actor_id=user.user_id,
                    key=idempotency_key,
                    digest=digest,
                    run_id=run_id,
                )
            )
        if operation == "resume":
            self._enqueue(run_id)
        return self.get(case_id, run_id, user)

    @staticmethod
    def _check_event_cursor(run, after):
        if after < 0:
            raise RunError("INVALID_CURSOR", "事件游标无效。", 400)
        if after > run.event_seq:
            raise RunError("INVALID_EVENT_CURSOR", "事件游标超出当前运行记录，请刷新后重试。")

    def get(self, case_id, run_id, user, *, after=None):
        self._context(case_id, user)
        with Session(self.engine) as session:
            run = self._get(session, case_id, run_id)
            if after is not None:
                self._check_event_cursor(run, after)
            return _serialize(run)

    def list(self, case_id, user):
        self._context(case_id, user)
        with Session(self.engine) as session:
            return [
                _serialize(run)
                for run in session.scalars(
                    select(AgentRun)
                    .where(AgentRun.case_id == case_id)
                    .order_by(AgentRun.created_at.desc())
                    .limit(100)
                )
            ]

    def events(self, case_id, run_id, user, after):
        rows, _ = self.event_page(case_id, run_id, user, after)
        return rows

    def event_page(self, case_id, run_id, user, after):
        self._context(case_id, user)
        with Session(self.engine) as session:
            run = self._get(session, case_id, run_id)
            self._check_event_cursor(run, after)
            rows = [
                {
                    "seq": row.seq,
                    "kind": row.kind,
                    "payload": _event_payload(row, user),
                    "created_at": _iso(row.created_at),
                }
                for row in session.scalars(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.seq > after,
                        RunEvent.seq <= run.event_seq,
                    )
                    .order_by(RunEvent.seq)
                    .limit(EVENT_PAGE_SIZE)
                )
            ]
            return rows, run.status

    def pending(self, limit=100):
        """Return recoverable jobs. Worker obtains a per-run lock before claiming."""
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(AgentRun.id)
                    .where(
                        AgentRun.status.in_(["QUEUED", "RUNNING"]),
                    )
                    .order_by(AgentRun.updated_at)
                    .limit(limit)
                )
            )

    def _actor(self, session, run):
        actor = session.get(User, run.actor["user_id"])
        if actor is None or not actor.enabled:
            raise ModelFailure("RUN_ACTOR_REVOKED")
        return UserContext(user_id=actor.id, role=actor.role, customer_id=actor.customer_id)

    def _bind_context(self, run, snapshot, context_digest, *, binding_version, policy_resolution):
        with Session(self.engine) as session, session.begin():
            _require_queue(session, self.queue_admission)
            current = session.get(AgentRun, run.id, with_for_update=True)
            if current.status != "RUNNING" or current.cycle != run.cycle:
                raise ModelFailure("RUN_INACTIVE")
            events = select(RunEvent).where(RunEvent.run_id == run.id)
            bindings = list(
                session.scalars(
                    events.where(
                        RunEvent.kind == "context_binding",
                        RunEvent.payload["cycle"].as_integer() == run.cycle,
                    ).limit(2)
                )
            )
            if bindings:
                binding = bindings[0].payload
                if len(bindings) != 1 or binding.get("version") != binding_version:
                    raise ModelFailure("RECOVERY_CONTEXT_UNBOUND")
                if binding.get("digest") != context_digest:
                    raise ModelFailure("RECOVERY_CONTEXT_CHANGED")
                return

            prior_pause = (
                snapshot.interrupts
                and snapshot.values.get("cycle") == run.cycle - 1
                and snapshot.values.get("status") == "WAITING_INPUT"
            )
            if (snapshot.created_at is not None and not prior_pause) or session.scalar(
                select(ModelAttempt.run_id)
                .where(ModelAttempt.run_id == run.id, ModelAttempt.cycle == run.cycle)
                .limit(1)
            ):
                raise ModelFailure("RECOVERY_CONTEXT_UNBOUND")
            running_seq = session.scalar(
                events.with_only_columns(RunEvent.seq)
                .where(
                    RunEvent.kind == "status",
                    RunEvent.payload["status"].as_string() == "RUNNING",
                )
                .order_by(RunEvent.seq.desc())
                .limit(1)
            )
            if session.scalar(
                events.with_only_columns(RunEvent.seq)
                .where(RunEvent.seq > (running_seq or 0), RunEvent.kind != "status")
                .limit(1)
            ):
                raise ModelFailure("RECOVERY_CONTEXT_UNBOUND")
            _event(
                session,
                current,
                "context_binding",
                {
                    "cycle": run.cycle,
                    "version": binding_version,
                    "digest": context_digest,
                    **(
                        {
                            "policy_resolution_sha256": policy_resolution["snapshot"],
                            "selected_document_keys": sorted(
                                document_key(policy) for policy in policy_resolution["documents"]
                            ),
                        }
                        if policy_resolution and policy_resolution.get("snapshot")
                        else {}
                    ),
                },
            )

    def process(self, run_id, *, expected_cycle=None):
        if not self.graphs:
            raise RuntimeError("Worker must have a LangGraph checkpointer")
        with _lease(self.engine, run_id) as held:
            if held:
                self._process(run_id, expected_cycle)

    def _process(self, run_id, expected_cycle):
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            _require_queue(session, self.queue_admission)
            run = session.get(AgentRun, run_id, with_for_update=True)
            if (
                run is None
                or run.status not in {"QUEUED", "RUNNING"}
                or (expected_cycle is not None and run.cycle != expected_cycle)
            ):
                return
            try:
                user = self._actor(session, run)
                self._can_mutate(user, run)
            except (RunError, ModelFailure):
                _mark(session, run, "FAILED", "RUN_ACTOR_REVOKED")
                return
            if run.status == "QUEUED":
                seconds = self.model.registry.deadline_seconds if self.model else 120
                run.deadline = _now() + timedelta(seconds=seconds)
                _mark(session, run, "RUNNING")

        try:
            with Session(self.engine) as session:
                decision_input = load_binding(session, run)
            binding = decision_input or {}
            profile = binding.get("graph_profile")
            if profile and binding.get("investigation_prompt_version") != PROMPT_VERSION:
                raise ModelFailure("INVESTIGATION_PROMPT_VERSION_CHANGED")
            if (
                profile
                and binding.get("investigation_policy_catalogue_version") != CATALOGUE_VERSION
            ):
                raise ModelFailure("INVESTIGATION_POLICY_CATALOGUE_REQUIRED")
            if profile and (profile != CHAT_GRAPH_PROFILE or profile not in self.graphs):
                raise ModelFailure("INVESTIGATION_PROFILE_UNAVAILABLE")
            facts = _with_refund_lookup(self._context(run.case_id, user), run.message)
            self._check_version(facts, run.case_version)
            context_digest = _context_digest(facts)
            graph = self.graphs[profile or run.mode]
            config = {"configurable": {"thread_id": run.thread_id}, "recursion_limit": 30}
            snapshot = (
                _private_graph(graph.get_state, config) if profile else graph.get_state(config)
            )
            if decision_input is not None and decision_input["context_sha256"] != context_digest:
                raise ModelFailure("DECISION_RUN_CONTEXT_CHANGED")
            lookup_targeted = targeted_refund(facts.get("refund_lookup"))
            self._bind_context(
                run,
                snapshot,
                context_digest,
                binding_version=REFUND_CONTEXT_BINDING_VERSION
                if lookup_targeted
                else CONTEXT_BINDING_VERSION,
                policy_resolution=facts.get("policy_resolution"),
            )
            scope = DecisionScope.model_validate(binding["scope"]) if profile else None
            if scope is not None:
                self._check_policy_catalogue(scope)
            turn_id = binding.get("decision_turn_id")

            def guard(session, callback):
                if turn_id:
                    self._check_investigation_turn(session, scope, run.owner_id, turn_id)
                return callback(session) if callback is not None else True

            with Session(self.engine) as session:
                guard(session, None)

            def spend(session, reserve, protected_attempts):
                try:
                    admit_issue(
                        session,
                        scope.conversation_id,
                        scope.issue_id,
                        self.investigation_issue_budget,
                        to_microusd(reserve),
                        protected_calls=protected_attempts,
                    )
                except IssueBudgetDenied as error:
                    raise ModelFailure(str(error)) from None

            audit = RunAudit(
                self.engine,
                run,
                partial(guard, callback=self.attempt_admission)
                if turn_id
                else self.attempt_admission,
                self.provider_limiter,
                queue_admission=self.queue_admission,
                attempt_continuation=partial(guard, callback=self.attempt_continuation)
                if turn_id
                else self.attempt_continuation,
                spend_admission=spend if profile else None,
                spend_scope=(scope.conversation_id, scope.issue_id) if profile else None,
            )
            if (
                not profile
                and self.intent_probe is not None
                and self.model is not None
                and run.cycle == 0
                and not lookup_targeted
            ):
                self.intent_probe(
                    audit,
                    run.message or facts["case"].get("description", ""),
                    self.model,
                    run.strategy,
                )
            context = GraphContext(
                audit=audit,
                facts=facts,
                message=run.message or facts["case"].get("description", ""),
                strategy=run.strategy,
                model=self.model,
                search=self.search,
                validate_draft=partial(
                    self.business.validate_draft,
                    run.case_id,
                    user,
                    expected_version=run.case_version,
                ),
            )
            if profile:
                context = self._investigation_context(context, scope, binding["investigation_arm"])
            invoke = partial(_private_graph, graph.invoke) if profile else graph.invoke
            if not snapshot.values:
                state = invoke({"cycle": run.cycle}, config, context=context)
            elif snapshot.interrupts and snapshot.values.get("cycle") != run.cycle:
                state = invoke(Command(resume={"cycle": run.cycle}), config, context=context)
            elif snapshot.interrupts or not snapshot.next:
                state = snapshot.values
            else:
                state = invoke(None, config, context=context)
            with Session(self.engine) as session:
                user = self._actor(session, run)
                self._can_mutate(user, run)
            latest = _with_refund_lookup(self._context(run.case_id, user), run.message)
            if scope is not None:
                self._check_policy_catalogue(scope)
            if _context_digest(latest) != context_digest:
                raise ModelFailure("FACTS_CHANGED_DURING_RUN")
            if _seconds(run.deadline) <= 0:
                raise ModelFailure("RUN_DEADLINE_EXCEEDED")
            self._finish(
                run,
                state["status"],
                state.get("reason"),
                state.get("output"),
                decision_context=context_digest if decision_input is not None else None,
                decision_guard=partial(guard, callback=None) if turn_id else None,
            )
        except QueueFenceLost:
            raise
        except (BusinessError, ModelFailure, RunError) as error:
            self._finish(run, "NEEDS_REVIEW", error.code, None)
        except Exception:
            self._finish(run, "FAILED", "RUN_INTERNAL_ERROR", None)
            raise

    def _check_policy_catalogue(self, scope):
        catalogue = (scope.frozen_context or {}).get("policy_catalogue")
        if catalogue and digest(self.business.all_policy_versions()) != catalogue.get(
            "documents_sha256"
        ):
            raise ModelFailure("INVESTIGATION_POLICY_CATALOGUE_CHANGED")

    def _investigation_context(self, legacy, scope, arm):
        try:
            value = build_input(scope)
            if value.policy_catalogue_version != CATALOGUE_VERSION:
                raise InputUnavailable("INVESTIGATION_POLICY_CATALOGUE_REQUIRED")
            route = route_input(value)
            investigation_tools = InvestigationTools(value, policy_search=legacy.search)
        except InputUnavailable as error:
            value, route, investigation_tools = None, route_unavailable(error), None
        model, reserve = None, None
        if legacy.model and route.path != "PROGRAM":
            registry = legacy.model.registry
            caps = [cap for cap in (registry.max_usd, registry.max_run_usd) if cap is not None]
            if caps and min(caps) > 0:
                model = legacy.model.with_evaluation_limits(calls=8, budget=min(caps))
                costs = [
                    _reserve_price(spec, INVESTIGATION_INPUT_TOKEN_LIMIT, P4_OUTPUT_RESERVE)
                    for spec in registry.models
                    if "customer_conversation" in spec.data_scopes and spec.cache_write_ttl != "12h"
                ]
                priced = [cost for cost in costs if cost is not None]
                if priced:
                    reserve = 2 * max(priced)
        return InvestigationContext(
            value,
            route,
            model,
            legacy.audit,
            investigation_tools,
            legacy.validate_draft,
            legacy.strategy,
            frozenset(scope.line_ids),
            frozenset(
                ref.id
                for ref in scope.input_refs
                if ref.kind in {"FACT", "POLICY"} and ref.id in scope.evidence_ids
            ),
            reserve,
            arm,
            scope.frozen_context if (scope.frozen_context or {}).get("policy_catalogue") else None,
        )

    def _finish(self, run, status, reason, output, *, decision_context=None, decision_guard=None):
        with Session(self.engine) as session, session.begin():
            _require_queue(session, self.queue_admission)
            current = session.get(AgentRun, run.id, with_for_update=True)
            if current.status == "RUNNING" and current.cycle == run.cycle:
                if self.attempt_admission is not None and not self.attempt_admission(session):
                    status, reason, output = "CANCELED", "EXPERIMENT_CANCELLED", None
                if decision_guard is not None:
                    try:
                        decision_guard(session)
                    except ModelFailure as error:
                        status, reason, output, decision_context = (
                            "CANCELED",
                            error.code,
                            None,
                            None,
                        )
                _mark(session, current, status, reason, output)
                if decision_context is not None and status in {
                    "WAITING_INPUT",
                    "NEEDS_REVIEW",
                    "COMPLETED",
                }:
                    _event(
                        session,
                        current,
                        DECISION_READY_EVENT,
                        {
                            "cycle": current.cycle,
                            "context_sha256": decision_context,
                            "output_sha256": digest([status, reason, output]),
                        },
                    )
