"""Internal execution of one verified private campaign, outside ordinary dispatch."""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID, uuid5

from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest, now
from aftercare.config import TARGETS
from aftercare.db import User
from aftercare.intelligence.models import (
    TRANSPORT_EVIDENCE_VERSION,
    ModelRunner,
    _stream_transport,
    _transport,
)
from aftercare.intelligence.tables import AgentRun, ModelAttempt

from ..progress import append_progress
from ..retrieval_evidence import catalogue_sha256, fixture_policies, policy_snapshot
from ..runner import EvaluationRunner, _namespace
from ..service import (
    METRICS_VERSION,
    SCORER_VERSION,
    EvaluationService,
    _authorized,
    _counts,
    _dependency_lock_sha256,
    _embedding_snapshot,
    _live_model_snapshot,
    _result_json,
    require_current_code,
)
from ..supplemental_usage import _identity
from ..tables import EvaluationResult, Experiment
from .contracts import ConditionControls, FormalFreezeDraftV2, FormalFreezeDraftV3, FreezeQuota
from .files import json_bytes
from .native import project_runtime_controls
from .package import PrivatePackage

NAMESPACE = UUID("0ba222a2-b730-4578-ab70-086d15866394")
ONE_DOLLAR = Decimal(1_000_000)
_FORMAL_METADATA = {
    "formal_controls_sha256",
    "formal_manifest_sha256",
    "formal_raw_reference_sha256",
    "formal_cases_sha256",
    "formal_row_quota",
    "formal_schedule",
}


def _project_controls(config):
    return project_runtime_controls(
        {key: value for key, value in config.items() if key not in _FORMAL_METADATA}
    )


def _experiment_id(freeze_sha256, key):
    return str(uuid5(NAMESPACE, f"{freeze_sha256}/{key}"))


def _ordered_results(db, experiment_id):
    return sorted(
        EvaluationService._results(db, experiment_id), key=lambda row: (row.repetition, row.case_id)
    )


def _hex_digest(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def fail(code, message, status=409):
    raise BusinessError(code, message, status)


def _binding_error():
    fail("FORMAL_EXECUTION_BINDING_MISMATCH", "正式评测的冻结绑定与当前执行输入不一致。")


@contextmanager
def _campaign_lease(engine, freeze_sha256):
    if engine.dialect.name != "postgresql":
        yield lambda: True
        return
    key = int.from_bytes(
        sha256(f"eval:formal:{freeze_sha256}".encode()).digest()[:8], "big", signed=True
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            acquired = conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        except SQLAlchemyError:
            conn.invalidate()
            raise
        if not acquired:
            yield None
            return

        try:
            pid = conn.scalar(text("SELECT pg_backend_pid()"))
            lost = False

            def held():
                nonlocal lost
                if lost or conn.invalidated:
                    return False
                try:
                    lost = not conn.scalar(
                        text(
                            "SELECT pg_backend_pid() = :pid AND EXISTS ("
                            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                            "AND pid = :pid AND classid::bigint = :high "
                            "AND objid::bigint = :low AND objsubid = 1)"
                        ),
                        {"pid": pid, "high": (key >> 32) & 0xFFFFFFFF, "low": key & 0xFFFFFFFF},
                    )
                except SQLAlchemyError:
                    lost = True
                    conn.invalidate()
                return not lost

            yield held
        finally:
            if not conn.invalidated:
                try:
                    if not conn.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key}):
                        conn.invalidate()
                except SQLAlchemyError:
                    conn.invalidate()


@dataclass(frozen=True)
class _Actor:
    user_id: str
    token_hash: str

    def matches(self, row):
        return bool(
            row
            and row.enabled
            and row.role == "tester"
            and compare_digest(row.token_hash, self.token_hash)
        )


@dataclass(frozen=True)
class _Block:
    key: str
    id: str
    config: dict
    controls: ConditionControls


@dataclass(frozen=True)
class _Plan:
    freeze: str
    blocks: tuple[_Block, ...]
    cases: dict
    references: dict
    quotas: dict
    repetitions: int


def _attempt_cost(attempt):
    value = attempt["estimated_cost_usd"]
    if value is None or not isinstance(attempt["usage"], dict):
        return None
    try:
        cost = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return cost if cost.is_finite() and cost >= 0 else None


@dataclass(frozen=True)
class _Accounting:
    calls: int
    cost: Decimal
    unknown: bool
    over_quota: bool = False

    @property
    def reason(self):
        if self.unknown:
            return "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
        if self.over_quota:
            return "FORMAL_ROW_QUOTA_EXCEEDED"
        return None


def _audit(attempts, quota, *, incomplete=False):
    costs = [_attempt_cost(attempt) for attempt in attempts]
    known = sum((cost for cost in costs if cost is not None), Decimal(0))
    return _Accounting(
        len(attempts),
        known,
        incomplete
        or any(cost is None for cost in costs)
        or any(attempt["status"] not in {"SUCCEEDED", "FAILED"} for attempt in attempts),
        len(attempts) > quota["calls"] or known > Decimal(quota["budget_usd"]),
    )


def _recorded(row):
    if row.started_at is None:
        return _Accounting(0, Decimal(0), row.status != "QUEUED")
    actual = row.actual or {}
    return _audit(
        actual.get("model_attempts", []),
        row.quota,
        incomplete=actual.get("model_audit_complete") is not True,
    )


class FormalEvaluationService:
    """Only the private CLI passes an authorized, verified register handoff here."""

    def __init__(self, engine, settings, checkpointer, *, model):
        self.engine, self.settings, self.checkpointer, self.model = (
            engine,
            settings,
            checkpointer,
            model,
        )

    def _guard_target(self):
        if self.settings.profile != "test" or self.engine.dialect.name != "postgresql":
            fail("FORMAL_EXECUTION_TEST_REQUIRED", "正式评测仅限独立测试 PostgreSQL。", 403)
        _, database, username, cluster = TARGETS["test"]
        with self.engine.connect() as conn:
            found = conn.execute(
                text("SELECT current_database(), current_user, current_setting('cluster_name')")
            ).one()
        if tuple(found) != (database, username, cluster):
            fail("FORMAL_EXECUTION_TEST_REQUIRED", "正式评测仅限独立测试 PostgreSQL。", 403)

    def _runner(self, package):
        return EvaluationRunner(
            self.engine, self.settings, self.checkpointer, model=self.model, catalogue=package
        )

    def _model_runtime(self):
        if (
            not self.settings.model_enabled
            or type(self.model) is not ModelRunner
            or self.model.transport is not _transport
            or self.model.stream_transport is not _stream_transport
        ):
            fail("FORMAL_EXECUTION_TRANSPORT_REQUIRED", "正式评测要求当前生产模型传输配置。", 422)
        model = _live_model_snapshot(self.model)
        if self.model.base_registry_digest != model["registry_digest"]:
            _binding_error()
        return {
            "code_digest": require_current_code(),
            "dependency_lock_sha256": _dependency_lock_sha256(),
            "model_snapshot": model,
        }

    def _runtime(self, policies):
        return {
            **self._model_runtime(),
            "scorer_version": SCORER_VERSION,
            "metrics_version": METRICS_VERSION,
            "policy_snapshot": policy_snapshot(policies),
            **_embedding_snapshot(self.settings),
        }

    def _plan(self, freeze, draft, controls, package, execution_cap):
        if (
            not _hex_digest(freeze)
            or not isinstance(draft, FormalFreezeDraftV2)
            or not isinstance(package, PrivatePackage)
            or controls.keys() != draft.conditions.keys()
            or not isinstance(execution_cap, FreezeQuota)
        ):
            _binding_error()
        if (
            draft.campaign_quota.calls > execution_cap.calls
            or draft.campaign_quota.budget_microusd > execution_cap.budget_microusd
        ):
            fail("FORMAL_BUDGET_CAP_EXCEEDED", "冻结批次超过当前正式执行封顶。", 422)
        cases = {case["id"]: case for case in package.load_cases("holdout")}
        references = package.load_references()
        case_hashes = {key: digest(case) for key, case in cases.items()}
        policies = {
            key: catalogue_sha256(fixture_policies(case["fixture"])) for key, case in cases.items()
        }
        reference_sha = digest({key: digest(ref) for key, ref in references.items()})
        binding = package.binding
        if (
            len(cases) != draft.case_count
            or references.keys() != cases.keys()
            or binding.manifest_sha256 != draft.manifest_sha256
            or binding.cases_sha256 != package.manifest.cases_sha256
            or binding.references_sha256 != package.manifest.references_sha256
            or binding.gold_approvals_sha256 != package.manifest.gold_approvals_sha256
            or digest(policies) != binding.policy_catalogues_sha256
            or binding.policy_catalogues_sha256 != package.manifest.policy_catalogues_sha256
            or reference_sha != binding.effective_reference_sha256
        ):
            _binding_error()
        quotas = {
            key: {
                "calls": draft.row_quota.calls,
                "budget_usd": str(Decimal(draft.row_quota.budget_microusd) / ONE_DOLLAR),
                "case_hash": case_hashes[key],
                "policy_catalogue_sha256": policies[key],
            }
            for key in cases
        }
        runtime = self._runtime(policies)
        blocks = []
        for key in draft.block_order:
            selected, locked = draft.conditions[key], controls[key]
            mode, strategy = key.split("/")
            if (
                selected.validation_experiment_id != locked.validation_experiment_id
                or selected.controls_sha256 != locked.controls_sha256
                or sha256(json_bytes(locked.controls.model_dump(mode="json"))).hexdigest()
                != locked.controls_sha256
            ):
                _binding_error()
            config = {
                **runtime,
                "source": "live",
                "mode": mode,
                "strategy": strategy,
                "probe_intent": locked.controls.probe_intent,
                "dataset_id": package.manifest.dataset_id,
                "split": "holdout",
                "case_ids": sorted(cases),
                "repetitions": draft.repetitions,
                "concurrency": 1,
                "input_digest": digest(case_hashes),
                "reference_digest": reference_sha,
                "max_calls": selected.quota.calls,
                "budget_usd": float(Decimal(selected.quota.budget_microusd) / ONE_DOLLAR),
                "gate_policy": draft.gate_policy.model_dump(mode="json"),
                "execution_driver": "formal",
                "formal_controls_sha256": locked.controls_sha256,
                "formal_manifest_sha256": binding.manifest_sha256,
                "formal_raw_reference_sha256": binding.references_sha256,
                "formal_cases_sha256": binding.cases_sha256,
                "formal_row_quota": draft.row_quota.model_dump(),
            }
            if isinstance(draft, FormalFreezeDraftV3):
                config["formal_schedule"] = {
                    "version": draft.schema_version,
                    "order": draft.schedule,
                    "conditions": list(draft.block_order),
                }
            if _project_controls(config) != locked.controls:
                fail("FORMAL_RUNTIME_CHANGED", "当前运行控制与冻结验证配置不一致。", 409)
            blocks.append(_Block(key, _experiment_id(freeze, key), config, locked))
        return _Plan(freeze, tuple(blocks), cases, references, quotas, draft.repetitions)

    def _read_recovery(self, connection, result_id):
        if connection.dialect.name != "postgresql":
            return None
        schema = _namespace(result_id)
        inspection = inspect(connection)
        if not all(
            inspection.has_table(model.__tablename__, schema=schema)
            for model in (AgentRun, ModelAttempt)
        ):
            return None
        options = {"schema_translate_map": {None: schema}}
        runs = (
            connection.execute(
                select(AgentRun.id, AgentRun.cycle, AgentRun.mode, AgentRun.strategy),
                execution_options=options,
            )
            .mappings()
            .all()
        )
        fields = (
            "run_id",
            "cycle",
            "logical_id",
            "ordinal",
            "status",
            "usage",
            "estimated_cost_usd",
            "metadata_json",
            "finished_at",
        )
        attempts = (
            connection.execute(
                select(*(getattr(ModelAttempt, key) for key in fields)).order_by(
                    ModelAttempt.run_id,
                    ModelAttempt.cycle,
                    ModelAttempt.logical_id,
                    ModelAttempt.ordinal,
                ),
                execution_options=options,
            )
            .mappings()
            .all()
        )
        return runs, attempts

    def _recovery_accounting(self, connection, row, config):
        # This supplemental read may authorize later rows, never rewrite the interrupted result.
        if row.status != "ERROR" or row.failure_reason != "WORKER_INTERRUPTED":
            return None
        frozen = (row.actual or {}).get("model_attempts")
        evidence = self._read_recovery(connection, row.id) if isinstance(frozen, list) else None
        if evidence is None:
            return None
        runs, attempts = evidence
        frozen_ids = {_identity(item) for item in frozen}
        raw_ids = {_identity(dict(item)) for item in attempts}
        if (
            len(runs) > 1
            or (attempts and not runs)
            or any(
                (run["mode"], run["strategy"]) != (config["mode"], config["strategy"])
                for run in runs
            )
            or None in frozen_ids
            or len(frozen_ids) != len(frozen)
            or None in raw_ids
            or len(raw_ids) != len(attempts)
            or raw_ids != frozen_ids
            or _audit(attempts, row.quota).reason is not None
        ):
            return None
        model = config["model_snapshot"]
        for item in attempts:
            metadata = item["metadata_json"]
            marker = metadata.get("transport_evidence") or {}
            if (
                item["run_id"] != runs[0]["id"]
                or item["cycle"] > runs[0]["cycle"]
                or item["finished_at"] is None
                or metadata.get("run_id") != item["run_id"]
                or metadata.get("base_registry_digest") != model["registry_digest"]
                or not any(
                    all(
                        spec.get(key) == metadata.get(key) for key in ("alias", "provider", "model")
                    )
                    for spec in model["models"]
                )
                or marker.get("version") != TRANSPORT_EVIDENCE_VERSION
                or marker.get("path") not in {"DEFAULT_SYNC", "DEFAULT_STREAM"}
                or marker.get("response") not in {"SDK_RETURNED", "HTTP_ERROR_RESPONSE"}
                or not all(
                    type(item["usage"].get(key)) is int and item["usage"][key] >= 0
                    for key in ("input_tokens", "output_tokens")
                )
            ):
                return None
        return {
            "result_id": row.id,
            "status": "SETTLED_FOR_LATER_ROWS",
            "model_calls": len(attempts),
            "known_cost_usd": str(_audit(attempts, row.quota).cost),
            "attempts": [
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"metadata_json", "finished_at"}
                    },
                    "finished_at": item["finished_at"].isoformat(),
                }
                for item in attempts
            ],
        }

    def _actor(self, user, credential_token_hash):
        _authorized(user)
        if not _hex_digest(credential_token_hash):
            fail("FORMAL_EXECUTION_ACTOR_REVOKED", "需要有效的当前测试员凭据。", 403)
        return _Actor(user.user_id, credential_token_hash)

    @staticmethod
    def _check_actor(db, actor, *, lock=False):
        statement = select(User).where(User.id == actor.user_id)
        if lock:
            statement = statement.with_for_update(read=True)
        if not actor.matches(db.scalar(statement)):
            fail("FORMAL_EXECUTION_ACTOR_REVOKED", "需要有效的当前测试员凭据。", 403)

    def _prepare(self, plan, actor):
        with Session(self.engine) as db, db.begin():
            if self.engine.dialect.name == "postgresql":
                key = int.from_bytes(
                    sha256(f"eval:formal:create:{plan.freeze}".encode()).digest()[:8],
                    "big",
                    signed=True,
                )
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            self._check_actor(db, actor, lock=True)
            for block in plan.blocks:
                row = db.get(Experiment, block.id, with_for_update=True)
                if row is not None:
                    _require_binding(row, plan.freeze, block.key)
                    if row.config != block.config:
                        _binding_error()
                    planned = [
                        (case_id, repeat, plan.references[case_id], plan.quotas[case_id])
                        for repeat in range(1, plan.repetitions + 1)
                        for case_id in sorted(plan.cases)
                    ]
                    stored = [
                        (result.case_id, result.repetition, result.expected, result.quota)
                        for result in _ordered_results(db, row.id)
                    ]
                    if stored != planned:
                        _binding_error()
                    continue
                stamp = now()
                row = Experiment(
                    id=block.id,
                    name=f"正式留出评测 {block.key}",
                    status="QUEUED",
                    version=1,
                    progress_seq=0,
                    created_by=actor.user_id,
                    config=block.config,
                    formal_freeze_sha256=plan.freeze,
                    formal_condition_key=block.key,
                    created_at=stamp,
                    updated_at=stamp,
                )
                EvaluationService._store_plan(
                    db,
                    row,
                    (
                        (key, repeat, plan.references[key], plan.quotas[key])
                        for repeat in range(1, plan.repetitions + 1)
                        for key in sorted(plan.cases)
                    ),
                )

    def run(
        self, user, *, freeze_sha256, draft, controls, package, credential_token_hash, execution_cap
    ):
        self._guard_target()
        actor = self._actor(user, credential_token_hash)
        with Session(self.engine) as db:
            self._check_actor(db, actor)
        if not _hex_digest(freeze_sha256):
            _binding_error()
        with _campaign_lease(self.engine, freeze_sha256) as held:
            if held is None:
                fail("FORMAL_EXECUTION_BUSY", "该正式评测批次正在执行。", 409)
            plan = self._plan(freeze_sha256, draft, controls, package, execution_cap)
            if not held():
                fail("FORMAL_EXECUTION_LEASE_LOST", "正式评测执行租约已失效。", 409)
            self._prepare(plan, actor)
            runner = self._runner(package)
            if isinstance(draft, FormalFreezeDraftV3):
                reason = self._interleaved(plan, actor, held, runner)
                return self._summary(plan, draft, reason)
            reason = None
            for block in plan.blocks:
                step = _ExecutionBlock(self, plan, block, actor, held)
                reason = step.execute(runner)
                if reason is not None:
                    break
            return self._summary(plan, draft, reason)

    def _interleaved(self, plan, actor, held, runner):
        steps = [_ExecutionBlock(self, plan, block, actor, held) for block in plan.blocks]
        # Recover and audit ALL conditions before claiming any new row. A later
        # condition's interrupted call must also fence earlier conditions.
        for step in steps:
            if reason := step.execute(runner, audit_only=True):
                return reason
        for repetition in range(1, plan.repetitions + 1):
            for case_id in sorted(plan.cases):
                for step in steps:
                    if reason := step.execute(runner, target=(repetition, case_id)):
                        return reason
        return None

    def _summary(self, plan, draft, reason):
        with Session(self.engine) as db:
            blocks = []
            for block in plan.blocks:
                experiment = db.get(Experiment, block.id)
                _require_binding(experiment, plan.freeze, block.key)
                results = _ordered_results(db, block.id)
                observed = [_recorded(row) for row in results]
                reserved = sum(
                    (
                        max(Decimal(row.quota["budget_usd"]) - item.cost, Decimal(0))
                        for row, item in zip(results, observed, strict=True)
                        if item.unknown
                    ),
                    Decimal(0),
                )
                unknown_cases = sum(item.unknown for item in observed)
                blocks.append(
                    {
                        "condition_key": block.key,
                        "formal_condition_key": block.key,
                        "experiment_id": block.id,
                        "status": experiment.status,
                        "counts": _counts([_result_json(row, []) for row in results]),
                        "model_calls": sum(item.calls for item in observed),
                        "accounting_complete": unknown_cases == 0,
                        "unknown_case_count": unknown_cases,
                        "quota_breach_case_count": sum(item.over_quota for item in observed),
                        "known_cost_usd": str(sum((item.cost for item in observed), Decimal(0))),
                        "reserved_budget_microusd": int(
                            (reserved * ONE_DOLLAR).to_integral_value(rounding=ROUND_CEILING)
                        ),
                    }
                )
            return {
                "formal": True,
                "formal_freeze_sha256": plan.freeze,
                "freeze_sha256": plan.freeze,
                "status": "INCOMPLETE" if reason else "COMPLETED",
                "reason": reason,
                "campaign_quota": draft.campaign_quota.model_dump(),
                "conditions": blocks,
            }

    def private_report(self, user, *, freeze_sha256, draft, credential_token_hash):
        self._guard_target()
        actor = self._actor(user, credential_token_hash)
        if not _hex_digest(freeze_sha256) or not isinstance(draft, FormalFreezeDraftV2):
            _binding_error()
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execution_options(isolation_level="REPEATABLE READ")
            with Session(connection) as db, db.begin():
                if self.engine.dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                return self._private_report(db, actor, freeze_sha256, draft)

    def _private_report(self, db, actor, freeze_sha256, draft):
        self._check_actor(db, actor)
        service = EvaluationService(self.engine, self.settings)
        reports = []
        for key in draft.block_order:
            row = db.get(Experiment, _experiment_id(freeze_sha256, key))
            _require_binding(row, freeze_sha256, key)
            if row.config.get("formal_controls_sha256") != draft.conditions[key].controls_sha256:
                _binding_error()
            supplemental = [
                evidence
                for result in _ordered_results(db, row.id)
                if (evidence := self._recovery_accounting(db.connection(), result, row.config))
                is not None
            ]
            reports.append(
                {
                    **service._detail(db, row),
                    "recovery_accounting": supplemental,
                    "formal_freeze_sha256": freeze_sha256,
                    "formal_condition_key": key,
                }
            )
        return {"formal": True, "formal_freeze_sha256": freeze_sha256, "conditions": reports}


def _require_binding(row, freeze, condition):
    if (
        row is None
        or row.formal_freeze_sha256 != freeze
        or row.formal_condition_key != condition
        or row.config.get("execution_driver") != "formal"
    ):
        _binding_error()


class _ExecutionBlock(EvaluationService):
    """Bound service used only under the campaign lease."""

    def __init__(self, owner, plan, block, actor, held):
        super().__init__(
            owner.engine, owner.settings, model=owner.model, attempt_admission=lambda _: held()
        )
        self.owner, self.plan, self.block, self.actor, self.held = owner, plan, block, actor, held
        self.stop_reason = None
        self._active_quota = None
        self._runtime_result = None

    def _find(self, db, experiment_id, *, lock=False):
        if experiment_id != self.block.id:
            _binding_error()
        row = db.get(Experiment, experiment_id, with_for_update=lock)
        _require_binding(row, self.plan.freeze, self.block.key)
        if row.config != self.block.config:
            _binding_error()
        return row

    def _owns_job(self, _session):
        return self.held()

    def _runtime_reason(self):
        try:
            runtime = self.owner._runtime(
                {key: quota["policy_catalogue_sha256"] for key, quota in self.plan.quotas.items()}
            )
        except (BusinessError, OSError, ValueError):
            return "FORMAL_RUNTIME_CHANGED"
        if _project_controls({**self.block.config, **runtime}) != self.block.controls.controls:
            return "FORMAL_RUNTIME_CHANGED"
        return None

    def _cancelled(self, experiment_id):
        if not self.held():
            self.stop_reason = "FORMAL_EXECUTION_LEASE_LOST"
        if self.stop_reason is not None:
            return True
        with Session(self.engine) as db:
            experiment = self._find(db, experiment_id)
            if experiment.status != "RUNNING":
                self.stop_reason = "FORMAL_EXPERIMENT_STOPPED"
            elif not self.actor.matches(db.get(User, self.actor.user_id)):
                self.stop_reason = "FORMAL_EXECUTION_ACTOR_REVOKED"
        return self.stop_reason is not None

    def _active_attempt(self, db, experiment_id, result_id):
        if self.engine.dialect.name != "postgresql":
            # SQLite is usable only through an internal test subclass.
            with Session(self.engine) as parent:
                experiment = self._find(parent, experiment_id)
                result = parent.get(EvaluationResult, result_id)
                actor = parent.get(User, self.actor.user_id)
                return self._active_records(experiment, result, actor)
        active_schema = (
            db.get_bind().get_execution_options().get("schema_translate_map", {}).get(None)
        )
        if active_schema != _namespace(result_id):
            return False
        schema = (
            self.engine.get_execution_options().get("schema_translate_map", {}).get(None)
            or "public"
        )
        metadata = MetaData()
        experiment, result, user = (
            model.__table__.to_metadata(metadata, schema=schema)
            for model in (Experiment, EvaluationResult, User)
        )
        row = db.execute(
            select(
                experiment.c.status,
                experiment.c.config,
                result.c.quota,
                result.c.expected,
                result.c.case_id,
                user.c.enabled,
                user.c.role,
                user.c.token_hash,
            )
            .join(result, result.c.experiment_id == experiment.c.id)
            .join(user, user.c.id == self.actor.user_id)
            .where(
                experiment.c.id == experiment_id,
                experiment.c.formal_freeze_sha256 == self.plan.freeze,
                experiment.c.formal_condition_key == self.block.key,
                result.c.id == result_id,
                result.c.status == "RUNNING",
            )
            .with_for_update(read=True, of=(experiment, result, user))
        ).one_or_none()
        if row is None:
            return False
        if not self.actor.matches(row):
            self.stop_reason = "FORMAL_EXECUTION_ACTOR_REVOKED"
            return False
        return (
            row.status == "RUNNING"
            and row.config == self.block.config
            and row.quota == self.plan.quotas.get(row.case_id)
            and row.expected == self.plan.references.get(row.case_id)
        )

    def _active_records(self, experiment, result, actor):
        if not self.actor.matches(actor):
            self.stop_reason = "FORMAL_EXECUTION_ACTOR_REVOKED"
            return False
        return bool(
            result
            and result.experiment_id == experiment.id
            and experiment.status == result.status == "RUNNING"
            and result.quota == self.plan.quotas.get(result.case_id)
            and result.expected == self.plan.references.get(result.case_id)
        )

    def _continuation(self, session, experiment_id, result_id):
        if self.stop_reason is not None:
            return False
        if not self.held():
            self.stop_reason = "FORMAL_EXECUTION_LEASE_LOST"
        elif not self._active_attempt(session, experiment_id, result_id):
            self.stop_reason = self.stop_reason or "FORMAL_EXECUTION_BINDING_MISMATCH"
        return self.stop_reason is None

    def _admit_result_attempt(self, session, experiment_id, result_id):
        if not self._continuation(session, experiment_id, result_id):
            return False
        if self._runtime_result != result_id:
            self.stop_reason = self._runtime_reason()
            if self.stop_reason is None:
                self._runtime_result = result_id
        else:
            try:
                runtime = self.owner._model_runtime()
            except (BusinessError, OSError, ValueError):
                self.stop_reason = "FORMAL_RUNTIME_CHANGED"
            else:
                if any(self.block.config[key] != value for key, value in runtime.items()):
                    self.stop_reason = "FORMAL_RUNTIME_CHANGED"
        if self.stop_reason is None:
            attempts = (
                session.execute(
                    select(ModelAttempt.estimated_cost_usd, ModelAttempt.status, ModelAttempt.usage)
                )
                .mappings()
                .all()
            )
            self.stop_reason = _audit(attempts, self._active_quota).reason
        if self.stop_reason is None and not self.held():
            self.stop_reason = "FORMAL_EXECUTION_LEASE_LOST"
        return self.stop_reason is None

    def execute(self, runner, *, target=None, audit_only=False):
        if not self.held():
            return "FORMAL_EXECUTION_LEASE_LOST"
        if reason := self._runtime_reason():
            return reason
        with Session(self.engine) as db, db.begin():
            try:
                self.owner._check_actor(db, self.actor, lock=True)
            except BusinessError as error:
                return error.code
            experiment = self._find(db, self.block.id, lock=True)
            if experiment.status not in {"QUEUED", "RUNNING", "COMPLETED"}:
                return "FORMAL_EXPERIMENT_STOPPED"
            if experiment.status != "COMPLETED":
                self._start_processing(db, experiment, runner)

        while self.stop_reason is None:
            if not self.held():
                return "FORMAL_EXECUTION_LEASE_LOST"
            with Session(self.engine) as db, db.begin():
                try:
                    self.owner._check_actor(db, self.actor, lock=True)
                except BusinessError as error:
                    return error.code
                experiment = self._find(db, self.block.id, lock=True)
                rows = _ordered_results(db, experiment.id)
                for row in rows:
                    reason = _recorded(row).reason
                    if (
                        reason
                        and self.owner._recovery_accounting(db.connection(), row, self.block.config)
                        is None
                    ):
                        return reason
                if audit_only:
                    return None
                pending = next((row for row in rows if row.status == "QUEUED"), None)
                if pending is None:
                    if experiment.status != "COMPLETED":
                        experiment.status = "COMPLETED"
                        self._touch(experiment)
                        append_progress(db, experiment)
                    return None
                if target is not None:
                    selected = next(
                        (row for row in rows if (row.repetition, row.case_id) == target), None
                    )
                    if selected is None:
                        _binding_error()
                    if selected.status != "QUEUED":
                        return None
                    # A damaged plan cannot jump an earlier unstarted row.
                    if selected.id != pending.id:
                        _binding_error()
                    pending = selected
                if experiment.status != "RUNNING":
                    return "FORMAL_EXPERIMENT_STOPPED"
                pending.status = "RUNNING"
                pending.started_at = now()
                pending.version += 1
                self._touch(experiment)
                append_progress(db, experiment, pending)
                result_id, case_id, expected, quota = (
                    pending.id,
                    pending.case_id,
                    pending.expected,
                    pending.quota,
                )
                self._active_quota = quota
            self._run_one(
                self.block.id,
                runner,
                self.block.config,
                self.plan.cases[case_id],
                result_id,
                expected,
                quota,
                attempt_continuation=lambda session, ident=result_id: self._continuation(
                    session, self.block.id, ident
                ),
            )
        return self.stop_reason
