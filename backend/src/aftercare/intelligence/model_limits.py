"""Atomic provider-scope request and in-flight limits backed by PostgreSQL."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, delete, func, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from aftercare.db import Base


class ProviderRateScope(Base):
    __tablename__ = "provider_rate_scopes"
    scope: Mapped[str] = mapped_column(String(96), primary_key=True)
    max_in_flight: Mapped[int] = mapped_column(Integer)
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderRateLease(Base):
    __tablename__ = "provider_rate_leases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(96), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class Slot:
    id: str
    scope: str


class ProviderLimitError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _clock(session):
    if session.bind.dialect.name == "postgresql":
        return session.scalar(select(func.clock_timestamp()))
    return datetime.now(UTC)


class ProviderLimiter:
    def __init__(self, engine):
        # Evaluation schemas may remap unqualified tables. Provider rows are shared.
        self.engine = (
            engine.execution_options(schema_translate_map={None: "public"})
            if engine.dialect.name == "postgresql"
            else engine
        )

    @staticmethod
    def _lock(session, scope):
        if session.bind.dialect.name == "postgresql":
            key = int.from_bytes(
                sha256(f"provider:{scope}".encode()).digest()[:8], "big", signed=True
            )
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def claim(self, limit, lease_seconds):
        with Session(self.engine) as db, db.begin():
            self._lock(db, limit.scope)
            state = db.get(ProviderRateScope, limit.scope)
            if state is None:
                state = ProviderRateScope(
                    scope=limit.scope,
                    max_in_flight=limit.max_in_flight,
                    requests_per_minute=limit.requests_per_minute,
                )
                db.add(state)
            if (state.max_in_flight, state.requests_per_minute) != (
                limit.max_in_flight,
                limit.requests_per_minute,
            ):
                raise ProviderLimitError("PROVIDER_LIMIT_CONFIG_MISMATCH")
            now = _clock(db)
            if state.cooldown_until and state.cooldown_until.replace(tzinfo=UTC) > now:
                raise ProviderLimitError("PROVIDER_COOLDOWN")
            cutoff = now - timedelta(minutes=1)
            filters = ProviderRateLease.scope == limit.scope
            db.execute(
                delete(ProviderRateLease).where(
                    filters,
                    ProviderRateLease.started_at <= cutoff,
                    ProviderRateLease.active_until <= now,
                )
            )
            in_flight = db.scalar(
                select(func.count())
                .select_from(ProviderRateLease)
                .where(
                    filters,
                    ProviderRateLease.active_until > now,
                )
            )
            if in_flight >= limit.max_in_flight:
                raise ProviderLimitError("PROVIDER_IN_FLIGHT_LIMIT")
            recent = db.scalar(
                select(func.count())
                .select_from(ProviderRateLease)
                .where(
                    filters,
                    ProviderRateLease.started_at > cutoff,
                )
            )
            if recent >= limit.requests_per_minute:
                raise ProviderLimitError("PROVIDER_REQUEST_RATE_LIMIT")
            slot = Slot(str(uuid4()), limit.scope)
            db.add(
                ProviderRateLease(
                    id=slot.id,
                    scope=slot.scope,
                    started_at=now,
                    active_until=now + timedelta(seconds=lease_seconds),
                )
            )
            return slot

    def release(self, slot, *, attempted: bool, uncertain: bool, cooldown_seconds: float):
        with Session(self.engine) as db, db.begin():
            self._lock(db, slot.scope)
            now = _clock(db)
            lease = db.get(ProviderRateLease, slot.id)
            if lease is not None:
                if not attempted:
                    db.delete(lease)
                elif not uncertain:
                    lease.active_until = now
            if cooldown_seconds > 0:
                state = db.get(ProviderRateScope, slot.scope)
                until = now + timedelta(seconds=cooldown_seconds)
                if state.cooldown_until is None or state.cooldown_until.replace(tzinfo=UTC) < until:
                    state.cooldown_until = until
