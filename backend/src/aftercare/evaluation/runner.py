"""Replay frozen customer and staff events through the application services."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.models import Approval, Case, Event, Execution, Inventory, Policy, Proposal
from aftercare.business.payment import LocalPaymentGateway
from aftercare.business.rules import digest, utc
from aftercare.business.service import BusinessService, initialize
from aftercare.business.storage import S3ObjectStore
from aftercare.db import Base, User
from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry
from aftercare.intelligence.policies import PolicySearch
from aftercare.intelligence.retrieval_evidence import MODEL_ID, bound_encoder
from aftercare.intelligence.service import AgentService, RunError
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

from .dataset import material_bytes
from .observations import (
    blind_model_intent,
    citation_observations,
    draft_observations,
    finance_observations,
    rule_intent,
)
from .retrieval_evidence import (
    SCHEMA_VERSION,
    catalogue_sha256,
    fixture_policies,
    read_observed_index,
)

MISSING = object()
APPLICATION_TABLES = {"users", "policy_vectors"}
TIMEOUT_CODES = {
    "RUN_DEADLINE_EXCEEDED",
    "MODEL_TIMEOUT",
    "TOOL_TIMEOUT",
}
TRANSPORT_ERROR_CODES = {"MODEL_TIMEOUT_OR_CONNECTION", "MODEL_CONNECTION_FAILURE"}
SAFETY_REJECTIONS = {
    "NOT_FOUND",
    "FORBIDDEN",
    "APPROVAL_REQUIRED",
    "REFUND_CAP_EXCEEDED",
    "STALE_PROPOSAL",
    "DUPLICATE_MATTER",
    "STOCK_INSUFFICIENT",
    "PARTIAL_RESOLUTION_REQUIRED",
}
STOP_CODES = TIMEOUT_CODES | TRANSPORT_ERROR_CODES | {"EXPERIMENT_CANCELLED"}
TEST_STORAGE_ENDPOINTS = {"http://127.0.0.1:58351", "http://objects-test:8333"}


class UnsupportedFixture(Exception):
    def __init__(self, code):
        self.code = code


class InterruptedTransport(BaseException):
    """In-process interruption after the model attempt has been durably started."""


class SnapshotClock:
    def __init__(self, stamp):
        self.current = datetime.fromisoformat(stamp)
        if self.current.tzinfo is None:
            raise UnsupportedFixture("FIXTURE_TIME_UNKNOWN")

    def __call__(self):
        return self.current

    def advance(self):
        self.current += timedelta(minutes=16)


class NamespacedObjects:
    def __init__(self, storage, namespace):
        self.storage = storage
        self.prefix = f"evaluation/{namespace}/"

    def put(self, key, data, content_type):
        self.storage.put(self.prefix + key, data, content_type)

    def get(self, key, expected_size):
        return self.storage.get(self.prefix + key, expected_size)

    def presign(self, key, content_type):
        return self.storage.presign(self.prefix + key, content_type)


class PaymentFault:
    def __init__(self, engine):
        self.gateway = LocalPaymentGateway(engine)
        self.fault = None
        self.triggered = False

    def submit(self, *args):
        if self.fault == "timeout_no_receipt":
            self.triggered = True
            raise TimeoutError()
        result = self.gateway.submit(*args)
        if self.fault == "commit_then_disconnect":
            self.triggered = True
            raise TimeoutError()
        return result

    def lookup(self, *args):
        return self.gateway.lookup(*args)


def _namespace(result_id):
    return "eval_" + UUID(result_id).hex


@contextmanager
def _isolated_engine(engine, result_id):
    namespace = _namespace(result_id)
    tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name in APPLICATION_TABLES or name.startswith(("business_", "agent_"))
    ]
    if engine.dialect.name == "postgresql":
        with engine.begin() as db:
            db.execute(CreateSchema(namespace))
        isolated = engine.execution_options(schema_translate_map={None: namespace})
        Base.metadata.create_all(isolated, tables=tables)
        yield isolated, namespace
    else:
        # Used only for injected local checks, never as a runtime replacement for PG.
        with tempfile.TemporaryDirectory(prefix="aftercare-eval-") as folder:
            isolated = create_engine(f"sqlite:///{Path(folder) / 'case.db'}")
            try:
                Base.metadata.create_all(isolated, tables=tables)
                yield isolated, namespace
            finally:
                isolated.dispose()


def _actor_set(fixture):
    owner = fixture["business"]["customers"][0]["id"]
    return {
        name: UserContext(user_id=f"eval-{name}", role=role, customer_id=customer)
        for name, role, customer in (
            ("customer", "customer", owner),
            ("outsider", "customer", "not-owner"),
            ("support", "support", None),
            ("approver", "approver", None),
        )
    }


def _seed(engine, fixture, actors):
    data = fixture["business"]
    initialize(engine, {**data, "policies": []})
    with Session(engine) as db, db.begin():
        # The snapshot can intentionally contain concurrent or future revisions.
        db.execute(delete(Policy))
        for policy in fixture_policies(fixture):
            db.add(
                Policy(
                    id=policy["id"],
                    version=policy["version"],
                    data=policy,
                    effective_at=datetime.fromisoformat(policy["effective_at"]),
                )
            )
        for stock in fixture.get("inventory", []):
            db.get_one(Inventory, stock["product_id"]).available_quantity = stock[
                "available_quantity"
            ]
        for actor in actors.values():
            db.add(
                User(
                    id=actor.user_id,
                    token_hash=digest([actor.user_id, str(uuid4())]),
                    enabled=True,
                    role=actor.role,
                    customer_id=actor.customer_id,
                    display_name=actor.user_id,
                )
            )
        order = data["orders"][0]
        for history in fixture.get("refund_history", []):
            _seed_history(db, history, order)


def _seed_history(db, history, order):
    """Represent a fixture's preexisting ledger. Never submit its payment."""
    at = datetime.fromisoformat(history["created_at"])
    lines = history["lines"]
    line_ids = sorted(history.get("case_line_ids") or [line["line_id"] for line in lines])
    scope = digest(line_ids)
    payload = digest(history)
    case = Case(
        id=str(uuid4()),
        ticket_id=str(uuid4()),
        order_id=order["id"],
        customer_id=order["customer_id"],
        intent=history.get("intent", "return"),
        description="冻结快照中的历史售后记录",
        scope_hash=scope,
        line_ids=line_ids,
        status="RESOLVED",
        version=1,
        materials_version=0,
        facts_version=0,
        facts={},
        created_at=at,
    )
    db.add(case)
    db.flush()
    proposal = Proposal(
        id=str(uuid4()),
        case_id=case.id,
        version=1,
        action="REFUND",
        lines=lines,
        notes="历史快照",
        policy_ids=[],
        binding={},
        payload_hash=payload,
        amount_cents=history["amount_cents"],
        status="EXECUTED",
        proposer_id="fixture-history",
        created_at=at,
    )
    db.add(proposal)
    db.flush()
    approval = Approval(
        id=str(uuid4()),
        case_id=case.id,
        proposal_id=proposal.id,
        proposal_version=1,
        payload_hash=payload,
        decision="APPROVED",
        notes="历史快照",
        approver_id="fixture-history",
        created_at=at,
        expires_at=at + timedelta(minutes=15),
    )
    db.add(approval)
    db.flush()
    db.add(
        Execution(
            id=history["id"],
            business_key=digest(["historical", history["id"]]),
            case_id=case.id,
            order_id=order["id"],
            proposal_id=proposal.id,
            approval_id=approval.id,
            payload_hash=payload,
            action="REFUND",
            lines=lines,
            amount_cents=history["amount_cents"],
            status=history["status"],
            receipt=history["receipt"],
            actor_id="fixture-history",
            created_at=at,
            updated_at=at,
        )
    )


def _crash_model(config):
    selected = config["strategy"] if config["strategy"] != "routed" else "economy"
    fixture_model = ModelSpec(
        alias=selected,
        provider="evaluation",
        model="fixture-interrupted-attempt",
        endpoint="http://127.0.0.1:9",
        api_key_env="UNUSED_EVAL_FIXTURE_TOKEN",
    )
    registry = Registry(version="fault-transport-1", models=[fixture_model], allow_unverified=True)

    def interrupt(*_args, **_kwargs):
        raise InterruptedTransport()

    return ModelRunner(registry, transport=interrupt)


def _model_attempts(engine, run_id):
    if run_id is None:
        return []
    with Session(engine) as db:
        rows = db.scalars(
            select(ModelAttempt)
            .where(ModelAttempt.run_id == run_id)
            .order_by(ModelAttempt.cycle, ModelAttempt.started_at, ModelAttempt.ordinal)
        )
        return [
            {
                "cycle": row.cycle,
                "logical_id": row.logical_id,
                "ordinal": row.ordinal,
                "status": row.status,
                "code": row.error_code,
                "usage": row.usage,
                "elapsed_ms": round(
                    (utc(row.finished_at) - utc(row.started_at)).total_seconds() * 1000
                )
                if row.finished_at
                else None,
                "estimated_cost_usd": row.estimated_cost_usd,
                **row.metadata_json,
            }
            for row in rows
        ]


def _interrupted_audit(engine, namespace):
    with Session(engine) as db:
        run_ids = list(db.scalars(select(AgentRun.id).order_by(AgentRun.created_at, AgentRun.id)))
        events = (
            list(
                db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id.in_(run_ids))
                    .order_by(RunEvent.run_id, RunEvent.seq)
                )
            )
            if run_ids
            else []
        )
        trace = [
            {"run_id": row.run_id, "seq": row.seq, "kind": row.kind, "payload": row.payload}
            for row in events
        ]
        business_events = [
            {
                "case_id": row.case_id,
                "sequence": row.sequence,
                "type": row.type,
                "payload": row.payload,
            }
            for row in db.scalars(select(Event).order_by(Event.case_id, Event.sequence))
        ]
        ledger = [
            {
                "id": row.id,
                "case_id": row.case_id,
                "status": row.status,
                "action": row.action,
                "amount_cents": row.amount_cents,
                "lines": row.lines,
                "receipt": row.receipt,
            }
            for row in db.scalars(select(Execution).order_by(Execution.created_at, Execution.id))
        ]
    attempts = [
        {"run_id": run_id, **row} for run_id in run_ids for row in _model_attempts(engine, run_id)
    ]
    for attempt in attempts:
        if attempt["status"] == "STARTED":
            attempt["persisted_status"] = "STARTED"
            attempt["status"] = "UNKNOWN"
            attempt["code"] = "WORKER_INTERRUPTED"
    return {
        "observed_source": "interrupted_attempt_audit",
        "namespace": namespace,
        "model_attempts": attempts,
        "model_audit_complete": False,
        "business_audit": {"events": business_events, "ledger": ledger},
    }, trace


def _path(value, path):
    for segment in path.split("."):
        if isinstance(value, dict):
            value = value.get(segment, MISSING)
        elif isinstance(value, list) and segment.isascii() and segment.isdecimal():
            index = int(segment)
            value = value[index] if index < len(value) else MISSING
        else:
            return MISSING
    return value


def score(reference, events):
    checks = []
    for reference_check in reference.get("checks", []):
        value = _path(events.get(reference_check["event_id"]), reference_check["path"])
        checks.append(
            {
                "severity": (
                    "critical"
                    if reference_check["path"] == "detail.code"
                    and reference_check["equals"] in SAFETY_REJECTIONS
                    else "normal"
                ),
                **reference_check,
                "observed": None if value is MISSING else value,
                "status": (
                    "UNKNOWN"
                    if value is MISSING
                    else "PASS"
                    if type(value) is type(reference_check["equals"])
                    and value == reference_check["equals"]
                    else "FAIL"
                ),
            }
        )
    outputs = [
        value["output"]
        for value in events.values()
        if isinstance(value, dict) and isinstance(value.get("output"), dict)
    ]
    action = outputs[-1].get("action") if outputs else None
    if action is not None:
        checks.append(
            {
                "path": "draft.action",
                "observed": action,
                "status": "PASS"
                if action in reference.get("allowed_draft_actions", [])
                else "FAIL",
            }
        )
    limits = reference.get("refund_amount_cents")
    if action == "REFUND" and limits:
        lines = outputs[-1].get("lines") or []
        amounts = [row.get("amount_cents") for row in lines]
        amount = sum(amounts) if amounts and all(type(value) is int for value in amounts) else None
        checks.append(
            {
                "path": "draft.amount_cents",
                "observed": amount,
                "expected": limits,
                "status": "PASS"
                if amount is not None and limits["min"] <= amount <= limits["max"]
                else "FAIL",
            }
        )
    return checks


class EvaluationRunner:
    def __init__(self, engine, settings, checkpointer, *, model=None, storage=None, catalogue=None):
        self.engine, self.settings, self.checkpointer, self.model = (
            engine,
            settings,
            checkpointer,
            model,
        )
        self.storage = storage if storage is not None else S3ObjectStore(settings)
        self._injected_storage = storage is not None
        self._material_bytes = catalogue.material_bytes if catalogue is not None else material_bytes

    def recover_audit(self, result_id):
        if self.engine.dialect.name != "postgresql":
            return None, []
        namespace = _namespace(result_id)
        inspector = inspect(self.engine)
        if not all(
            inspector.has_table(name, schema=namespace)
            for name in (
                "agent_runs",
                "agent_run_events",
                "agent_model_attempts",
            )
        ):
            return None, []
        isolated = self.engine.execution_options(schema_translate_map={None: namespace})
        return _interrupted_audit(isolated, namespace)

    def _guard_storage(self):
        if (
            not self._injected_storage
            and self.settings.object_endpoint not in TEST_STORAGE_ENDPOINTS
        ):
            raise UnsupportedFixture("TEST_OBJECT_STORAGE_REQUIRED")

    def _model(self, config, quota):
        if config["source"] != "live":
            return None
        if self.model is None or not getattr(self.settings, "model_enabled", False):
            raise UnsupportedFixture("LIVE_MODEL_NOT_CONFIGURED")
        return self.model.with_evaluation_limits(
            calls=quota["calls"], budget=Decimal(quota["budget_usd"])
        )

    def run(
        self,
        result_id,
        case,
        reference,
        config,
        quota,
        *,
        cancelled,
        attempt_admission=None,
        attempt_continuation=None,
        queue_admission=None,
    ):
        if self.settings.profile != "test":
            raise UnsupportedFixture("EVALUATION_REQUIRES_TEST")
        self._guard_storage()
        started = monotonic()
        with _isolated_engine(self.engine, result_id) as (engine, namespace):
            fixture = case["fixture"]
            actors = _actor_set(fixture)
            _seed(engine, fixture, actors)
            clock = SnapshotClock(fixture["as_of"])
            settings = self.settings.model_copy(update={"model_enabled": False})
            payment = PaymentFault(engine)
            business = BusinessService(
                engine,
                settings,
                storage=NamespacedObjects(self.storage, namespace),
                payments=payment,
                clock=clock,
            )
            retrieval = config.get("retrieval_snapshot") or {}
            encoder = retrieval.get("encoder")
            if settings.embedding_enabled and not encoder:
                raise UnsupportedFixture("EVALUATION_EMBEDDING_SNAPSHOT_UNAVAILABLE")
            search = PolicySearch(
                engine,
                encode=bound_encoder(settings.embedding_model_path, encoder) if encoder else None,
                encoder=f"{MODEL_ID}@{encoder['sha256']}" if encoder else MODEL_ID,
                evaluation_catalogue=business.all_policy_versions,
            )
            catalogue = catalogue_sha256(business.all_policy_versions())
            expected_catalogue = quota.get("policy_catalogue_sha256")
            if expected_catalogue is not None and expected_catalogue != catalogue:
                raise UnsupportedFixture("EVALUATION_POLICY_SNAPSHOT_MISMATCH")
            agent = AgentService(
                engine,
                settings,
                business,
                self.checkpointer,
                dispatch=lambda _run_id: None,
                model=self._model(config, quota),
                attempt_admission=attempt_admission,
                attempt_continuation=attempt_continuation,
                queue_admission=queue_admission,
                intent_probe=blind_model_intent if config.get("probe_intent") else None,
                search=search,
            )
            setup_ms = round((monotonic() - started) * 1000)
            replay = ScriptReplay(
                case, config, actors, business, agent, payment, clock, self._material_bytes
            )
            if config["source"] in {"fixture", "load"}:
                replay.trace.append(
                    {"kind": "intent_probe", "payload": rule_intent(case["message"])}
                )
            stopped = replay.apply(cancelled)
            load_operations = (
                replay.load(cancelled) if not stopped and config["source"] == "load" else []
            )
            attempts = _model_attempts(engine, replay.run_id)
            if config["source"] == "live":
                observed_source = "live_provider" if attempts else "no_provider_call"
            elif config["source"] == "fault":
                observed_source = "fault_in_process"
            elif config["source"] == "load":
                observed_source = "application_load"
            else:
                observed_source = "application_offline"
            actual = {
                "observed_source": observed_source,
                "events": replay.observed,
                "model_attempts": attempts,
                "model_audit_complete": True,
                "retrieval_evidence": {
                    "schema_version": SCHEMA_VERSION,
                    "catalogue_sha256": catalogue,
                    "selector_sha256": (config.get("policy_snapshot") or {}).get("selector_sha256"),
                    "configured_mode": "HYBRID" if encoder else "BM25",
                    "encoder_manifest_sha256": encoder["sha256"] if encoder else None,
                    "index": {"state": search.index_state, **read_observed_index(engine)},
                },
                "elapsed_ms": round((monotonic() - started) * 1000),
                "namespace": namespace,
                "material_continuity": replay.material_continuity,
                "citation_audit": citation_observations(replay),
                "financial_safety": finance_observations(engine, replay),
                "fault_observed": replay.fault_observed or payment.triggered,
                "load_operations": load_operations,
                "load_planned": config.get("load_reads") if config["source"] == "load" else None,
                "timings": {
                    "setup_ms": setup_ms,
                    "active_ms": replay.active_ms,
                    "script_input_wait_ms": replay.script_input_wait_ms,
                    "model_ms": sum(row["elapsed_ms"] for row in attempts)
                    if all(row["elapsed_ms"] is not None for row in attempts)
                    else None,
                },
            }
            if replay.case_id:
                actual["final_business"] = business.detail(replay.case_id, actors["support"])
            checks = score(reference, replay.observed)
            if actual["financial_safety"]["status"] == "FAIL":
                checks.append(
                    {
                        "path": "financial_safety",
                        "status": "FAIL",
                        "severity": "critical",
                        "observed": actual["financial_safety"]["violations"],
                    }
                )
            if stopped:
                status, reason = ("TIMEOUT" if stopped in TIMEOUT_CODES else "ERROR"), stopped
            elif any(check["status"] == "FAIL" for check in checks):
                status, reason = "FAILED", "DETERMINISTIC_CHECK_FAILED"
            elif config["source"] == "live" and not attempts:
                status, reason = "NEEDS_REVIEW", "NO_PROVIDER_CALL"
            elif not checks or any(check["status"] == "UNKNOWN" for check in checks):
                status, reason = "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE"
            else:
                status, reason = "NEEDS_REVIEW", "HUMAN_REFERENCE_PENDING"
            trace = replay.trace
            if replay.run_id and replay.case_id:
                cursor = 0
                while batch := agent.events(
                    replay.case_id, replay.run_id, actors["support"], cursor
                ):
                    trace.extend(batch)
                    cursor = batch[-1]["seq"]
            actual["draft_safety"] = draft_observations(replay, trace, attempts)
            return {
                "status": status,
                "failure_reason": reason,
                "actual": actual,
                "trace": trace,
                "checks": checks,
            }


class ScriptReplay:
    def __init__(
        self, case, config, actors, business, agent, payment, clock, read_material=material_bytes
    ):
        self.case, self.config, self.actors = case, config, actors
        self.business, self.agent, self.payment, self.clock = business, agent, payment, clock
        self.read_material = read_material
        self.case_id = self.run_id = self.proposal_id = self.execution_id = None
        self.observed = {}
        self.requests = {}
        self.trace = []
        self.crash = False
        self.cycle_context = {}
        self.material_continuity = []
        self.active_ms = 0
        self.script_input_wait_ms = 0
        self.fault_observed = False

    def apply(self, cancelled):
        waiting = False
        for step in self.case["events"]:
            started = monotonic()
            if cancelled():
                return "EXPERIMENT_CANCELLED"
            try:
                output = self._apply(step)
            except (BusinessError, RunError) as error:
                output = {
                    "detail": {"code": error.code, "message": error.message},
                    "http_status": error.status,
                }
            except UnsupportedFixture as error:
                self.trace.append({"event_id": step["id"], "op": step["op"], "error": error.code})
                return error.code
            elapsed = round((monotonic() - started) * 1000)
            if waiting and step["op"] in {"upload", "review"}:
                self.script_input_wait_ms += elapsed
            else:
                self.active_ms += elapsed
            if step["op"] in {"start", "resume"}:
                waiting = output.get("status") == "WAITING_INPUT"
            self.observed[step["id"]] = output
            self.trace.append(
                {
                    "event_id": step["id"],
                    "op": step["op"],
                    "actor": step["actor"],
                    "elapsed_ms": elapsed,
                    "status": output.get("status"),
                    "error": output.get("detail", {}).get("code"),
                }
            )
            if output.get("reason") in STOP_CODES:
                return output["reason"]
        return None

    def _case_version(self, user):
        return self.business.context(self.case_id, user)["case"]["version"]

    def _process(self, actor):
        prior = self.agent.get(self.case_id, self.run_id, actor)
        before = self.business.context(self.case_id, actor)
        self.cycle_context[prior["cycle"]] = before
        try:
            self.agent.process(self.run_id)
        except InterruptedTransport:
            if not self.crash:
                raise
            self.crash = False
            self.fault_observed = True
            self.trace.append({"event": "INJECTED_MODEL_TRANSPORT_INTERRUPTION"})
            self.agent.process(self.run_id)
        after = self.business.context(self.case_id, actor)
        prior_ids = {item["id"] for item in before["materials"]}
        if prior_ids:
            self.material_continuity.append(
                prior_ids <= {item["id"] for item in after["materials"]}
            )
        return self.agent.get(self.case_id, self.run_id, actor)

    def load(self, cancelled):
        if self.case_id is None:
            return []
        actor = self.actors["support"]
        baseline = self.business.context(self.case_id, actor)["versions"]
        samples = []
        for _ in range(self.config["load_reads"]):
            if cancelled():
                break
            start = monotonic()
            try:
                snapshot = self.business.context(self.case_id, actor)
                events = self.business.events(self.case_id, actor, 0)
                ok = snapshot["versions"] == baseline and len(events) == snapshot["case"]["version"]
            except BusinessError:
                ok = False
            samples.append(
                {
                    "operation": "case_and_event_read",
                    "ok": ok,
                    "elapsed_ms": round((monotonic() - start) * 1000),
                }
            )
        return samples

    def _fault(self, args):
        if self.config["source"] != "fault":
            raise UnsupportedFixture("FAULT_SOURCE_REQUIRED")
        target, fault = args["target"], args["fault"]
        if target == "clock" and fault == "expire_approval":
            self.clock.advance()
            self.fault_observed = True
        elif target == "payment" and fault in {"timeout_no_receipt", "commit_then_disconnect"}:
            self.payment.fault = fault
        elif target == "agent" and fault == "crash_after_attempt_started":
            self.agent.model = _crash_model(self.config)
            self.crash = True
        else:
            raise UnsupportedFixture("FAULT_HOOK_UNAVAILABLE")
        return {"status": "FAULT_ARMED", "fault": fault}

    def _parallel(self, key, args):
        if self.config["source"] != "fault" or self.business.engine.dialect.name != "postgresql":
            raise UnsupportedFixture("PG_CONCURRENCY_REQUIRED")
        if args["parallelism"] != 2:
            raise UnsupportedFixture("FIXTURE_PARALLELISM_INVALID")
        actor = self.actors["support"]
        version = self._case_version(actor)
        barrier = Barrier(2, timeout=5)

        def execute(index):
            barrier.wait()
            return self.business.execute(
                self.case_id,
                actor,
                proposal_id=self.proposal_id,
                expected_version=version,
                idempotency_key=f"{key}-{index}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, range(2)))
        ledger = self.business.context(self.case_id, actor)["ledger"]
        self.fault_observed = True
        return {
            "distinct_execution_ids": len({row["id"] for row in results}),
            "ledger_rows": len(ledger),
            "results": results,
        }

    def _apply(self, step):
        op, key, args = step["op"], step["id"], dict(step["args"])
        if op == "fault":
            return self._fault(args)
        if op == "parallel_execute":
            return self._parallel(key, args)
        if op == "repeat_event":
            original = args["event_id"]
            before = self.observed.get(original, {})
            request = self.requests.get(original)
            if request is None:
                raise UnsupportedFixture("FIXTURE_REPEAT_TARGET_INVALID")
            after = self.business.execute(**request)
            return {"same_execution": before.get("id") == after.get("id"), "result": after}

        actor = self.actors[step["actor"]]
        if op == "create_case":
            order = self.business.order(args["order_id"], actor)
            result = self.business.create_case(
                actor, expected_version=order["version"], idempotency_key=key, **args
            )
            self.case_id = result["id"]
            return result
        if self.case_id is None:
            raise UnsupportedFixture("FIXTURE_CASE_NOT_CREATED")
        if op == "read":
            return self.business.detail(self.case_id, actor)

        mutation = {
            "expected_version": self._case_version(actor),
            "idempotency_key": key,
        }
        if op == "start":
            if self.run_id is not None:
                raise UnsupportedFixture("MULTIPLE_MODEL_RUNS_PER_ATTEMPT")
            result = self.agent.start(
                self.case_id,
                actor,
                **mutation,
                mode=self.config["mode"],
                strategy=self.config["strategy"],
                message=self.case["message"],
            )
            self.run_id = result["id"]
            return self._process(actor)
        if op == "resume":
            self.agent.transition(self.case_id, self.run_id, actor, operation="resume", **mutation)
            return self._process(actor)
        if op == "upload":
            asset = args.pop("asset_id")
            data = self.read_material(self.case["id"], asset)
            return self.business.upload(self.case_id, actor, content=data, **mutation, **args)
        if op == "review":
            return self.business.review_materials(self.case_id, actor, **mutation, **args)
        if op == "facts":
            return self.business.record_facts(self.case_id, actor, **mutation, **args)
        if op == "propose":
            result = self.business.propose(self.case_id, actor, **mutation, **args)
            self.proposal_id = result["id"]
            return result
        if op == "approve":
            return self.business.approve(
                self.case_id,
                actor,
                proposal_id=self.proposal_id,
                **mutation,
                **args,
            )
        if op == "execute":
            request = {
                "case_id": self.case_id,
                "user": actor,
                "proposal_id": self.proposal_id,
                **mutation,
                **args,
            }
            self.requests[key] = request
            result = self.business.execute(**request)
            self.execution_id = result["id"]
            return result
        if op == "reconcile":
            return self.business.reconcile(
                self.case_id, self.execution_id, actor, **mutation, **args
            )
        raise UnsupportedFixture("FIXTURE_OPERATION_UNAVAILABLE")
