"""Recover decision candidates from the persisted, checked Agent run outbox."""

from hashlib import sha256

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.db import User
from aftercare.intelligence.decision_candidate import DECISION_READY_EVENT, project_candidate
from aftercare.intelligence.tables import AgentRun, RunEvent

from .decisions import ConversationDecisions
from .failure_delivery import FAILURES, notify
from .tables import DecisionProjection, InvestigationRequest


def pending(engine, *, limit=50):
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AgentRun.id)
                .where(
                    or_(
                        select(RunEvent.run_id).where(RunEvent.run_id == AgentRun.id, RunEvent.kind == DECISION_READY_EVENT).exists(),
                        (AgentRun.reason.in_(FAILURES) & AgentRun.status.in_(['FAILED', 'NEEDS_REVIEW']) &
                         select(InvestigationRequest.run_id).where(InvestigationRequest.run_id == AgentRun.id).exists()),
                    ),
                    ~select(DecisionProjection.run_id)
                    .where(DecisionProjection.run_id == AgentRun.id)
                    .exists(),
                )
                .order_by(AgentRun.created_at, AgentRun.id)
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
        run = db.get_one(AgentRun, run_id)
        delivered_failure = notify(business, db, run)
        if ready is None:
            if run.reason in FAILURES and run.status in {'FAILED', 'NEEDS_REVIEW'}:
                db.add(DecisionProjection(run_id=run_id, snapshot_id=None, state='BLOCKED',
                                          reason=run.reason if delivered_failure else 'FAILURE_NOTICE_STALE_OR_REVOKED',
                                          created_at=business.clock()))
            return
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
