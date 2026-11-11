"""Read immutable native decision records for a predeclared agreement comparison."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.rules import digest
from aftercare.decisions.contracts import DecisionRef
from aftercare.decisions.tables import DecisionSnapshot, SupportDecision

from .scoring import PlannedRow
from .support_agreement import (
    AgreementInput,
    FrozenSnapshot,
    FrozenSupportReview,
    native_review_request,
    summarize_support_agreement,
)


@dataclass(frozen=True)
class ExportTarget:
    row: PlannedRow
    snapshot: DecisionRef
    option_id: str
    cutoff: datetime


def _utc(value):
    # The isolated SQLite test backend stores application UTC as naive datetime.
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


def _snapshot(row):
    return FrozenSnapshot(
        ref=DecisionRef(id=row.id, version=row.version, sha256=row.sha256),
        conversation_id=row.conversation_id,
        issue_id=row.issue_id,
        parent_id=row.parent_id,
        run_id=row.run_id,
        origin=row.origin,
        actor_id=row.actor_id,
        input=row.input,
        candidate=row.candidate,
        checks=row.checks,
        expires_at=_utc(row.expires_at),
    )


def _previous_id(db, snapshot, record, review):
    matching_scope = (
        SupportDecision.conversation_id == record.conversation_id,
        SupportDecision.issue_id == record.issue_id,
    )
    earlier = db.scalars(
        select(SupportDecision.id).where(
            *matching_scope,
            SupportDecision.id != record.id,
            SupportDecision.resulting_snapshot_id == snapshot.ref.id,
            SupportDecision.created_at <= record.created_at,
        )
    )
    ref = snapshot.ref.model_dump(mode="json")
    payload = review.model_dump(mode="json")
    matches = [
        ident
        for ident in (None, *earlier)
        if digest([ref, payload, ident]) == record.request_sha256
    ]
    if len(matches) != 1:
        raise ValueError("SUPPORT_PREVIOUS_REVIEW_UNVERIFIED")
    return matches[0]


def _extract(db, target):
    base = AgreementInput(target.row, target.option_id, target.snapshot, None, None)
    source = db.get(DecisionSnapshot, target.snapshot.id)
    if source is None or _utc(source.created_at) > target.cutoff:
        return base, "NO_CANDIDATE_BY_CUTOFF"
    try:
        snapshot = _snapshot(source)
        if snapshot.ref != target.snapshot or snapshot.origin != "AGENT":
            return base, "CANDIDATE_REFERENCE_MISMATCH"
        snapshot.verify()
    except (ValueError, TypeError):
        return base, "INVALID_CANDIDATE_SNAPSHOT"

    empty = AgreementInput(target.row, target.option_id, target.snapshot, None, None, snapshot)
    records = list(
        db.scalars(
            select(SupportDecision)
            .where(
                SupportDecision.snapshot_id == source.id,
                SupportDecision.conversation_id == source.conversation_id,
                SupportDecision.issue_id == source.issue_id,
                SupportDecision.created_at <= _utc(target.cutoff),
            )
            .order_by(SupportDecision.created_at, SupportDecision.id)
            .limit(2)
        )
    )
    if not records:
        return empty, "NO_SUPPORT_REVIEW_BY_CUTOFF"
    if len(records) == 2 and records[0].created_at == records[1].created_at:
        return empty, "FIRST_SUPPORT_REVIEW_AMBIGUOUS"
    record = records[0]
    if _utc(record.created_at) < _utc(source.created_at):
        return empty, "SUPPORT_REVIEW_PRECEDES_CANDIDATE"
    result_row = db.get(DecisionSnapshot, record.resulting_snapshot_id)
    if result_row is None or _utc(result_row.created_at) > _utc(record.created_at):
        return empty, "RESULT_SNAPSHOT_UNAVAILABLE"
    try:
        result = _snapshot(result_row)
        result.verify()
        review = native_review_request(record.review)
        previous = _previous_id(db, snapshot, record, review)
    except (ValueError, TypeError, AttributeError):
        return empty, "SUPPORT_RECORD_BINDING_UNVERIFIED"
    return AgreementInput(
        target.row,
        target.option_id,
        target.snapshot,
        record.id,
        result.ref,
        snapshot,
        FrozenSupportReview(
            id=record.id,
            conversation_id=record.conversation_id,
            issue_id=record.issue_id,
            snapshot_id=record.snapshot_id,
            resulting_snapshot_id=record.resulting_snapshot_id,
            actor_id=record.actor_id,
            request_sha256=record.request_sha256,
            review=record.review,
            expected_previous_review_id=previous,
        ),
        result,
    ), None


def export_support_agreement(engine, targets: tuple[ExportTarget, ...]):
    """Extract and summarize without returning private review text or mutable business facts."""
    if not targets or len({target.row for target in targets}) != len(targets):
        raise ValueError("A unique nonempty export plan is required")
    if any(target.cutoff.utcoffset() is None or not target.option_id for target in targets):
        raise ValueError("An aware cutoff and predeclared option ID are required")
    backend = engine.dialect.name
    if backend == "postgresql":
        engine = engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
    elif backend != "sqlite":
        raise ValueError("Unsupported decision export backend")
    with engine.connect() as connection:
        if backend == "sqlite":
            connection.exec_driver_sql("BEGIN")
        with Session(connection) as db:
            extracted = [_extract(db, target) for target in targets]
    inputs = tuple(replace(item, extraction_exclusion=exclusion) for item, exclusion in extracted)
    report = summarize_support_agreement(
        inputs, planned_rows=tuple(target.row for target in targets)
    )
    report["evidence_basis"] = "CANDIDATE_EXPOSED_SUPPORT_DECISION"
    report["extraction_plan_sha256"] = digest(
        [
            [
                target.row.case_id,
                target.row.repetition,
                target.row.arm,
                target.snapshot.model_dump(mode="json"),
                target.option_id,
                _utc(target.cutoff).isoformat(),
            ]
            for target in targets
        ]
    )
    report["extraction"] = tuple(
        {
            "row": item.row,
            "review_id": item.expected_review_id,
            "result": item.expected_result,
            "exclusion": exclusion,
        }
        for item, exclusion in extracted
    )
    return report
