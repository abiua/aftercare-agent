"""Isolated P5 blind-review records. Ordinary evaluation never queries these tables."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class BlindTask(Base):
    __tablename__ = "eval_chat_blind_tasks"
    __table_args__ = (UniqueConstraint("reviewer_id", "input_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    case_label: Mapped[str] = mapped_column(String(80))
    input_sha256: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict] = mapped_column(JSON)
    image_objects: Mapped[dict] = mapped_column(JSON)
    candidates: Mapped[list] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BlindInitial(Base):
    __tablename__ = "eval_chat_blind_initials"

    task_id: Mapped[str] = mapped_column(String(36), ForeignKey(BlindTask.id), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BlindExposure(Base):
    __tablename__ = "eval_chat_blind_exposures"

    task_id: Mapped[str] = mapped_column(String(36), ForeignKey(BlindTask.id), primary_key=True)
    initial_sha256: Mapped[str] = mapped_column(String(64))
    candidates_sha256: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BlindLabels(Base):
    __tablename__ = "eval_chat_blind_labels"

    task_id: Mapped[str] = mapped_column(String(36), ForeignKey(BlindTask.id), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    exposure_sha256: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
