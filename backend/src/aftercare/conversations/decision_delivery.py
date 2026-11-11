"""Recover decision candidates from the persisted, checked Agent run outbox."""

from hashlib import sha256

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.db import User
from aftercare.intelligence.decision_candidate import DECISION_READY_EVENT, project_candidate
from aftercare.intelligence.tables import AgentRun, RunEvent

from .decisions import ConversationDecisions
from .tables import DecisionProjection


def pending(engine, *, limit=50):
    with Session(engine) as db:
        return list(
            db.scalars(
                select(RunEvent.run_id)
                .where(
                    RunEvent.kind == DECISION_READY_EVENT,
                    ~select(DecisionProjection.run_id)
                    .where(DecisionProjection.run_id == RunEvent.run_id)
                    .exists(),
                )
                .order_by(RunEvent.created_at, RunEvent.run_id)
                .limit(limit)
            )
        )


def project(business, run_id):
    with Session(business.engine) as db, db.begin():
        if business.engine.dialect.name == "postgresql":
            lock = int.from_bytes(
                sha256(f"decision-projection:{run_id}".encode()).digest()[:8], "big", signed=True
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
        if db.get(DecisionProjection, run_id) is not None:
            return
        ready = db.scalar(
            select(RunEvent.run_id).where(
                RunEvent.run_id == run_id,
                RunEvent.kind == DECISION_READY_EVENT,
            )
        )
        if ready is None:
            return
        run = db.get_one(AgentRun, run_id)
        user = db.get(User, run.owner_id)
        snapshot = None
        reason = None
        if user is None or not user.enabled or user.role not in {"customer", "support"}:
            reason = "DECISION_RUN_ACTOR_REVOKED"
        else:
            actor = UserContext(user_id=user.id, role=user.role, customer_id=user.customer_id)
            try:
                with db.begin_nested():
                    snapshot = project_candidate(
                        db, ConversationDecisions(business).records, actor, run_id
                    )
            except BusinessError as error:
                reason = error.code
        db.add(
            DecisionProjection(
                run_id=run_id,
                snapshot_id=snapshot.id if snapshot else None,
                state="PUBLISHED" if snapshot else "BLOCKED",
                reason=reason,
                created_at=business.clock(),
            )
        )
