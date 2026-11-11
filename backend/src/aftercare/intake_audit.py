"""Durable one-attempt audit for tool-free intake classification."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, DateTime, String, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from aftercare.business.errors import BusinessError
from aftercare.business.service import _read_key
from aftercare.db import Base, User
from aftercare.intelligence.model_limits import ProviderLimiter
from aftercare.intelligence.models import ModelFailure, finish_transport_evidence


class IntakeModelAudit(Base):
    __tablename__ = "intake_model_audits"

    actor_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(36))
    message_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    usage: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    trace: Mapped[list[dict]] = mapped_column(JSON)
    reserved_cost_usd: Mapped[str | None] = mapped_column(String(30))
    estimated_cost_usd: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _now():
    return datetime.now(UTC)


def _seconds(deadline):
    return (deadline.replace(tzinfo=deadline.tzinfo or UTC) - _now()).total_seconds()


def _advisory_lock(session, actor_id, request_key):
    if session.bind.dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(f"intake:{actor_id}:{request_key}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _authorized(session, actor_id, customer_id):
    actor = session.get(User, actor_id, with_for_update={"read": True})
    if (
        actor is None
        or not actor.enabled
        or actor.role != "customer"
        or actor.customer_id != customer_id
    ):
        raise ModelFailure("INTAKE_ACTOR_REVOKED")


class IntakeAuditFactory:
    def __init__(self, engine, *, provider_limiter=None):
        self.engine = engine
        self.provider_limiter = (
            provider_limiter if provider_limiter is not None else ProviderLimiter(engine)
        )

    def __call__(self, actor_id, customer_id, request_key, message_digest):
        _read_key(request_key)
        identity = actor_id, request_key
        with Session(self.engine) as db, db.begin():
            _advisory_lock(db, actor_id, request_key)
            _authorized(db, actor_id, customer_id)
            row = db.get(IntakeModelAudit, identity, with_for_update=True)
            if row is None:
                at = _now()
                db.add(
                    IntakeModelAudit(
                        actor_id=actor_id,
                        customer_id=customer_id,
                        request_key=request_key,
                        message_digest=message_digest,
                        status="PENDING",
                        created_at=at,
                        deadline=at + timedelta(seconds=15),
                        metadata_json={},
                        trace=[],
                    )
                )
            elif row.message_digest != message_digest or row.customer_id != customer_id:
                raise BusinessError("IDEMPOTENCY_CONFLICT", "同一分类请求键已用于不同文字或账户。")
        return _IntakeAudit(self.engine, self.provider_limiter, identity)


class _IntakeAudit:
    def __init__(self, engine, limiter, identity):
        self.engine, self.limiter, self.identity = engine, limiter, identity

    def _row(self, db, *, lock=False, authorize=True):
        row = db.get_one(IntakeModelAudit, self.identity, with_for_update=lock)
        if authorize:
            _authorized(db, row.actor_id, row.customer_id)
        return row

    def cooldown_aliases(self, _seconds):
        # The shared provider limiter enforces account-level cooldown before admission.
        return set()

    def replay(self, _logical_id):
        with Session(self.engine) as db, db.begin():
            row = self._row(db, lock=True)
            if row.status == "SUCCEEDED":
                return row.result
            if row.status == "PENDING":
                return None
            if row.status == "STARTED" and _seconds(row.deadline) <= 0:
                row.status, row.error_code, row.finished_at = (
                    "UNKNOWN",
                    "INTAKE_INTERRUPTED",
                    _now(),
                )
            reason = row.error_code or "INTAKE_ALREADY_RUNNING"
        raise ModelFailure(reason)

    def budget(self, registry):
        with Session(self.engine) as db:
            row = self._row(db)
            return {
                "attempts_used": int(row.started_at is not None),
                "attempts_limit": 1,
                "run_cost_limit_usd": str(registry.max_run_usd),
                "remaining_seconds": max(0, _seconds(row.deadline)),
            }

    def event(self, kind, payload):
        with Session(self.engine) as db, db.begin():
            row = self._row(db, lock=True)
            if len(row.trace) < 12:
                row.trace = [*row.trace, {"kind": kind, "payload": payload}]

    def claim_provider(self, spec, registry):
        return self.limiter.claim(
            registry.provider_limits[spec.provider],
            registry.deadline_seconds + 2 * registry.timeout_seconds + 5,
        )

    def release_provider(self, slot, *, attempted, uncertain=False, cooldown_seconds=0):
        self.limiter.release(
            slot,
            attempted=attempted,
            uncertain=uncertain,
            cooldown_seconds=cooldown_seconds,
        )

    def begin_attempt(
        self,
        logical_id,
        ordinal,
        spec,
        reserve,
        registry,
        *,
        transport_evidence=None,
        base_registry_digest=None,
    ):
        with Session(self.engine) as db, db.begin():
            row = self._row(db, lock=True)
            bounded_deadline = min(
                row.deadline,
                row.created_at + timedelta(seconds=registry.deadline_seconds),
            )
            remaining = _seconds(bounded_deadline)
            if row.status != "PENDING":
                raise ModelFailure("INTAKE_ALREADY_STARTED")
            if remaining <= 0 or logical_id != "intake:classify" or ordinal != 1:
                raise ModelFailure("INTAKE_BUDGET_EXHAUSTED")
            if reserve is None or reserve > min(registry.max_usd, registry.max_run_usd):
                raise ModelFailure("MODEL_COST_BUDGET_EXHAUSTED")
            row.status, row.started_at, row.deadline = "STARTED", _now(), bounded_deadline
            row.metadata_json = {
                **(
                    {"base_registry_digest": base_registry_digest}
                    if base_registry_digest is not None
                    else {}
                ),
                **(
                    {"transport_evidence": transport_evidence}
                    if transport_evidence is not None
                    else {}
                ),
                "logical_id": logical_id,
                "ordinal": ordinal,
                "alias": spec.alias,
                "provider": spec.provider,
                "model": spec.model,
                "registry_version": registry.version,
                "price_date": spec.price_date.isoformat() if spec.price_date else None,
                "cache_write_ttl": spec.cache_write_ttl,
                "pricing_usd_per_million": {
                    name: str(value) if value is not None else None
                    for name, value in (
                        ("input", spec.input_usd_per_million),
                        ("output", spec.output_usd_per_million),
                        ("cache_read", spec.cache_read_usd_per_million),
                        ("cache_write", spec.cache_write_usd_per_million),
                    )
                },
            }
            row.reserved_cost_usd = str(reserve)
            return remaining

    def finish_attempt(
        self, _logical_id, _ordinal, status, code, usage, price, result, *, transport_evidence=None
    ):
        with Session(self.engine) as db, db.begin():
            row = self._row(db, lock=True, authorize=False)
            if row.status not in {"STARTED", "UNKNOWN"}:
                return
            # A complete response that arrives after a poll marked it unknown is retained.
            # No poll may start a second request while waiting for reconciliation.
            row.status, row.error_code, row.usage, row.result = status, code, usage, result
            row.metadata_json = finish_transport_evidence(row.metadata_json, transport_evidence)
            row.finished_at = _now()
            row.estimated_cost_usd = str(price) if price is not None else None

    def confirm_admission(self):
        with Session(self.engine) as db:
            row = self._row(db)
            if row.finished_at is not None and row.finished_at > row.deadline:
                raise ModelFailure("INTAKE_DEADLINE_EXCEEDED")

    def confirm_inflight_admission(self):
        self.confirm_admission()
