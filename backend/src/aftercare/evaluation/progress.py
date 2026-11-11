"""Append and read a minimal, durable execution journal for evaluation experiments."""

import re

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import now, utc

from .tables import EvaluationProgress, EvaluationResult, Experiment, ordinary_experiment

PAGE_SIZE = 100
TERMINAL = {"CANCELLED", "COMPLETED", "FAILED"}


def append_progress(db, experiment, result=None):
    """Call inside the state transaction, with the experiment row locked or newly inserted."""
    if experiment.progress_seq is None:
        return
    payload = {
        "kind": "EXPERIMENT_STATUS" if result is None else "RESULT_STATUS",
        "experiment_version": experiment.version,
        "status": experiment.status if result is None else result.status,
    }
    if result is not None:
        payload.update(
            result_id=result.id,
            result_version=result.version,
            case_id=result.case_id,
            repetition=result.repetition,
        )
        if result.failure_reason and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", result.failure_reason):
            payload["reason"] = result.failure_reason
    experiment.progress_seq += 1
    db.add(
        EvaluationProgress(
            experiment_id=experiment.id,
            sequence=experiment.progress_seq,
            payload=payload,
            created_at=now(),
        )
    )


def read_progress(engine, experiment_id, after, limit):
    # Read the high-water mark, settled status and page from the same SQL snapshot.
    unsettled = exists(
        select(EvaluationResult.id).where(
            EvaluationResult.experiment_id == Experiment.id,
            EvaluationResult.status.in_(("QUEUED", "RUNNING")),
        )
    ).correlate(Experiment)
    query = (
        select(Experiment.status, Experiment.progress_seq, unsettled, EvaluationProgress)
        .outerjoin(
            EvaluationProgress,
            and_(
                EvaluationProgress.experiment_id == Experiment.id,
                EvaluationProgress.sequence > after,
            ),
        )
        .where(Experiment.id == experiment_id, ordinary_experiment())
        .order_by(EvaluationProgress.sequence)
        .limit(limit)
    )
    with Session(engine) as db:
        rows = db.execute(query).all()
        if not rows:
            raise BusinessError("EXPERIMENT_NOT_FOUND", "评测实验不存在。", 404)
        status, latest, active_results, _ = rows[0]
        if after > (latest or 0):
            raise BusinessError("FUTURE_PROGRESS_CURSOR", "游标超过当前实验的进度记录。", 400)
        events = [event for _, _, _, event in rows if event is not None]
        remaining = (latest or 0) - after
        if len(events) != min(limit, remaining) or any(
            event.sequence != sequence for sequence, event in enumerate(events, after + 1)
        ):
            raise BusinessError("PROGRESS_HISTORY_INCOMPLETE", "实验进度记录不完整。", 409)
        return {
            "experiment_id": experiment_id,
            "history": "HISTORICAL_SNAPSHOT" if latest is None else "RECORDED",
            "status": status,
            "terminal": status in TERMINAL and not active_results,
            "latest_sequence": latest or 0,
            "events": [
                {
                    "experiment_id": experiment_id,
                    "sequence": event.sequence,
                    "created_at": utc(event.created_at).isoformat(),
                    **event.payload,
                }
                for event in events
            ],
        }
