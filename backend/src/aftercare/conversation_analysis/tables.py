"""Durable records for conversation analysis, separate from business facts."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class ChatAnalysisTurn(Base):
    __tablename__ = "chat_analysis_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "generation"),
        UniqueConstraint("conversation_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_conversations.id"))
    customer_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    input_from_seq: Mapped[int] = mapped_column(Integer)
    input_through_seq: Mapped[int] = mapped_column(Integer)
    scope_sha256: Mapped[str] = mapped_column(String(64))
    attachment_sha256: Mapped[str] = mapped_column(String(64))
    attachments: Mapped[list] = mapped_column(JSON)
    vision_input: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), index=True)
    reason: Mapped[str | None] = mapped_column(String(64))
    quiet_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    wait_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatModelAttempt(Base):
    __tablename__ = "chat_model_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(36), ForeignKey(ChatAnalysisTurn.id), unique=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_conversations.id"), index=True
    )
    state: Mapped[str] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(64))
    reserved_microusd: Mapped[int] = mapped_column(BigInteger)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    budget_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[dict | None] = mapped_column(JSON)
    model_evidence: Mapped[dict] = mapped_column(JSON)
    response_evidence: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Observation(Base):
    __tablename__ = "chat_observations"
    __table_args__ = (UniqueConstraint("turn_id", "attachment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(36), ForeignKey(ChatAnalysisTurn.id))
    attempt_id: Mapped[str] = mapped_column(String(36), ForeignKey(ChatModelAttempt.id))
    attachment_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_attachments.id"))
    original_sha256: Mapped[str] = mapped_column(String(64))
    sent_sha256: Mapped[str] = mapped_column(String(64))
    transform_version: Mapped[str] = mapped_column(String(64))
    certainty: Mapped[str] = mapped_column(String(24))
    candidate_kind: Mapped[str] = mapped_column(String(24))
    findings: Mapped[list] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
