from __future__ import annotations

import csv
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest, now, utc
from aftercare.business.service import _read_key
from aftercare.db import User

from .gates import evaluate as evaluate_gate
from .gates import read_gold_approvals, read_policies
from .imports import ImportInput, parse_report
from .metrics import METRICS_VERSION, _ignore, _percentile_samples, extend_metrics
from .progress import append_progress, read_progress
from .retrieval_evidence import (
    catalogue_sha256,
    fixture_policies,
    policy_snapshot,
    retrieval_configuration,
)
from .runner import UnsupportedFixture
from .scenarios import scenario_report
from .supplemental_usage import changes as supplemental_usage_changes
from .supplemental_usage import read_attempts as read_supplemental_attempts
from .tables import (
    EvaluationImport,
    EvaluationMutation,
    EvaluationResult,
    EvaluationReview,
    Experiment,
    ordinary_experiment,
)

DATASET_ID = "v0.2"
SCORER_VERSION = "ac-eval-2"
RETRYABLE = {"FAILED", "TIMEOUT", "ERROR", "NEEDS_REVIEW", "NOT_RUN"}
IMPORT_LOCK = 0x41434556414C
ACTIVE = {"QUEUED", "RUNNING"}
FINISHED = {"PASSED", "FAILED", "TIMEOUT", "ERROR", "NEEDS_REVIEW"}
log = logging.getLogger(__name__)


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    dataset_id: Literal["v0.2"]
    split: Literal["dev", "validation", "holdout", "regression", "test"] | None = None
    mode: Literal["single", "multi"]
    strategy: Literal["economy", "analysis", "routed"]
    repetitions: StrictInt = Field(ge=1, le=3)
    concurrency: StrictInt = Field(ge=1, le=4)
    max_calls: StrictInt = Field(ge=1, le=144)
    budget_usd: Decimal = Field(ge=0, le=10, max_digits=10, decimal_places=6)
    source: Literal["fixture", "live", "fault", "load"]
    case_ids: list[str] | None = Field(default=None, min_length=1, max_length=144)
    gate_policy_id: str | None = Field(default=None, max_length=64)
    load_reads: StrictInt | None = Field(default=None, ge=1, le=20)
    probe_intent: bool = False


def _uid():
    return str(uuid4())


def _time(value):
    return utc(value).isoformat() if value else None


def _authorized(user):
    if user.role != "tester":
        raise BusinessError("EVALUATION_FORBIDDEN", "仅测试员可访问评测工作台。", 403)


def _live_model_snapshot(model):
    if model is None:
        return None
    registry = model.registry
    return {
        "version": registry.version,
        "max_attempts": registry.max_attempts,
        "max_usd": str(registry.max_usd) if registry.max_usd is not None else None,
        "deadline_seconds": registry.deadline_seconds,
        "timeout_seconds": registry.timeout_seconds,
        "cooldown_seconds": registry.cooldown_seconds,
        "models": [
            spec.model_dump(mode="json", exclude={"endpoint", "api_key_env"})
            for spec in registry.models
        ],
        "registry_digest": digest(registry.model_dump(mode="json")),
    }


def _embedding_snapshot(settings):
    try:
        retrieval = retrieval_configuration(settings)
    except (OSError, ValueError) as error:
        raise BusinessError(
            "EVALUATION_EMBEDDING_SNAPSHOT_UNAVAILABLE", "评测所需的本地向量模型快照不可核验。", 422
        ) from error
    encoder = retrieval["encoder"]
    return {
        "embedding_enabled": encoder is not None,
        "embedding_model": encoder["model_id"] if encoder else None,
        "retrieval_snapshot": retrieval,
    }


def _dependency_lock_sha256(root: Path | None = None) -> str | None:
    package = root if root is not None else Path(__file__).resolve().parents[1]
    lock = package.parents[1] / "uv.lock"
    return sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None


def _source_fingerprint(root: Path | None = None) -> str:
    package = root if root is not None else Path(__file__).resolve().parents[1]
    sources = sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts)
    inputs = {
        path.relative_to(package).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sources
    }
    if (lock_digest := _dependency_lock_sha256(package)) is not None:
        inputs["backend-lock"] = lock_digest
    return digest(inputs)


# The API and worker import this module at startup, before either accepts jobs.
_RUNTIME_CODE_DIGEST = _source_fingerprint()


def require_current_code() -> str:
    try:
        current = _source_fingerprint()
    except OSError:
        current = None
    if current != _RUNTIME_CODE_DIGEST:
        raise BusinessError("CODE_RESTART_REQUIRED", "评测代码已更新，请重启服务后再试。", 503)
    return _RUNTIME_CODE_DIGEST


def _allocate(config, rows):
    count = len(rows)
    if config.source != "live":
        return [{"calls": 0, "budget_usd": "0"} for _ in rows]
    n = min(count, config.max_calls)
    calls, excess = divmod(config.max_calls, n)
    micros = int(config.budget_usd * 1_000_000)
    fee, fee_extra = divmod(micros, n)
    return [
        {
            "calls": calls + int(index < excess),
            "budget_usd": str(Decimal(fee + int(index < fee_extra)) / 1_000_000),
        }
        if index < n
        else {"calls": 0, "budget_usd": "0"}
        for index in range(count)
    ]


@contextmanager
def _lease(engine, experiment_id):
    if engine.dialect.name != "postgresql":
        yield True
        return
    lease = int.from_bytes(
        sha256(f"eval:{experiment_id}".encode()).digest()[:8], "big", signed=True
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        locked = conn.scalar(text("SELECT pg_try_advisory_lock(:lease)"), {"lease": lease})
        try:
            yield locked
        finally:
            if locked:
                conn.execute(text("SELECT pg_advisory_unlock(:lease)"), {"lease": lease})


def _mutate(db, user, scope, key, request):
    checksum = digest(request)
    row = db.get(EvaluationMutation, (user.user_id, scope, _read_key(key)))
    if row is not None and row.digest != checksum:
        raise BusinessError("IDEMPOTENCY_CONFLICT", "同一评测请求键已用于不同参数。")
    return row, checksum


def _remember(db, user, scope, key, checksum, object_id):
    db.add(
        EvaluationMutation(
            actor_id=user.user_id,
            scope=scope,
            key=key,
            digest=checksum,
            object_id=object_id,
        )
    )


def _version(row, expected):
    if type(expected) is not int or expected != row.version:
        raise BusinessError("VERSION_CONFLICT", "评测记录已更新，请刷新。")


def _review_json(row):
    return {
        "id": row.id,
        "decision": row.decision,
        "notes": row.notes,
        "reviewer_id": row.reviewer_id,
        "created_at": _time(row.created_at),
        "version": row.version,
        "annotations": row.annotations or {},
    }


def _result_json(row, reviews):
    raw = row.status
    effective = raw
    if raw == "NEEDS_REVIEW" and reviews:
        if reviews[-1]["decision"] == "FAIL":
            effective = "FAILED"
        elif (
            reviews[-1]["decision"] == "PASS"
            and row.checks
            and all(check["status"] == "PASS" for check in row.checks)
            and row.actual
            and row.actual.get("observed_source") not in {None, "no_provider_call"}
        ):
            effective = "PASSED"
    return {
        "id": row.id,
        "case_id": row.case_id,
        "repetition": row.repetition,
        "retry_of_result_id": row.retry_of_result_id,
        "status": effective,
        "machine_status": raw,
        "version": row.version,
        "intent": row.intent,
        "expected": row.expected,
        "actual": row.actual,
        "checks": row.checks,
        "trace": row.trace,
        "reviews": reviews,
        "failure_reason": row.failure_reason if effective != "PASSED" else None,
        "quota": row.quota,
        "started_at": _time(row.started_at),
        "finished_at": _time(row.finished_at),
    }


def _counts(results):
    started = sum(row["started_at"] is not None for row in results)
    return {
        "planned": len(results),
        "started": started,
        "finished": sum(
            row["finished_at"] is not None and row["started_at"] is not None for row in results
        ),
        "not_run": len(results) - started,
        "failed": sum(row["status"] in {"FAILED", "ERROR"} for row in results),
        "timed_out": sum(row["status"] == "TIMEOUT" for row in results),
        "cancelled": sum(row["failure_reason"] == "EXPERIMENT_CANCELLED" for row in results),
    }


def _metrics(rows, counts, source, *, evidence=None):
    metrics = []
    record = evidence.add if evidence is not None else _ignore

    def add(key, label, value, numerator, denominator, unit, basis):
        metrics.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "numerator": numerator,
                "denominator": denominator,
                "unit": unit,
                "source": basis,
            }
        )

    started, planned, finished = counts["started"], counts["planned"], counts["finished"]
    passed = sum(row["status"] == "PASSED" for row in rows)
    for row in rows:
        ran = row["started_at"] is not None
        ended = ran and row["finished_at"] is not None
        success = row["status"] == "PASSED"
        record(
            "task_success",
            row,
            "included" if success else "attention" if ran else "excluded",
            "TASK_PASSED" if success else "TASK_NOT_PASSED" if ran else "RUN_NOT_STARTED",
            measured=ran,
            numerator=int(success),
            denominator=int(ran),
            evidence={"status": row["status"], "failure_reason": row["failure_reason"]},
        )
        record(
            "coverage",
            row,
            "included" if ended else "attention",
            "RUN_FINISHED" if ended else "RUN_UNFINISHED" if ran else "RUN_NOT_STARTED",
            measured=True,
            numerator=int(ended),
            denominator=1,
        )

    add(
        "task_success",
        "任务成功率",
        passed / started if started else None,
        passed,
        started,
        "ratio",
        f"{source} · 程序及人工",
    )
    add(
        "coverage",
        "执行覆盖率",
        finished / planned if planned else None,
        finished,
        planned,
        "ratio",
        "执行记录",
    )
    checks = [(row, index, check) for row in rows for index, check in enumerate(row["checks"])]
    for outcome, label in [
        ("PASS", "确定性检查通过"),
        ("FAIL", "确定性检查失败"),
        ("UNKNOWN", "未知检查"),
    ]:
        key = f"check_{outcome.lower()}"
        n = 0
        for row, index, check in checks:
            match = check["status"] == outcome
            n += match
            record(
                key,
                row,
                {"PASS": "included", "FAIL": "attention", "UNKNOWN": "unknown"}[check["status"]],
                f"CHECK_{check['status']}",
                measured=True,
                numerator=int(match),
                denominator=1,
                evidence={"check_index": index, "path": check.get("path")},
            )
        add(key, label, n if checks else None, n, len(checks), "count", "确定性检查")

    done = [row for row in rows if row["finished_at"] and row["actual"]]
    model_uses = [
        (row, i, entry)
        for row in done
        for i, entry in enumerate(row["actual"].get("model_attempts", []))
    ]
    missing_cost = sum(entry.get("estimated_cost_usd") is None for _, _, entry in model_uses)
    known_cost = sum(
        (
            Decimal(entry["estimated_cost_usd"])
            for _, _, entry in model_uses
            if entry.get("estimated_cost_usd") is not None
        ),
        Decimal(0),
    )
    complete_audits = sum(bool(row["actual"].get("model_audit_complete")) for row in done)
    model_audit_complete = len(done) == started == complete_audits
    cost = float(known_cost) if started and model_audit_complete and not missing_cost else None
    known_attempt_count = (
        len(model_uses) if model_uses or (started and model_audit_complete) else None
    )
    for row in rows:
        actual = row["actual"] if row["finished_at"] and row["actual"] else {}
        complete = bool(actual.get("model_audit_complete"))
        if row["started_at"] is not None or complete:
            record(
                "model_audit_coverage",
                row,
                "included" if complete else "attention",
                "AUDIT_COMPLETE" if complete else "AUDIT_MISSING",
                measured=True,
                numerator=int(complete),
                denominator=int(row["started_at"] is not None),
            )
            if not complete or not actual.get("model_attempts"):
                for key in ("model_attempts", "cost", "cost_per_success", "cost_per_started"):
                    record(
                        key,
                        row,
                        "included" if complete else "unknown",
                        "NO_MODEL_CALLS" if complete else "AUDIT_MISSING",
                    )
        if row["started_at"] is not None:
            record(
                "cost_per_started",
                row,
                "included",
                "COST_DENOMINATOR",
                measured=True,
                denominator=1,
            )
        if row["status"] == "PASSED":
            record(
                "cost_per_success",
                row,
                "included",
                "COST_DENOMINATOR",
                measured=True,
                denominator=1,
            )
    for row, index, entry in model_uses:
        locator = {
            "attempt_index": index,
            "logical_id": entry.get("logical_id"),
            "cycle": entry.get("cycle"),
            "ordinal": entry.get("ordinal"),
        }
        estimate = entry.get("estimated_cost_usd")
        record(
            "model_attempts",
            row,
            "included",
            "OBSERVATION_PRESENT",
            measured=True,
            numerator=1,
            evidence=locator,
        )
        record(
            "cost_unknown",
            row,
            "unknown" if estimate is None else "included",
            "COST_UNKNOWN" if estimate is None else "COST_RECORDED",
            measured=True,
            numerator=int(estimate is None),
            denominator=1,
            evidence=locator,
        )
        for key in ("cost", "cost_per_success", "cost_per_started"):
            record(
                key,
                row,
                "unknown" if estimate is None else "included",
                "COST_UNKNOWN" if estimate is None else "COST_RECORDED",
                measured=key == "cost" and estimate is not None,
                value=estimate,
                evidence=locator,
            )
    add(
        "model_attempts",
        "已记录模型调用",
        known_attempt_count,
        known_attempt_count,
        started,
        "count",
        "模型调用账本 · 审计不全时为已记录下限",
    )
    add(
        "model_audit_coverage",
        "调用审计覆盖率",
        complete_audits / started if started else None,
        complete_audits,
        started,
        "ratio",
        "模型调用账本",
    )
    add(
        "cost_unknown",
        "费用未知调用占比",
        missing_cost / len(model_uses) if model_uses else None,
        missing_cost,
        len(model_uses),
        "ratio",
        "模型调用账本",
    )
    add("cost", "总估算费用", cost, None, started, "usd", "模型调用账本")
    add(
        "cost_per_success",
        "每成功案估算费用",
        cost / passed if cost is not None and passed else None,
        None,
        passed,
        "usd",
        "模型调用账本与人工",
    )

    elapsed = [
        (row, row["actual"]["elapsed_ms"], {"field": "actual.elapsed_ms"})
        for row in done
        if row["actual"].get("elapsed_ms") is not None
    ]
    for percent in (50, 95):
        key = f"latency_p{percent}"
        for row in rows:
            if row["started_at"] and not (
                row in done and row["actual"].get("elapsed_ms") is not None
            ):
                record(
                    key,
                    row,
                    "unknown",
                    "OBSERVATION_MISSING",
                    evidence={"field": "actual.elapsed_ms"},
                )
        add(
            key,
            f"执行耗时 P{percent}",
            _percentile_samples(key, elapsed, percent, record),
            None,
            len(elapsed),
            "ms",
            "脚本端到端时钟",
        )

    retrieval_hits = retrieval_labels = tool_ok = tool_count = blocks = 0
    for row in rows:
        required = set(row["expected"].get("required_policy_ids", []))
        history = [
            (index, event["payload"])
            for index, event in enumerate(row["trace"])
            if event.get("kind") == "retrieval"
        ]
        if required and history:
            index, event = history[-1]
            fused = set(event.get("fused", [])[:5])
            for policy in sorted(required):
                hit = policy in fused
                retrieval_labels += 1
                retrieval_hits += hit
                record(
                    "recall_at_5",
                    row,
                    "included" if hit else "attention",
                    "OBSERVATION_PASSED" if hit else "OBSERVATION_FAILED",
                    measured=True,
                    numerator=int(hit),
                    denominator=1,
                    evidence={"trace_index": index, "policy_id": policy},
                )
        elif required:
            for policy in sorted(required):
                record(
                    "recall_at_5",
                    row,
                    "unknown",
                    "OBSERVATION_MISSING",
                    evidence={"policy_id": policy},
                )
        row_blocks = 0
        for index, event in enumerate(row["trace"]):
            if event.get("kind") == "tool":
                ok = event["payload"]["status"] == "SUCCEEDED"
                tool_count += 1
                tool_ok += ok
                record(
                    "observed_tool_success",
                    row,
                    "included" if ok else "attention",
                    "OBSERVATION_PASSED" if ok else "OBSERVATION_FAILED",
                    measured=True,
                    numerator=int(ok),
                    denominator=1,
                    evidence={"trace_index": index},
                )
            if event.get("kind") == "draft_rejected":
                row_blocks += 1
                record(
                    "draft_rule_blocks",
                    row,
                    "attention",
                    "DRAFT_BLOCKED",
                    numerator=1,
                    evidence={"trace_index": index},
                )
        blocks += row_blocks
        if row["started_at"] is not None:
            record(
                "draft_rule_blocks",
                row,
                "included" if row in done else "unknown",
                "OBSERVATION_PRESENT"
                if row_blocks
                else "NO_DRAFT_BLOCK"
                if row in done
                else "OBSERVATION_MISSING",
                measured=row in done,
                denominator=1,
            )
    add(
        "recall_at_5",
        "政策检索 Recall@5",
        retrieval_hits / retrieval_labels if retrieval_labels else None,
        retrieval_hits,
        retrieval_labels,
        "ratio",
        "末次检索 · 合成条款参考待人工核验",
    )
    add(
        "observed_tool_success",
        "已记录工具调用成功率",
        tool_ok / tool_count if tool_count else None,
        tool_ok,
        tool_count,
        "ratio",
        "运行轨迹",
    )
    add(
        "draft_rule_blocks",
        "草稿规则拦截记录",
        blocks if done else None,
        blocks,
        started,
        "count",
        "运行轨迹",
    )
    metrics.extend(extend_metrics(rows, known_cost=cost, cost_denominator=started, record=record))
    return metrics


class EvaluationService:
    def __init__(
        self,
        engine,
        settings,
        *,
        dispatch=None,
        model=None,
        catalog=None,
        gate_policies=None,
        attempt_admission=None,
        execution_driver: Literal["queued", "inline"] = "queued",
    ):
        self.engine, self.settings = engine, settings
        self.dispatch, self.model, self.catalog = dispatch, model, catalog
        self.gate_policies = gate_policies
        self.attempt_admission = attempt_admission
        self.execution_driver = execution_driver

    def _catalog(self):
        if self.catalog is None:
            raise FileNotFoundError("Legacy evaluation catalogue retired")
        return self.catalog

    def capabilities(self, user):
        _authorized(user)
        enabled = self.settings.profile == "test"
        legacy_enabled = enabled and self.catalog is not None
        return {
            "eval_import": legacy_enabled,
            "eval_retry": legacy_enabled,
            "eval_gates": legacy_enabled,
            "eval_supplemental_usage": legacy_enabled,
            "eval_scenario_metrics": self.catalog is not None,
            "eval_progress_events": legacy_enabled,
            "chat_blind_review": enabled,
        }

    def gates(self, user):
        _authorized(user)
        return [policy.model_dump(mode="json") for policy in self._gate_catalogue()]

    def _gate_catalogue(self):
        try:
            return self.gate_policies if self.gate_policies is not None else read_policies()
        except (OSError, ValueError):
            raise BusinessError("GATE_CATALOGUE_INVALID", "发布门槛目录无法读取。", 503) from None

    def datasets(self, user):
        _authorized(user)
        try:
            rows = self._catalog().load_cases()
        except FileNotFoundError:
            return []
        return [
            {
                "id": DATASET_ID,
                "name": "三类售后与冲突回归",
                "version": DATASET_ID,
                "case_count": len(rows),
                "source": "synthetic_pending_human_review",
            }
        ]

    def cases(self, user, dataset_id, *, split=None):
        _authorized(user)
        if dataset_id != DATASET_ID:
            raise BusinessError("DATASET_NOT_FOUND", "数据集版本不存在。", 404)
        try:
            catalogue = self._catalog()
            cases = catalogue.load_cases("holdout" if split == "test" else split)
            refs = catalogue.load_references()
        except FileNotFoundError:
            raise BusinessError("DATASET_NOT_FOUND", "数据集版本尚未安装。", 404) from None
        return [
            {
                "id": row["id"],
                "dataset_id": DATASET_ID,
                "split": row["split"],
                "intent": refs.get(row["id"], {}).get("intent"),
                "scenario": refs.get(row["id"], {}).get("family", ""),
                "difficulty": refs.get(row["id"], {}).get("difficulty", "standard"),
                "review_status": refs.get(row["id"], {}).get("human_review", "PENDING"),
                "input": {"message": row["message"], "events": row["events"]},
                "snapshot": row["fixture"],
                "expected": refs.get(row["id"]),
            }
            for row in cases
        ]

    def _guard_test(self):
        if self.settings.profile != "test":
            raise BusinessError("EVALUATION_REQUIRES_TEST", "评测运行仅限独立测试环境。", 403)

    @staticmethod
    def _find(db, experiment_id, *, lock=False):
        query = select(Experiment).where(Experiment.id == experiment_id, ordinary_experiment())
        row = db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise BusinessError("EXPERIMENT_NOT_FOUND", "评测实验不存在。", 404)
        return row

    @staticmethod
    def _find_result(db, result_id, *, lock=False):
        query = (
            select(EvaluationResult)
            .join(Experiment, Experiment.id == EvaluationResult.experiment_id)
            .where(EvaluationResult.id == result_id, ordinary_experiment())
        )
        row = db.scalar(query.with_for_update(of=EvaluationResult) if lock else query)
        if row is None:
            raise BusinessError("RESULT_NOT_FOUND", "案例结果不存在。", 404)
        return row

    @staticmethod
    def _results(db, experiment_id):
        return list(
            db.scalars(
                select(EvaluationResult)
                .where(EvaluationResult.experiment_id == experiment_id)
                .order_by(EvaluationResult.repetition, EvaluationResult.case_id)
            )
        )

    def _detail(self, db, row, *, include_results=True):
        raw = self._results(db, row.id)
        reviews = {}
        if raw:
            for review in db.scalars(
                select(EvaluationReview)
                .where(EvaluationReview.result_id.in_([item.id for item in raw]))
                .order_by(EvaluationReview.result_id, EvaluationReview.version)
            ):
                reviews.setdefault(review.result_id, []).append(_review_json(review))
        results = [_result_json(item, reviews.get(item.id, [])) for item in raw]
        counts = _counts(results)
        metrics = _metrics(results, counts, row.config["source"])
        return {
            "id": row.id,
            "name": row.name,
            "parent_experiment_id": row.parent_experiment_id,
            "status": row.status,
            "version": row.version,
            "created_at": _time(row.created_at),
            "updated_at": _time(row.updated_at),
            "config": row.config,
            "counts": counts,
            "results": results if include_results else [],
            "metrics": metrics,
            "gate": evaluate_gate(
                row.config.get("gate_policy"),
                results,
                counts,
                metrics,
                source=row.config["source"],
                status=row.status,
            ),
        }

    def experiments(self, user, *, limit=100, offset=0):
        _authorized(user)
        with Session(self.engine) as db:
            rows = list(
                db.scalars(
                    select(Experiment)
                    .where(ordinary_experiment())
                    .order_by(
                        Experiment.created_at.desc(),
                        Experiment.id,
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [self._detail(db, row, include_results=False) for row in rows]

    def experiment(self, user, experiment_id):
        _authorized(user)
        with Session(self.engine) as db:
            return self._detail(db, self._find(db, experiment_id))

    def progress(self, user, experiment_id, *, after, limit):
        _authorized(user)
        self._guard_test()
        return read_progress(self.engine, experiment_id, after, limit)

    def scenario_metrics(self, user, experiment_id, **filters):
        _authorized(user)
        # One SQL statement gives a consistent view even under READ COMMITTED.
        latest_review = (
            select(func.max(EvaluationReview.version))
            .where(EvaluationReview.result_id == EvaluationResult.id)
            .correlate(EvaluationResult)
            .scalar_subquery()
        )
        statement = (
            select(Experiment, EvaluationResult, EvaluationReview)
            .outerjoin(EvaluationResult, EvaluationResult.experiment_id == Experiment.id)
            .outerjoin(
                EvaluationReview,
                and_(
                    EvaluationReview.result_id == EvaluationResult.id,
                    EvaluationReview.version == latest_review,
                ),
            )
            .where(Experiment.id == experiment_id, ordinary_experiment())
            .order_by(
                EvaluationResult.case_id,
                EvaluationResult.repetition,
                EvaluationResult.id,
            )
        )
        with Session(self.engine) as db:
            joined = db.execute(statement).all()
            if not joined:
                raise BusinessError("EXPERIMENT_NOT_FOUND", "评测实验不存在。", 404)
            experiment = joined[0][0]
            results = [
                _result_json(result, [_review_json(review)] if review is not None else [])
                for _, result, review in joined
                if result is not None
            ]
            return scenario_report(experiment, results, _counts, _metrics, **filters)

    def create(self, user, request: ExperimentInput, key):
        _authorized(user)
        self._guard_test()
        code_digest = require_current_code()
        data = request.model_dump(mode="json")
        name = request.name.strip()
        if not name:
            raise BusinessError("INVALID_EXPERIMENT", "请输入实验名称。", 422)
        data["name"] = name
        scope = "create:inline" if self.execution_driver == "inline" else "create"
        _read_key(key)
        with Session(self.engine) as db:
            replay, _ = _mutate(db, user, scope, key, data)
            if replay is not None:
                return self._detail(db, self._find(db, replay.object_id))
        if request.source == "live" and (request.budget_usd <= 0 or self.model is None):
            raise BusinessError(
                "LIVE_EVALUATION_UNAVAILABLE", "真实评测须有已配置模型和正数费用上限。", 422
            )
        if request.source != "live" and request.budget_usd:
            raise BusinessError(
                "INVALID_EXPERIMENT", "离线、故障与负载实验的模型费用上限必须为零。", 422
            )

        gate_policy = None
        if request.gate_policy_id:
            gate_policy = next(
                (
                    policy
                    for policy in self._gate_catalogue()
                    if policy.id == request.gate_policy_id
                ),
                None,
            )
            if gate_policy is None:
                raise BusinessError("GATE_POLICY_NOT_FOUND", "未找到指定发布门槛。", 404)
        if request.probe_intent and request.source != "live":
            raise BusinessError("INVALID_INTENT_PROBE", "真实意图探针只用于真实模型实验。", 422)
        if request.load_reads is not None and request.source != "load":
            raise BusinessError("INVALID_LOAD_PLAN", "压力请求数仅用于负载实验。", 422)
        if request.source == "load" and request.concurrency < 2:
            raise BusinessError("INVALID_LOAD_PLAN", "负载实验至少需要两个并发执行通道。", 422)

        split = "holdout" if request.split == "test" else request.split
        if request.source == "fault" and split is None:
            split = "regression"
        try:
            catalogue = self._catalog()
            cases = catalogue.load_cases(split)
            references = catalogue.load_references()
        except FileNotFoundError:
            raise BusinessError("DATASET_NOT_FOUND", "数据集版本尚未安装。", 404) from None
        requested_cases = request.case_ids or []
        selected = set(requested_cases)
        if selected:
            if len(selected) != len(requested_cases) or not selected <= {
                case["id"] for case in cases
            }:
                raise BusinessError(
                    "CASE_SELECTION_INVALID", "选中的案例无效、重复或不在本划分。", 422
                )
            cases = [case for case in cases if case["id"] in selected]
        if not cases:
            raise BusinessError("CASE_SELECTION_EMPTY", "当前划分没有可运行的案例。", 422)
        # Repeated runs are separate attempts on the same independently numbered cases.
        planned = [
            (case, repeat)
            for repeat in range(1, request.repetitions + 1)
            for case in sorted(cases, key=lambda case: case["id"])
        ]
        load_eligible = sum(
            not any(step["op"] in {"fault", "parallel_execute"} for step in case["events"])
            for case, _ in planned
        )
        if request.source == "load" and load_eligible < request.concurrency:
            raise BusinessError("INVALID_LOAD_PLAN", "负载实验案例数须覆盖全部并发执行通道。", 422)
        quotas = _allocate(request, planned)
        case_hashes = {case["id"]: digest(case) for case in cases}
        case_policies = {
            case["id"]: catalogue_sha256(fixture_policies(case["fixture"])) for case in cases
        }
        try:
            gold_approvals = read_gold_approvals()
        except (OSError, ValueError):
            raise BusinessError("GOLD_CATALOGUE_INVALID", "人工核验目录无法读取。", 503) from None
        selected_references = {}
        for case in cases:
            case_id = case["id"]
            reference = references.get(case_id) or {}
            approval = gold_approvals.get(case_id)
            if approval is not None:
                if not reference or (
                    approval.case_sha256 != case_hashes[case_id]
                    or approval.reference_sha256 != digest(reference)
                ):
                    raise BusinessError(
                        "GOLD_SNAPSHOT_MISMATCH", "人工核验的用例或参考摘要与当前版本不同。"
                    )
                reference = {
                    **reference,
                    "original_label_source": reference.get("label_source"),
                    "human_review": "VERIFIED",
                    "label_source": "INDEPENDENT_HUMAN",
                    "reviewer_id": approval.reviewer_id,
                    "reviewed_at": _time(approval.reviewed_at),
                    "gold_approval": approval.model_dump(mode="json"),
                }
            selected_references[case_id] = reference
        ref_hashes = {
            case_id: digest(reference) for case_id, reference in selected_references.items()
        }
        config = {
            **{key_: value for key_, value in data.items() if key_ not in {"name", "case_ids"}},
            "split": split,
            "budget_usd": float(request.budget_usd),
            "case_ids": sorted(case_hashes),
            "input_digest": digest(case_hashes),
            "policy_snapshot": policy_snapshot(case_policies),
            "reference_digest": digest(ref_hashes),
            "scorer_version": SCORER_VERSION,
            "metrics_version": METRICS_VERSION,
            "gate_policy": gate_policy.model_dump(mode="json") if gate_policy else None,
            "load_reads": (request.load_reads or 5) if request.source == "load" else None,
            "code_digest": code_digest,
            "dependency_lock_sha256": _dependency_lock_sha256(),
            "execution_driver": self.execution_driver,
            "model_snapshot": _live_model_snapshot(self.model)
            if request.source == "live"
            else None,
            **_embedding_snapshot(self.settings),
        }

        with Session(self.engine) as db, db.begin():
            if self.engine.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:lease)"),
                    {
                        "lease": int.from_bytes(
                            sha256(f"eval:create:{user.user_id}:{key}".encode()).digest()[:8],
                            "big",
                            signed=True,
                        ),
                    },
                )
            replay, checksum = _mutate(db, user, scope, key, data)
            if replay is not None:
                return self._detail(db, self._find(db, replay.object_id))
            stamp = now()
            row = Experiment(
                id=_uid(),
                name=name,
                status="QUEUED",
                version=1,
                progress_seq=0,
                config=config,
                created_by=user.user_id,
                created_at=stamp,
                updated_at=stamp,
            )
            experiment_id = row.id
            self._store_plan(
                db,
                row,
                (
                    (
                        case["id"],
                        repeat,
                        selected_references[case["id"]],
                        {
                            **quota,
                            "case_hash": case_hashes[case["id"]],
                            "policy_catalogue_sha256": case_policies[case["id"]],
                        },
                    )
                    for (case, repeat), quota in zip(planned, quotas, strict=True)
                ),
            )
            _remember(db, user, scope, key, checksum, row.id)
            response = self._detail(db, row)

        self._dispatch(experiment_id)
        return response

    @staticmethod
    def _store_plan(db, experiment, planned):
        db.add(experiment)
        db.flush()
        append_progress(db, experiment)
        for case_id, repetition, reference, quota in planned:
            result = EvaluationResult(
                id=_uid(),
                experiment_id=experiment.id,
                case_id=case_id,
                repetition=repetition,
                status="QUEUED",
                version=1,
                intent=reference.get("intent"),
                expected=reference,
                actual=None,
                checks=[],
                trace=[],
                quota=quota,
            )
            db.add(result)
            append_progress(db, experiment, result)

    def _dispatch(self, experiment_id):
        if self.execution_driver == "queued" and self.dispatch is not None:
            try:
                with Session(self.engine) as db:
                    visible = db.scalar(
                        select(Experiment.id).where(
                            Experiment.id == experiment_id, ordinary_experiment()
                        )
                    )
                if visible is not None:
                    self.dispatch(experiment_id)
            except Exception:  # noqa: BLE001 - plan already committed for broker recovery
                log.warning("Evaluation dispatch pending: %s", experiment_id)

    @staticmethod
    def _import_json(row, *, detailed=False):
        output = {
            "id": row.id,
            "sha256": row.sha256,
            "origin": row.origin,
            "source": "imported_unverified",
            "created_at": _time(row.created_at),
            "created_by": row.created_by,
            **row.summary,
        }
        if detailed:
            output["report"] = json.loads(row.content)
        return output

    def imports(self, user, *, limit=100):
        _authorized(user)
        with Session(self.engine) as db:
            rows = db.scalars(
                select(EvaluationImport)
                .order_by(EvaluationImport.created_at.desc(), EvaluationImport.id)
                .limit(limit)
            )
            return [self._import_json(row) for row in rows]

    def imported_report(self, user, import_id):
        _authorized(user)
        with Session(self.engine) as db:
            row = db.get(EvaluationImport, import_id)
            if row is None:
                raise BusinessError("IMPORT_NOT_FOUND", "导入记录不存在。", 404)
            return self._import_json(row, detailed=True)

    def import_report(self, user, request: ImportInput, key):
        _authorized(user)
        self._guard_test()
        origin = request.origin.strip()
        if not origin:
            raise BusinessError("INVALID_REPORT", "请填写报告来源。", 422)
        report = parse_report(request)
        if any(
            report.get(field) is not None or report["config"].get(field) is not None
            for field in ("formal_freeze_sha256", "formal_condition_key")
        ):
            raise BusinessError(
                "FORMAL_REPORT_IMPORT_FORBIDDEN", "正式评测报告不能通过普通入口导入。", 422
            )
        with Session(self.engine) as db, db.begin():
            if self.engine.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IMPORT_LOCK})
            report_id = report.get("id")
            if isinstance(report_id, str) and db.scalar(
                select(Experiment.id).where(Experiment.id == report_id, ~ordinary_experiment())
            ):
                raise BusinessError(
                    "FORMAL_REPORT_IMPORT_FORBIDDEN", "正式评测报告不能通过普通入口导入。", 422
                )
            replay, checksum = _mutate(db, user, "import", key, [request.sha256, origin])
            if replay is not None:
                return self._import_json(db.get_one(EvaluationImport, replay.object_id))
            row = db.scalar(
                select(EvaluationImport).where(EvaluationImport.sha256 == request.sha256)
            )
            if row is None:
                row = EvaluationImport(
                    id=_uid(),
                    sha256=request.sha256,
                    origin=origin,
                    content=request.content,
                    summary={field: report[field] for field in ("name", "status", "counts")},
                    created_by=user.user_id,
                    created_at=now(),
                )
                db.add(row)
            _remember(db, user, "import", key, checksum, row.id)
            return self._import_json(row)

    def retry(self, user, experiment_id, *, expected_version, result_ids, name=None, key):
        _authorized(user)
        self._guard_test()
        code_digest = require_current_code()
        identifiers = sorted(set(result_ids))
        if not identifiers or len(identifiers) != len(result_ids):
            raise BusinessError("RESULT_NOT_RETRYABLE", "请选择不重复的待补跑结果。", 422)
        name = name.strip() if name is not None else None
        if name is not None and not name:
            raise BusinessError("INVALID_EXPERIMENT", "请输入实验名称。", 422)
        with Session(self.engine) as db, db.begin():
            original = self._find(db, experiment_id, lock=True)
            scope = (
                f"retry:inline:{original.id}"
                if self.execution_driver == "inline"
                else f"retry:{original.id}"
            )
            replay, checksum = _mutate(db, user, scope, key, [expected_version, identifiers, name])
            if replay is not None:
                return self._detail(db, self._find(db, replay.object_id))
            _version(original, expected_version)
            if original.status in ACTIVE:
                raise BusinessError("RESULT_NOT_RETRYABLE", "请等待原实验结束后再补跑。")
            selected = list(
                db.scalars(
                    select(EvaluationResult)
                    .where(
                        EvaluationResult.experiment_id == original.id,
                        EvaluationResult.id.in_(identifiers),
                    )
                    .order_by(EvaluationResult.id)
                    .with_for_update()
                )
            )
            if len(selected) != len(identifiers) or any(
                row.status not in RETRYABLE for row in selected
            ):
                raise BusinessError("RESULT_NOT_RETRYABLE", "所选结果不属于可补跑范围。")
            views = {row["id"]: row for row in self._detail(db, original)["results"]}
            if any(views[row.id]["status"] not in RETRYABLE for row in selected):
                raise BusinessError(
                    "RESULT_NOT_RETRYABLE", "已有通过记录的案例不能从失败入口补跑。"
                )
            config = dict(original.config)
            case_ids = sorted({row.case_id for row in selected})
            input_hashes = {row.case_id: row.quota["case_hash"] for row in selected}
            ref_hashes = {row.case_id: digest(row.expected) for row in selected}
            config.update(
                case_ids=case_ids,
                input_digest=digest(input_hashes),
                reference_digest=digest(ref_hashes),
                code_digest=code_digest,
                dependency_lock_sha256=_dependency_lock_sha256(),
                scorer_version=SCORER_VERSION,
                metrics_version=METRICS_VERSION,
                execution_driver=self.execution_driver,
                **_embedding_snapshot(self.settings),
            )
            case_policies = {
                row.case_id: row.quota.get("policy_catalogue_sha256") for row in selected
            }
            if all(case_policies.values()):
                config["policy_snapshot"] = policy_snapshot(case_policies)
            else:
                config.pop("policy_snapshot", None)
            if config["source"] == "live":
                if self.model is None:
                    raise BusinessError("LIVE_EVALUATION_UNAVAILABLE", "真实模型未配置。", 422)
                config["max_calls"] = sum(row.quota["calls"] for row in selected)
                config["budget_usd"] = float(
                    sum(Decimal(row.quota["budget_usd"]) for row in selected)
                )
                config["model_snapshot"] = _live_model_snapshot(self.model)
            if config["source"] == "load" and len(selected) < config["concurrency"]:
                raise BusinessError("INVALID_LOAD_PLAN", "补跑结果须覆盖全部负载执行通道。", 422)
            stamp = now()
            retry = Experiment(
                id=_uid(),
                parent_experiment_id=original.id,
                name=name or f"补跑 · {original.name}"[:120],
                status="QUEUED",
                version=1,
                progress_seq=0,
                config=config,
                created_by=user.user_id,
                created_at=stamp,
                updated_at=stamp,
            )
            db.add(retry)
            db.flush()
            append_progress(db, retry)
            new_id = retry.id
            for row in selected:
                result = EvaluationResult(
                    id=_uid(),
                    experiment_id=new_id,
                    retry_of_result_id=row.id,
                    case_id=row.case_id,
                    repetition=row.repetition,
                    status="QUEUED",
                    version=1,
                    intent=row.intent,
                    expected=row.expected,
                    actual=None,
                    checks=[],
                    trace=[],
                    quota=row.quota,
                )
                db.add(result)
                append_progress(db, retry, result)
            _remember(db, user, scope, key, checksum, new_id)
            response = self._detail(db, retry)
        self._dispatch(new_id)
        return response

    def cancel(self, user, experiment_id, *, expected_version, key):
        _authorized(user)
        self._guard_test()
        scope = f"cancel:{experiment_id}"
        with Session(self.engine) as db, db.begin():
            row = self._find(db, experiment_id, lock=True)
            replay, checksum = _mutate(db, user, scope, key, [expected_version])
            if replay is None:
                _version(row, expected_version)
                if row.status not in ACTIVE:
                    raise BusinessError("EXPERIMENT_FINISHED", "实验已结束。")
                row.status = "CANCELLED"
                self._touch(row)
                append_progress(db, row)
                for item in self._results(db, row.id):
                    if item.status == "QUEUED":
                        item.status = "NOT_RUN"
                        item.failure_reason = "EXPERIMENT_CANCELLED"
                        item.version += 1
                        append_progress(db, row, item)
                _remember(db, user, scope, key, checksum, row.id)
            return self._detail(db, row)

    def supplemental_usage(self, user, result_id):
        _authorized(user)
        self._guard_test()
        with Session(self.engine) as db:
            row = self._find_result(db, result_id)
            result_id = row.id
            finished = row.status in FINISHED and row.finished_at is not None
            snapshot = row.actual.get("model_attempts") if isinstance(row.actual, dict) else None

        attempts, unmatched, unmatched_snapshot = [], 0, 0
        if not finished:
            reason = "RESULT_NOT_FINISHED"
        elif not isinstance(snapshot, list):
            reason = "SNAPSHOT_FORMAT_UNSUPPORTED"
        else:
            raw, reason = read_supplemental_attempts(self.engine, result_id)
            if reason is None:
                attempts, unmatched, unmatched_snapshot = supplemental_usage_changes(raw, snapshot)
        return {
            "result_id": result_id,
            "as_of": _time(now()),
            "status": "AVAILABLE" if reason is None else "UNAVAILABLE",
            "unavailable_reason": reason,
            "unmatched_attempt_count": unmatched,
            "unmatched_snapshot_attempt_count": unmatched_snapshot,
            "attempts": attempts,
        }

    def native_validation_snapshot(self, user, plan, request):
        _authorized(user)
        self._guard_test()
        from aftercare.config import TARGETS

        from .formal.native import build_snapshot, read_isolated

        if self.engine.dialect.name != "postgresql":
            raise BusinessError(
                "FORMAL_NATIVE_POSTGRES_REQUIRED", "原生验证需要测试 PostgreSQL。", 422
            )
        _, database, username, cluster = TARGETS["test"]
        with (
            self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection,
            connection.begin(),
        ):
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            target = connection.execute(
                text(
                    "SELECT current_database(), current_user, current_setting('cluster_name'), transaction_timestamp()"
                )
            ).one()
            if target[:3] != (database, username, cluster):
                raise BusinessError("FORMAL_NATIVE_TEST_REQUIRED", "原生验证仅限测试数据库。", 403)
            with Session(connection) as db:
                actor = db.get(User, user.user_id)
                if actor is None or not actor.enabled or actor.role != "tester":
                    raise BusinessError("EVALUATION_FORBIDDEN", "需要有效的测试员身份。", 403)
                reports = {
                    ident: self._detail(db, self._find(db, ident))
                    for ident in dict.fromkeys(
                        str(item.experiment_id) for item in request.experiments
                    )
                }
                return build_snapshot(
                    plan,
                    request,
                    reports,
                    lambda result_id: read_isolated(connection, result_id),
                    read_at=target[3],
                )

    def review(self, user, result_id, *, expected_version, key, decision, notes, annotations=None):
        _authorized(user)
        notes = notes.strip()
        if decision not in {"PASS", "FAIL", "UNKNOWN"} or not 1 <= len(notes) <= 1500:
            raise BusinessError("INVALID_REVIEW", "请选择人工复核结论并填写依据。", 422)
        scope = f"review:{result_id}"
        with Session(self.engine) as db, db.begin():
            row = self._find_result(db, result_id, lock=True)
            replay, checksum = _mutate(
                db, user, scope, key, [expected_version, decision, notes, annotations]
            )
            if replay is not None:
                return _review_json(db.get_one(EvaluationReview, replay.object_id))
            _version(row, expected_version)
            if row.status not in FINISHED:
                raise BusinessError("RESULT_NOT_FINISHED", "该案例还没有可复核的已结束结果。")
            row.version += 1
            review = EvaluationReview(
                id=_uid(),
                result_id=row.id,
                version=row.version,
                decision=decision,
                notes=notes,
                annotations=annotations,
                reviewer_id=user.user_id,
                created_at=now(),
            )
            db.add(review)
            _remember(db, user, scope, key, checksum, review.id)
            return _review_json(review)

    def compare(self, user, left, right):
        _authorized(user)
        with Session(self.engine) as db:
            left_row, right_row = self._find(db, left), self._find(db, right)
            if left == right:
                raise BusinessError("COMPARISON_REQUIRES_DISTINCT", "请选择两个不同的实验。", 422)
            a = self._detail(db, left_row)
            b = self._detail(db, right_row)
        reasons = []
        fixed = set(a["config"]) | set(b["config"])
        for key in sorted(fixed - {"mode", "strategy"}):
            if a["config"].get(key) != b["config"].get(key):
                reasons.append(f"冻结条件不同 · {key}")
        ca, cb = a["config"], b["config"]
        if ca["strategy"] != cb["strategy"] and (ca["mode"] != "single" or cb["mode"] != "single"):
            reasons.append("模型策略对照须固定为单 Agent。")
        index = {(row["case_id"], row["repetition"]): row["status"] for row in b["results"]}
        left_pairs = {(row["case_id"], row["repetition"]) for row in a["results"]}
        if left_pairs != set(index):
            reasons.append("两组案例或重复编号不一致。")
        if reasons:
            return {"compatible": False, "reasons": reasons, "rows": [], "metrics": []}
        right_metrics = {row["key"]: row for row in b["metrics"]}
        pairs = []
        paired = 0
        for row in a["results"]:
            status = index[(row["case_id"], row["repetition"])]
            first = row["status"]
            change = "UNPAIRED"
            if first in FINISHED and status in FINISHED:
                paired += 1
                change = "UNCHANGED" if first == status else "CHANGED"
                if first == "PASSED" and status != "PASSED":
                    change = "REGRESSION"
                elif first != "PASSED" and status == "PASSED":
                    change = "IMPROVED"
            pairs.append(
                {
                    "case_id": row["case_id"],
                    "repetition": row["repetition"],
                    "left_status": first,
                    "right_status": status,
                    "change": change,
                }
            )
        complete = bool(pairs) and paired == len(pairs)
        return {
            "compatible": True,
            "reasons": [],
            "rows": pairs,
            "pairing": {"planned": len(pairs), "finished": paired, "complete": complete},
            "metrics": [
                {
                    "key": row["key"],
                    "label": row["label"],
                    "unit": row["unit"],
                    "left": row["value"],
                    "right": right_metrics[row["key"]]["value"],
                    "left_denominator": row["denominator"],
                    "right_denominator": right_metrics[row["key"]]["denominator"],
                }
                for row in a["metrics"]
            ]
            if complete
            else [],
        }

    def export_csv(self, user, experiment_id):
        report = self.experiment(user, experiment_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "experiment_id",
                "name",
                "source",
                "case_id",
                "repetition",
                "status",
                "machine_status",
                "failure_reason",
                "started_at",
                "finished_at",
                "latest_review",
                "planned",
                "started",
                "finished",
                "not_run",
                "task_success",
                "gate_status",
            ]
        )
        success = next(item["value"] for item in report["metrics"] if item["key"] == "task_success")
        for row in report["results"]:
            writer.writerow(
                [
                    _safe_cell(cell)
                    for cell in [
                        report["id"],
                        report["name"],
                        report["config"]["source"],
                        row["case_id"],
                        row["repetition"],
                        row["status"],
                        row["machine_status"],
                        row["failure_reason"],
                        row["started_at"],
                        row["finished_at"],
                        row["reviews"][-1]["decision"] if row["reviews"] else None,
                        *(
                            report["counts"][key]
                            for key in ("planned", "started", "finished", "not_run")
                        ),
                        success,
                        report["gate"]["status"],
                    ]
                ]
            )
        return output.getvalue()

    @staticmethod
    def _touch(experiment):
        experiment.version += 1
        experiment.updated_at = now()

    def pending(self, limit=100):
        if self.catalog is None or self.settings.profile != "test" or self.execution_driver != "queued":
            return []
        driver = Experiment.config["execution_driver"].as_string()
        with Session(self.engine) as db:
            return list(
                db.scalars(
                    select(Experiment.id)
                    .where(
                        ordinary_experiment(),
                        or_(driver.is_(None), driver == "queued"),
                        or_(
                            Experiment.status.in_(ACTIVE),
                            and_(
                                Experiment.status == "CANCELLED",
                                exists(
                                    select(EvaluationResult.id).where(
                                        EvaluationResult.experiment_id == Experiment.id,
                                        EvaluationResult.status == "RUNNING",
                                    )
                                ),
                            ),
                        ),
                    )
                    .order_by(Experiment.updated_at, Experiment.id)
                    .limit(limit)
                )
            )

    def _cancelled(self, experiment_id):
        with Session(self.engine) as db:
            row = self._find(db, experiment_id)
            creator = db.get(User, row.created_by)
            return (
                row.status == "CANCELLED"
                or creator is None
                or not creator.enabled
                or creator.role != "tester"
            )

    def _owns_job(self, session):
        return self.attempt_admission is None or self.attempt_admission(session)

    def _admit_attempt(self, session, experiment_id):
        if self.engine.dialect.name != "postgresql":
            return not self._cancelled(experiment_id)
        return bool(
            session.scalar(
                text(
                    "SELECT 1 FROM public.eval_experiments AS e "
                    "JOIN public.users AS u ON u.id = e.created_by "
                    "WHERE e.id = :id AND e.status = 'RUNNING' AND u.enabled AND u.role = 'tester' "
                    "AND e.formal_freeze_sha256 IS NULL AND e.formal_condition_key IS NULL "
                    "FOR SHARE OF e, u"
                ),
                {"id": experiment_id},
            )
        )

    def process(self, experiment_id, runner):
        self._guard_test()
        code_digest = require_current_code()
        with Session(self.engine) as db:
            self._find(db, experiment_id)
        if self.catalog is None:
            raise BusinessError("EVALUATION_RETIRED", "项目用例评测已退役，请查看官方评测。", 410)
        with _lease(self.engine, experiment_id) as held:
            if held:
                self._process(experiment_id, runner, code_digest)

    def _process(self, experiment_id, runner, code_digest):
        with Session(self.engine) as db, db.begin():
            if not self._owns_job(db):
                return
            experiment = self._find(db, experiment_id, lock=True)
            if experiment.config.get("execution_driver", "queued") != self.execution_driver:
                raise BusinessError(
                    "EXECUTION_DRIVER_MISMATCH", "该实验只能由创建时的执行入口运行。"
                )
            if experiment.status not in ACTIVE | {"CANCELLED"}:
                return
            self._start_processing(db, experiment, runner)
            if experiment.status == "CANCELLED":
                return
            config = experiment.config

        frozen = code_digest == config["code_digest"]
        if frozen and "dependency_lock_sha256" in config:
            try:
                frozen = config["dependency_lock_sha256"] == _dependency_lock_sha256()
            except OSError:
                frozen = False
        if frozen:
            try:
                embedding = _embedding_snapshot(runner.settings)
            except BusinessError:
                frozen = False
            else:
                frozen = all(config.get(key) == value for key, value in embedding.items())
        if config["source"] == "live":
            frozen = frozen and _live_model_snapshot(runner.model) == config["model_snapshot"]
        try:
            catalogue = self._catalog()
            cases = {case["id"]: case for case in catalogue.load_cases(config["split"])}
        except FileNotFoundError:
            cases = {}

        if config["concurrency"] == 1:
            while batch := self._claim(experiment_id, config, cases, frozen):
                result_id, case_id, expected, quota = batch[0]
                self._run_one(
                    experiment_id, runner, config, cases[case_id], result_id, expected, quota
                )
            return

        with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
            while batch := self._claim(experiment_id, config, cases, frozen):
                futures = [
                    pool.submit(
                        self._run_one,
                        experiment_id,
                        runner,
                        config,
                        cases[case_id],
                        result_id,
                        expected,
                        quota,
                    )
                    for result_id, case_id, expected, quota in batch
                ]
                for future in futures:
                    future.result()

    def _start_processing(self, db, experiment, runner):
        interrupted = [row for row in self._results(db, experiment.id) if row.status == "RUNNING"]
        starting = experiment.status == "QUEUED"
        if starting or interrupted:
            self._touch(experiment)
        if starting:
            experiment.status = "RUNNING"
            append_progress(db, experiment)
        for row in interrupted:
            row.status, row.failure_reason, row.finished_at = (
                "ERROR",
                "WORKER_INTERRUPTED",
                now(),
            )
            row.actual, row.trace = self._recover_audit(runner, row.id)
            row.version += 1
            append_progress(db, experiment, row)

    def _claim(self, experiment_id, config, cases, frozen):
        cancelled = self._cancelled(experiment_id)
        with Session(self.engine) as db, db.begin():
            if not self._owns_job(db):
                return []
            experiment = self._find(db, experiment_id, lock=True)
            if experiment.status != "RUNNING":
                return []
            batch = []
            changed = []
            for row in self._results(db, experiment.id):
                if row.status != "QUEUED":
                    continue
                case = cases.get(row.case_id)
                reason = None
                if cancelled:
                    reason = "EVALUATION_ACTOR_REVOKED"
                elif not frozen or case is None or digest(case) != row.quota["case_hash"]:
                    reason = "FROZEN_INPUT_CHANGED"
                elif config["source"] == "live" and (
                    row.quota["calls"] <= 0 or Decimal(row.quota["budget_usd"]) <= 0
                ):
                    reason = "LIVE_BUDGET_UNALLOCATED"
                elif config["source"] == "fault" and not any(
                    step["op"] in {"fault", "parallel_execute"} for step in case["events"]
                ):
                    reason = "FAULT_SCRIPT_UNAVAILABLE"
                elif config["source"] != "fault" and any(
                    step["op"] in {"fault", "parallel_execute"} for step in case["events"]
                ):
                    reason = "FAULT_SOURCE_REQUIRED"
                if reason:
                    row.status = "NOT_RUN"
                    row.failure_reason = reason
                else:
                    row.status = "RUNNING"
                    row.started_at = now()
                    batch.append((row.id, row.case_id, row.expected, row.quota))
                row.version += 1
                changed.append(row)
                if len(batch) == config["concurrency"]:
                    break
            if not batch:
                if cancelled:
                    experiment.status = "CANCELLED"
                elif not frozen or not cases:
                    experiment.status = "FAILED"
                else:
                    experiment.status = "COMPLETED"
            self._touch(experiment)
            for row in changed:
                append_progress(db, experiment, row)
            if not batch:
                append_progress(db, experiment)
            return batch

    @staticmethod
    def _recover_audit(runner, result_id):
        try:
            return runner.recover_audit(result_id)
        except SQLAlchemyError:
            log.warning("Evaluation audit is unavailable: %s", result_id)
            return None, []

    def _admit_result_attempt(self, session, experiment_id, result_id):
        return self._admit_attempt(session, experiment_id)

    def _run_one(
        self,
        experiment_id,
        runner,
        config,
        case,
        result_id,
        expected,
        quota,
        *,
        attempt_continuation=None,
    ):
        try:
            outcome = runner.run(
                result_id,
                case,
                expected,
                config,
                quota,
                cancelled=lambda: self._cancelled(experiment_id),
                attempt_admission=lambda session: self._admit_result_attempt(
                    session, experiment_id, result_id
                ),
                queue_admission=self._owns_job if self.attempt_admission is not None else None,
                **(
                    {"attempt_continuation": attempt_continuation}
                    if attempt_continuation is not None
                    else {}
                ),
            )
        except Exception as error:  # noqa: BLE001 - isolate runner failures from other cases
            log.warning("Evaluation attempt failed: %s", result_id)
            actual, trace = self._recover_audit(runner, result_id)
            outcome = {
                "status": "ERROR",
                "failure_reason": error.code
                if isinstance(error, UnsupportedFixture)
                else "EVALUATION_INFRASTRUCTURE_ERROR",
                "actual": actual,
                "trace": trace,
                "checks": [],
            }
        with Session(self.engine) as db, db.begin():
            if not self._owns_job(db):
                return
            experiment = self._find(db, experiment_id, lock=True)
            row = db.get_one(EvaluationResult, result_id)
            if row.experiment_id != experiment.id or row.status != "RUNNING":
                return
            if outcome["actual"] is not None and row.started_at is not None:
                timings = outcome["actual"].setdefault("timings", {})
                timings["queue_ms"] = max(
                    0,
                    round(
                        (utc(row.started_at) - utc(experiment.created_at)).total_seconds() * 1000
                    ),
                )
            for field in ("status", "failure_reason", "actual", "trace", "checks"):
                setattr(row, field, outcome[field])
            row.version += 1
            row.finished_at = now()
            self._touch(experiment)
            append_progress(db, experiment, row)


def _safe_cell(value):
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return f"'{value}"
    return value
