from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class DecisionHead(Base):
    __tablename__ = "chat_decision_heads"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    issue_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_id: Mapped[str | None] = mapped_column(String(36))
    offer_id: Mapped[str | None] = mapped_column(String(36))
    review_id: Mapped[str | None] = mapped_column(String(36))


class DecisionSnapshot(Base):
    __tablename__ = "chat_decision_snapshots"
    __table_args__ = (
        UniqueConstraint("conversation_id", "issue_id", "version"),
        UniqueConstraint("conversation_id", "issue_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36))
    issue_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("chat_decision_snapshots.id"))
    run_id: Mapped[str | None] = mapped_column(String(80))
    origin: Mapped[str] = mapped_column(String(24))
    actor_id: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict] = mapped_column(JSON)
    candidate: Mapped[dict] = mapped_column(JSON)
    checks: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SupportDecision(Base):
    __tablename__ = "chat_support_decisions"
    __table_args__ = (UniqueConstraint("conversation_id", "issue_id", "actor_id", "request_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36))
    issue_id: Mapped[str] = mapped_column(String(36))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey(DecisionSnapshot.id))
    resulting_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey(DecisionSnapshot.id))
    actor_id: Mapped[str] = mapped_column(String(128))
    request_key: Mapped[str] = mapped_column(String(80))
    request_sha256: Mapped[str] = mapped_column(String(64))
    review: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Offer(Base):
    __tablename__ = "chat_decision_offers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey(DecisionSnapshot.id))
    support_decision_id: Mapped[str] = mapped_column(ForeignKey(SupportDecision.id), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    option: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CustomerDecision(Base):
    __tablename__ = "chat_customer_decisions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "issue_id", "actor_id", "request_key"),
        UniqueConstraint("offer_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36))
    issue_id: Mapped[str] = mapped_column(String(36))
    offer_id: Mapped[str] = mapped_column(ForeignKey(Offer.id))
    version: Mapped[int] = mapped_column(Integer, default=1)
    actor_id: Mapped[str] = mapped_column(String(128))
    request_key: Mapped[str] = mapped_column(String(80))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
