"""Read accounting differences without altering frozen evaluation results."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.business.rules import utc
from aftercare.intelligence.tables import ModelAttempt

from .runner import _namespace

IDENTITY = ("run_id", "cycle", "logical_id", "ordinal")
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
USAGE_DETAILS = {
    "input_token_details": ("cache_read", "cache_creation", "audio"),
    "output_token_details": ("reasoning", "audio"),
}


def read_attempts(engine, result_id):
    if engine.dialect.name != "postgresql":
        return None, "AUDIT_STORAGE_UNSUPPORTED"
    try:
        namespace = _namespace(result_id)
    except ValueError:
        return None, "SNAPSHOT_FORMAT_UNSUPPORTED"
    try:
        inspector = inspect(engine)
        if not inspector.has_schema(namespace):
            return None, "SCHEMA_NOT_FOUND"
        if not inspector.has_table(ModelAttempt.__tablename__, schema=namespace):
            return None, "AUDIT_UNREADABLE"
        isolated = engine.execution_options(schema_translate_map={None: namespace})
        with Session(isolated) as db:
            rows = db.execute(
                select(
                    ModelAttempt.run_id,
                    ModelAttempt.cycle,
                    ModelAttempt.logical_id,
                    ModelAttempt.ordinal,
                    ModelAttempt.status,
                    ModelAttempt.usage,
                    ModelAttempt.estimated_cost_usd,
                    ModelAttempt.finished_at,
                ).order_by(
                    ModelAttempt.run_id,
                    ModelAttempt.cycle,
                    ModelAttempt.logical_id,
                    ModelAttempt.ordinal,
                )
            ).mappings()
            return [dict(row) for row in rows], None
    except SQLAlchemyError:
        return None, "AUDIT_UNREADABLE"


def _identity(attempt):
    if not isinstance(attempt, dict):
        return None
    run_id, cycle, logical_id, ordinal = (attempt.get(key) for key in IDENTITY)
    if (
        isinstance(run_id, str)
        and run_id
        and isinstance(logical_id, str)
        and logical_id
        and type(cycle) is int
        and cycle >= 0
        and type(ordinal) is int
        and ordinal > 0
    ):
        return run_id, cycle, logical_id, ordinal
    return None


def _counters(value, fields):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields if type(value.get(key)) is int and value[key] >= 0}


def _usage(value):
    if not isinstance(value, dict):
        return None
    usage = _counters(value, USAGE_FIELDS)
    for field, keys in USAGE_DETAILS.items():
        counters = _counters(value.get(field), keys)
        if counters:
            usage[field] = counters
    return usage or None


def _cost(value):
    if not isinstance(value, str) or len(value) > 30:
        return None
    try:
        cost = Decimal(value)
    except InvalidOperation:
        return None
    return value if cost.is_finite() and cost >= 0 else None


def changes(raw_attempts, snapshot_attempts):
    previous = defaultdict(list)
    for attempt in snapshot_attempts:
        previous[_identity(attempt)].append(attempt)
    raw_counts = Counter(_identity(attempt) for attempt in raw_attempts)
    unmatched_snapshot = sum(
        len(matches)
        for key, matches in previous.items()
        if key is None or raw_counts[key] != 1 or len(matches) != 1
    )

    differences = []
    unmatched = 0
    for raw in raw_attempts:
        key = _identity(raw)
        matches = previous.get(key, [])
        if key is None or raw_counts[key] != 1 or len(matches) != 1:
            unmatched += 1
            continue
        frozen = matches[0]
        usage, snapshot_usage = _usage(raw.get("usage")), _usage(frozen.get("usage"))
        cost = _cost(raw.get("estimated_cost_usd"))
        snapshot_cost = _cost(frozen.get("estimated_cost_usd"))
        snapshot_status = frozen.get("status")
        # Recovery can project a raw STARTED attempt as UNKNOWN without changing its ledger row.
        persisted_status = frozen.get("persisted_status", snapshot_status)
        if (raw["status"], usage, cost) == (persisted_status, snapshot_usage, snapshot_cost):
            continue
        differences.append(
            {
                **dict(zip(IDENTITY, key, strict=True)),
                "status": raw["status"],
                "usage": usage,
                "estimated_cost_usd": cost,
                "finished_at": utc(raw["finished_at"]).isoformat() if raw["finished_at"] else None,
                "snapshot_status": snapshot_status,
                "snapshot_usage": snapshot_usage,
                "snapshot_estimated_cost_usd": snapshot_cost,
            }
        )
    return differences, unmatched, unmatched_snapshot
