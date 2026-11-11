"""Run paired chat investigations via the existing AgentService worker core."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import NoResultFound, SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.business.service import BusinessService
from aftercare.business.storage import verify_object
from aftercare.conversations.decisions import ConversationDecisions
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import Attachment, Conversation
from aftercare.db import Base, User
from aftercare.decisions.contracts import Candidate, DecisionScope, Option
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.chat.manifest import bound_rows
from aftercare.evaluation.runner import NamespacedObjects, _model_attempts, _namespace
from aftercare.intelligence.decision_candidate import load_binding, project_candidate
from aftercare.intelligence.models import (
    _DEFAULT_SYNC_TRANSPORT,
    P4_INPUT_LIMIT,
    P4_OUTPUT_RESERVE,
    ModelRunner,
)
from aftercare.intelligence.service import AgentService, RunError
from aftercare.intelligence.tables import AgentRun, RunEvent, RunMutation
from aftercare.investigation.catalogue import CATALOGUE_VERSION
from aftercare.investigation.graph import CHAT_GRAPH_PROFILE
from aftercare.investigation.input import build_input
from aftercare.investigation.issue_budget import IssueBudget
from aftercare.investigation.prompt_evidence import PROMPT_VERSION
from aftercare.investigation.routing import route_input
from aftercare.investigation.tools import InvestigationTools

_PATHS = {"strong_single": "SINGLE", "single_staged": "STAGED", "multi": "MULTI"}
_PHASES = {
    "SINGLE": {"single": "strong_single"},
    "STAGED": {"staged-fact": "fact", "staged-policy": "policy", "verify-0": "verifier"},
    "MULTI": {"fact": "fact", "policy": "policy", "verify-0": "verifier", "verify-1": "verifier"},
    "PROGRAM": {},
}
_TERMINAL = {"COMPLETED", "NEEDS_REVIEW", "FAILED", "CANCELED", "WAITING_INPUT"}
_PROJECTABLE = {"COMPLETED", "NEEDS_REVIEW", "WAITING_INPUT"}
_ERRORS = (BusinessError, RunError, SQLAlchemyError, OSError, RuntimeError, ValueError)
_INPUT_CHAT = {
    "chat_conversations",
    "chat_messages",
    "chat_attachments",
    "chat_case_links",
    "chat_material_sources",
    "chat_analysis_turns",
    "chat_model_attempts",
    "chat_observations",
}


@dataclass(frozen=True)
class FrozenCase:
    case_id: str
    repetition: int
    source: BusinessService
    actor: UserContext
    conversation_id: str
    issue_id: str
    decision_at: datetime
    provenance: str


@dataclass(frozen=True)
class _Source:
    definition: FrozenCase
    scope: DecisionScope
    images: tuple
    record: dict


def _images(db, storage, scope):
    result = []
    for image in scope.frozen_context["attachments"]:
        row = db.get_one(Attachment, image["id"])
        key = ConversationService._object_key(
            scope.conversation_id, image["message_id"], row.position, image["sha256"]
        )
        if (row.object_key, row.message_id, row.sha256, row.size, row.content_type) != (
            key,
            image["message_id"],
            image["sha256"],
            image["size"],
            image["content_type"],
        ):
            raise ValueError("CHAT_EVAL_IMAGE_BINDING_MISMATCH")
        original = storage.get(key, row.size)
        verify_object(original, row.size, row.sha256)
        result.append(({**image, "position": row.position}, key, original))
    return tuple(result)


def _source(case):
    settings, engine = case.source.settings, case.source.engine
    if (
        engine.dialect.name != "sqlite"
        or settings.profile != "test"
        or not settings.chat_decisions_enabled
        or not settings.chat_investigation_enabled
        or getattr(settings, "embedding_enabled", False)
        or case.actor.role != "support"
        or case.decision_at.utcoffset() is None
        or not case.provenance.strip()
    ):
        raise ValueError("CHAT_EVAL_ISOLATED_TEST_SOURCE_REQUIRED")
    business = BusinessService(
        engine, settings, storage=case.source.storage, clock=lambda: case.decision_at
    )
    with Session(engine) as db, db.begin():
        if (
            db.scalar(select(func.count()).select_from(Conversation)) != 1
            or db.scalar(select(func.count()).select_from(AgentRun))
            or db.scalar(select(func.count()).select_from(DecisionSnapshot))
        ):
            raise ValueError("CHAT_EVAL_PRISTINE_SOURCE_REQUIRED")
        scope = ConversationDecisions(business).resolve(
            db, case.actor, case.conversation_id, case.issue_id
        )
        images = _images(db, business.storage, scope)
    value = build_input(scope)
    if value.policy_catalogue_version != CATALOGUE_VERSION:
        raise ValueError("CHAT_EVAL_COMPLETE_CATALOGUE_REQUIRED")
    # Physical object keys, run IDs and runtime timestamps never enter this record.
    paired = {
        key: data
        for key, data in scope.frozen_context.items()
        if key not in {"legacy_agent_input", "policies", "eligibility"}
    }
    paired["attachments"] = [item for item, _, _ in images]
    serialized = scope.model_dump(mode="json")
    issue_budget = IssueBudget.from_settings(settings)
    record = {
        "case_id": case.case_id,
        "repetition": case.repetition,
        "provenance": case.provenance,
        "scope": serialized,
        "scope_sha256": digest(serialized),
        "input_sha256": scope.input_sha256,
        "dependency_sha256": scope.dependency_sha256,
        "relevant_input_sha256": value.relevant_input_sha256,
        "paired_sha256": digest(["chat-eval-pair-v1", paired]),
        "catalogue_sha256": value.policy_context["catalogue_sha256"],
        "decision_at": value.policy_context["timestamps"]["decision_at"],
        "images": paired["attachments"],
        "issue_budget": {"calls": issue_budget.calls, "microusd": issue_budget.microusd},
    }
    return _Source(case, scope, images, record)


def _tables():
    return [
        table
        for table in Base.metadata.sorted_tables
        if table.name == "users" or table.name.startswith(("business_", "chat_", "agent_"))
    ]


@contextmanager
def _arm_engine(manifest, row, *, create):
    namespace = _namespace(row["id"])
    target = manifest.directory / f"{namespace}.sqlite"
    if create:
        target.touch(mode=0o600, exist_ok=False)
    if not target.is_file() or target.is_symlink():
        raise ValueError("CHAT_EVAL_ARM_STORAGE_UNAVAILABLE")
    engine = create_engine(f"sqlite:///{target}")
    try:
        yield engine, namespace
    finally:
        engine.dispose()


def _populate(source, engine, storage):
    case = source.definition
    tables = _tables()
    Base.metadata.create_all(engine, tables=tables)
    with case.source.engine.connect() as original, engine.begin() as destination:
        for table in tables:
            if not table.name.startswith("business_") and table.name not in _INPUT_CHAT:
                continue
            rows = list(original.execute(select(table)).mappings())
            if rows:
                destination.execute(insert(table), rows)
        destination.execute(
            insert(User).values(
                id=case.actor.user_id,
                role="support",
                display_name="Chat evaluation",
                token_hash=digest(["chat-eval-noncredential", case.actor.user_id]),
                enabled=True,
            )
        )
    for item, key, data in source.images:
        storage.put(key, data, item["content_type"])


def _sum_attempts(attempts, field):
    return str(
        sum((Decimal(item[field]) for item in attempts if item[field] is not None), Decimal(0))
    )


def _elapsed(start, end):
    return round((end - start).total_seconds() * 1000) if start and end else None


def _failure(status, reason):
    return {
        "status": status,
        "reason": reason,
        "candidate_status": "MISSING",
        "input_comparable": False,
        "attempts": [],
        "actual_billed_usd": None,
        "known_estimated_usd": None if status == "UNKNOWN" else "0",
        "reserved_usd": None if status == "UNKNOWN" else "0",
        "latency_censored": True,
    }


def _candidate_bound(snapshot, run, source, expected):
    if snapshot is None:
        return False, False
    try:
        candidate = Candidate.model_validate(snapshot.candidate)
    except ValueError:
        return False, False
    bound = (
        snapshot.origin == "AGENT"
        and snapshot.actor_id == run.owner_id
        and snapshot.run_id == candidate.run_id == run.id
        and snapshot.conversation_id == source.scope.conversation_id
        and snapshot.issue_id == source.scope.issue_id
        and snapshot.input == expected
        and candidate.graph_version == CHAT_GRAPH_PROFILE
        and run.status in _PROJECTABLE
    )
    return bound, any(isinstance(option, Option) for option in candidate.options)


def _route_evidence(row, source, events, attempts, *, has_candidate, executable):
    routes = [event.payload for event in events if event.kind == "investigation_route"]
    phases = [event.payload for event in events if event.kind == "investigation_input"]
    if not routes:
        return None, phases, not (attempts or phases or has_candidate)
    path = routes[0].get("selected_path")
    planned = route_input(build_input(source.scope)).path
    expected = "PROGRAM" if planned == "PROGRAM" else _PATHS[row["arm"]]
    if len(routes) != 1 or path != expected or routes[0].get("arm") != row["arm"]:
        return path, phases, False
    if path == "PROGRAM":
        return path, phases, not (phases or attempts or executable)
    phase_roles = {phase.get("phase_id"): phase.get("role") for phase in phases}
    allowed = _PHASES[path].copy()
    if path == "MULTI" and phase_roles.get("recheck") in {"fact", "policy"}:
        allowed["recheck"] = phase_roles["recheck"]
    initial = set(_PHASES[path]) - {"verify-1"}
    prefixes = {f"p4:{PROMPT_VERSION}:{phase}" for phase in phase_roles}
    succeeded = {
        item["logical_id"].rsplit(":", 1)[0] for item in attempts if item["status"] == "SUCCEEDED"
    }
    required = initial | ({"recheck", "verify-1"} if "recheck" in phase_roles else set())
    valid = (
        len(phase_roles) == len(phases)
        and all(
            allowed.get(phase) == role and phase in allowed for phase, role in phase_roles.items()
        )
        and all(phase.get("prompt_version") == PROMPT_VERSION for phase in phases)
        and all(item["logical_id"].rsplit(":", 1)[0] in prefixes for item in attempts)
        and (not has_candidate or bool(phases))
        and (not executable or {f"p4:{PROMPT_VERSION}:{phase}" for phase in required} <= succeeded)
        and ("verify-1" not in phase_roles or "recheck" in phase_roles)
    )
    return path, phases, valid


def _audit(engine, storage, row, source, controls):
    with Session(engine) as db:
        mutation = db.get(
            RunMutation, (source.scope.case_id, source.definition.actor.user_id, row["id"])
        )
        if mutation is None:
            return None
        run = db.get_one(AgentRun, mutation.run_id)
        try:
            binding = load_binding(db, run)
        except BusinessError:
            binding = None
        snapshots = list(
            db.scalars(select(DecisionSnapshot).where(DecisionSnapshot.run_id == run.id).limit(2))
        )
        snapshot = snapshots[0] if snapshots else None
        events = list(
            db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))
        )
        attempts = _model_attempts(engine, run.id)
        expected = source.scope.model_dump(mode="json")
        bound, executable = _candidate_bound(snapshot, run, source, expected)
        route, phases, route_valid = _route_evidence(
            row,
            source,
            events,
            attempts,
            has_candidate=snapshot is not None,
            executable=executable,
        )
        valid = (
            binding is not None
            and run.case_id == source.scope.case_id
            and run.owner_id == source.definition.actor.user_id
            and binding.get("scope") == expected
            and binding.get("investigation_arm") == row["arm"]
            and binding.get("graph_profile") == CHAT_GRAPH_PROFILE
            and binding.get("investigation_prompt_version") == PROMPT_VERSION
            and binding.get("investigation_policy_catalogue_version") == CATALOGUE_VERSION
            and len(snapshots) <= 1
            and (snapshot is None or bound)
            and route_valid
            and (route is not None or run.status != "COMPLETED")
            and all(
                item["model"] == controls["model"]
                and item["alias"] == controls["alias"]
                and item.get("base_registry_digest") == controls["base_registry_sha256"]
                and item.get("transport_evidence", {}).get("max_output_tokens")
                == controls["output_limit"]
                for item in attempts
            )
        )
        unknown = run.status not in _TERMINAL or any(
            item["status"] in {"STARTED", "UNKNOWN"} for item in attempts
        )
        processing = next(
            (
                event.created_at
                for event in events
                if event.kind == "status" and event.payload["status"] == "RUNNING"
            ),
            None,
        )
        settled = run.updated_at if not unknown else None
        comparable = valid and route is not None
        result = {
            "status": "FAILED" if not valid else "UNKNOWN" if unknown else run.status,
            "run_status": run.status,
            "reason": "CHAT_EVAL_RUN_BINDING_MISMATCH" if not valid else run.reason,
            "run_id": run.id,
            "route": route,
            "input_comparable": comparable,
            "comparability_reason": (
                "BINDING_MISMATCH" if not valid else "ROUTE_NOT_REACHED" if route is None else None
            ),
            "phases": [phase.get("phase_id") for phase in phases],
            "rechecks": [
                event.payload for event in events if event.kind == "investigation_recheck"
            ],
            "snapshot_id": snapshot.id if snapshot is not None else None,
            "candidate_status": (
                "MISSING"
                if snapshot is None
                else "AVAILABLE"
                if comparable and not unknown
                else "UNUSABLE"
            ),
            "program_checks": snapshot.checks if snapshot is not None else {},
            "attempts": attempts,
            "known_estimated_usd": _sum_attempts(attempts, "estimated_cost_usd"),
            "reserved_usd": _sum_attempts(attempts, "reserved_cost_usd"),
            "actual_billed_usd": None,
            "unknown_cost_attempts": sum(item["estimated_cost_usd"] is None for item in attempts),
            "unknown_usage_attempts": sum(item["usage"] is None for item in attempts),
            "started_at": run.created_at.isoformat(),
            "settled_at": settled.isoformat() if settled else None,
            "queued_ms": _elapsed(run.created_at, processing),
            "worker_ms": _elapsed(processing, settled),
            "candidate_persisted_ms": _elapsed(run.created_at, snapshot.created_at)
            if snapshot
            else None,
            "latency_censored": unknown,
        }
        try:
            if _images(db, storage, source.scope) != source.images:
                raise ValueError("CHAT_EVAL_ARM_IMAGES_CHANGED")
        except _ERRORS as error:
            integrity_failed = isinstance(error, (ValueError, NoResultFound)) or (
                isinstance(error, BusinessError)
                and error.code in {"MATERIAL_INTEGRITY_ERROR", "MATERIAL_OBJECT_MISSING"}
            )
            result.update(
                status="FAILED" if integrity_failed else "UNKNOWN",
                reason="CHAT_EVAL_ARM_IMAGES_CHANGED"
                if integrity_failed
                else "CHAT_EVAL_IMAGE_READ_UNKNOWN",
                input_comparable=False,
                comparability_reason="IMAGE_MISMATCH" if integrity_failed else "IMAGE_UNVERIFIED",
                candidate_status="UNUSABLE" if snapshot is not None else "MISSING",
            )
        return result


class ChatEvaluationRunner:
    """Explicit local test seam. No settings or credential discovery."""

    def __init__(self, model: ModelRunner, checkpointer, *, calls: int, budget: Decimal):
        self.model = model.with_evaluation_limits(calls=calls, budget=budget)
        self.checkpointer = checkpointer
        registry = self.model.registry
        if (
            len(registry.models) != 1
            or registry.native_streaming
            or registry.models[0].provider != "fixture"
            or model.transport is _DEFAULT_SYNC_TRANSPORT
        ):
            raise ValueError("CHAT_EVAL_FIXED_MODEL_REQUIRED")
        self.controls = {
            "evidence_grade": "CONTROLLED_WORKER_CORE",
            "model": registry.models[0].model,
            "alias": registry.models[0].alias,
            "registry_sha256": digest(registry.model_dump(mode="json")),
            "base_registry_sha256": self.model.base_registry_digest,
            "calls": calls,
            "budget_usd": str(budget),
            "effective_calls": registry.max_run_attempts,
            "effective_budget_usd": str(registry.max_run_usd),
            "max_attempts": registry.max_attempts,
            "deadline_seconds": registry.deadline_seconds,
            "input_limit": P4_INPUT_LIMIT,
            "output_limit": P4_OUTPUT_RESERVE,
            "graph": CHAT_GRAPH_PROFILE,
            "prompt": PROMPT_VERSION,
            "catalogue": CATALOGUE_VERSION,
            "retrieval": "BM25",
        }

    def run(self, manifest, campaign, cases):
        with manifest.lock():
            keys = [(case.repetition, case.case_id) for case in cases]
            if len(set(keys)) != len(keys):
                raise ValueError("CHAT_EVAL_DUPLICATE_OR_EMPTY_CASES")
            sources = {(case.repetition, case.case_id): _source(case) for case in cases}
            records = [sources[(case.repetition, case.case_id)].record for case in cases]
            if len({digest(record["issue_budget"]) for record in records}) > 1:
                raise ValueError("CHAT_EVAL_ISSUE_BUDGET_MISMATCH")
            controls = {
                **self.controls,
                "tools": sorted(
                    {
                        item["function"]["name"]
                        for role in ("strong_single", "fact", "policy", "verifier")
                        for item in InvestigationTools(
                            build_input(next(iter(sources.values())).scope)
                        ).schemas(role)
                    }
                )
                if sources
                else [],
            }
            plan = manifest.prepare(campaign, records, controls)
            results = []
            for row in bound_rows(plan):
                source = sources[(row["repetition"], row["case_id"])]
                receipt = manifest.result(row)
                if receipt is None:
                    receipt = self._run_row(manifest, row, source)
                    manifest.finish(row, receipt)
                    receipt = manifest.result(row)
                results.append(receipt)
            return {"plan_sha256": digest(plan), "rows": results}

    def _run_row(self, manifest, row, source):
        case = source.definition
        if manifest.started(row):
            if self._ready(manifest, row):
                return self._reconcile(manifest, row, source)
            return _failure("UNKNOWN", "CHAT_EVAL_INTERRUPTED")
        manifest.start(row)
        try:
            with _arm_engine(manifest, row, create=True) as (engine, namespace):
                storage = NamespacedObjects(case.source.storage, namespace)
                _populate(source, engine, storage)
                business = BusinessService(
                    engine, case.source.settings, storage=storage, clock=lambda: case.decision_at
                )
                with Session(engine) as db, db.begin():
                    cloned = ConversationDecisions(business).resolve(
                        db, case.actor, case.conversation_id, case.issue_id
                    )
                    if cloned != source.scope or _images(db, storage, cloned) != source.images:
                        raise ValueError("CHAT_EVAL_ARM_INPUT_MISMATCH")
                manifest.mark_ready(row, namespace)
                agent = AgentService(
                    engine,
                    case.source.settings,
                    business,
                    self.checkpointer,
                    model=self.model,
                    dispatch=lambda _run_id: None,
                )
                started = agent.start(
                    cloned.case_id,
                    case.actor,
                    expected_version=cloned.business_versions["case"],
                    idempotency_key=row["id"],
                    mode="single",
                    strategy=self.controls["alias"],
                    message=cloned.frozen_context["legacy_agent_input"]["message"],
                    decision_scope=source.scope,
                    graph_profile=CHAT_GRAPH_PROFILE,
                    investigation_arm=row["arm"],
                )
                run_id = started["id"]
                agent.process(run_id)
                settled = agent.get(cloned.case_id, run_id, case.actor)
                projection_error = None
                if settled["status"] in _PROJECTABLE:
                    try:
                        with Session(engine) as db, db.begin():
                            project_candidate(
                                db, ConversationDecisions(business).records, case.actor, run_id
                            )
                    except BusinessError as error:
                        projection_error = error.code
                result = _audit(engine, storage, row, source, self.controls)
                if result is None:
                    return _failure("UNKNOWN", "CHAT_EVAL_RUN_UNBOUND")
                if projection_error:
                    result.update(
                        status="FAILED",
                        reason="CHAT_EVAL_CANDIDATE_PROJECTION_FAILED",
                        projection_error=projection_error,
                    )
                return result
        except _ERRORS as error:
            if self._ready(manifest, row):
                return self._reconcile(manifest, row, source)
            reason = getattr(error, "code", None)
            if isinstance(error, ValueError) and str(error).startswith("CHAT_EVAL_"):
                reason = str(error)
            return _failure("FAILED", reason or type(error).__name__)

    def _reconcile(self, manifest, row, source):
        try:
            with _arm_engine(manifest, row, create=False) as (engine, namespace):
                storage = NamespacedObjects(source.definition.source.storage, namespace)
                result = _audit(engine, storage, row, source, self.controls)
            return result or _failure("UNKNOWN", "CHAT_EVAL_RUN_UNBOUND")
        except _ERRORS as error:
            return _failure("UNKNOWN", f"CHAT_EVAL_RECOVERY_{type(error).__name__}")

    @staticmethod
    def _ready(manifest, row):
        receipt = manifest.ready(row)
        if receipt is not None and receipt["namespace"] != _namespace(row["id"]):
            raise ValueError("CHAT_EVAL_RECEIPT_BINDING_MISMATCH")
        return receipt is not None
