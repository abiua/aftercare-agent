"""Durable customer conversations and staged original photographs."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class Conversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("business_customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    active_case_id: Mapped[str | None] = mapped_column(ForeignKey("business_cases.id"))
    active_order_id: Mapped[str | None] = mapped_column(ForeignKey("business_orders.id"))
    line_ids: Mapped[list] = mapped_column(JSON)
    input_floor: Mapped[int] = mapped_column(Integer)
    require_new_case: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Message(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence"),
        UniqueConstraint("conversation_id", "client_message_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey(Conversation.id))
    client_message_id: Mapped[str | None] = mapped_column(String(36))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    sequence: Mapped[int | None] = mapped_column(Integer)
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    cards: Mapped[list] = mapped_column(JSON)
    reply_to_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Attachment(Base):
    __tablename__ = "chat_attachments"
    __table_args__ = (UniqueConstraint("message_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey(Message.id))
    position: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))


class CaseLink(Base):
    __tablename__ = "chat_case_links"
    __table_args__ = (UniqueConstraint("conversation_id", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey(Conversation.id))
    case_id: Mapped[str] = mapped_column(ForeignKey("business_cases.id"))
    source_message_id: Mapped[str] = mapped_column(ForeignKey(Message.id))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
