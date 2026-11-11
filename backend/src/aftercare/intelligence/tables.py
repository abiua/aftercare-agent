"""Persistent run ledger, model call audit, and resumable event cursor."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index(
            "agent_runs_active_case",
            "case_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING','WAITING_INPUT')"),
            sqlite_where=text("status IN ('QUEUED','RUNNING','WAITING_INPUT')"),
        ),
        Index("agent_runs_case_history", "case_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(100), unique=True)
    case_id: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[str] = mapped_column(String(80))
    actor: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24))
    mode: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text)
    case_version: Mapped[int] = mapped_column(Integer)
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(80))
    output: Mapped[dict | None] = mapped_column(JSON)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "agent_run_events"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunMutation(Base):
    __tablename__ = "agent_run_mutations"

    case_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))


class ModelAttempt(Base):
    __tablename__ = "agent_model_attempts"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    cycle: Mapped[int] = mapped_column(Integer, primary_key=True)
    logical_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    usage: Mapped[dict | None] = mapped_column(JSON)
    estimated_cost_usd: Mapped[str | None] = mapped_column(String(30))
    error_code: Mapped[str | None] = mapped_column(String(64))
