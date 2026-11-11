"""Versioned evaluation plans, attempts, and append-only review records."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class Experiment(Base):
    __tablename__ = "eval_experiments"
    __table_args__ = (
        CheckConstraint(
            "(formal_freeze_sha256 IS NULL) = (formal_condition_key IS NULL)",
            name="ck_eval_experiments_formal_pair",
        ),
        UniqueConstraint(
            "formal_freeze_sha256",
            "formal_condition_key",
            name="uq_eval_experiments_formal_condition",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    parent_experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("eval_experiments.id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    progress_seq: Mapped[int | None] = mapped_column(Integer)
    formal_freeze_sha256: Mapped[str | None] = mapped_column(String(64))
    formal_condition_key: Mapped[str | None] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ordinary_experiment():
    return and_(
        Experiment.formal_freeze_sha256.is_(None), Experiment.formal_condition_key.is_(None)
    )


class EvaluationProgress(Base):
    __tablename__ = "eval_progress_events"

    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(Experiment.id, ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationResult(Base):
    __tablename__ = "eval_results"
    __table_args__ = (UniqueConstraint("experiment_id", "case_id", "repetition"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey(Experiment.id), index=True)
    case_id: Mapped[str] = mapped_column(String(64))
    retry_of_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("eval_results.id"), index=True
    )
    repetition: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    intent: Mapped[str | None] = mapped_column(String(32))
    expected: Mapped[dict] = mapped_column(JSON)
    actual: Mapped[dict | None] = mapped_column(JSON)
    checks: Mapped[list] = mapped_column(JSON)
    trace: Mapped[list] = mapped_column(JSON)
    quota: Mapped[dict] = mapped_column(JSON)
    failure_reason: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationReview(Base):
    __tablename__ = "eval_reviews"
    __table_args__ = (UniqueConstraint("result_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_id: Mapped[str] = mapped_column(ForeignKey(EvaluationResult.id), index=True)
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text)
    annotations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationMutation(Base):
    __tablename__ = "eval_mutations"

    actor_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(36))


class EvaluationImport(Base):
    __tablename__ = "eval_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    origin: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
