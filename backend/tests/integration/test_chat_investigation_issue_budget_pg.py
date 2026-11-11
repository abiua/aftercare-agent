"""Actual shared P2/P4 admission through a disposable PostgreSQL server."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import test_chat_analysis_pg as p2_fixture
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_chat_analysis_pg import CHAT, LOGICAL, OWNER, _claim_bound

from aftercare.business.errors import BusinessError
from aftercare.conversation_analysis.tables import ChatModelAttempt
from aftercare.conversations.tables import Conversation
from aftercare.db import Base
from aftercare.intelligence.models import ModelFailure, Registry
from aftercare.intelligence.service import RunAudit
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from aftercare.investigation.issue_budget import (
    IssueBudget,
    IssueBudgetDenied,
    admit_issue,
    to_microusd,
)

postgres = p2_fixture.postgres
state = p2_fixture.state
pytestmark = p2_fixture.pytestmark


def test_image_and_investigation_admission_share_atomic_budget(state):
    Base.metadata.create_all(
        state.engine, tables=[AgentRun.__table__, RunEvent.__table__, ModelAttempt.__table__]
    )
    lease, _ = _claim_bound(state)
    stamp = datetime.now(UTC)
    issue, run_id = str(uuid4()), str(uuid4())
    budget = IssueBudget(calls=8, microusd=100)
    state.service.issue_budget = budget
    with Session(state.engine, expire_on_commit=False) as db, db.begin():
        db.get_one(Conversation, CHAT).analysis_issue_id = issue
        run = AgentRun(
            id=run_id,
            thread_id=run_id,
            case_id="fixture",
            owner_id=OWNER,
            actor={"user_id": OWNER, "role": "customer", "customer_id": OWNER},
            status="RUNNING",
            mode="single",
            strategy="economy",
            message="controlled",
            case_version=1,
            cycle=0,
            event_seq=1,
            created_at=stamp,
            updated_at=stamp,
            deadline=stamp + timedelta(seconds=90),
        )
        db.add(run)
        db.flush()
        db.add(
            RunEvent(
                run_id=run_id,
                seq=1,
                kind="decision_input",
                created_at=stamp,
                payload={
                    "graph_profile": "chat-investigation-v1",
                    "scope": {"conversation_id": CHAT, "issue_id": issue},
                },
            )
        )
    registry = Registry.model_validate(
        {
            "version": "issue-controlled",
            "models": [
                {
                    "alias": "economy",
                    "provider": "fixture",
                    "model": "fixture",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                }
            ],
        }
    )

    def admit(db, reserve, protected):
        try:
            admit_issue(db, CHAT, issue, budget, to_microusd(reserve), protected_calls=protected)
        except IssueBudgetDenied as error:
            raise ModelFailure(str(error)) from None

    audit = RunAudit(state.engine, run, spend_admission=admit, spend_scope=(CHAT, issue))
    barrier = Barrier(2)

    def photo():
        barrier.wait(5)
        try:
            state.service.reserve(lease.turn_id, lease.token, 70, LOGICAL)
        except BusinessError as error:
            return error.code
        return "STARTED"

    def text():
        barrier.wait(5)
        try:
            audit.begin_attempt("p4:single:0", 1, registry.models[0], Decimal("0.00007"), registry)
        except ModelFailure as error:
            return error.code
        return "STARTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(photo), pool.submit(text)]
        assert sorted(item.result(timeout=10) for item in futures) == [
            "INVESTIGATION_ISSUE_COST_LIMIT",
            "STARTED",
        ]
    with Session(state.engine) as db:
        image_rows = list(db.scalars(select(ChatModelAttempt)))
        model_rows = list(db.scalars(select(ModelAttempt)))
        assert len(image_rows) + len(model_rows) == 1
        if image_rows:
            assert image_rows[0].model_evidence["issue_id"] == issue
        if model_rows:
            assert model_rows[0].metadata_json["reserved_cost_usd"] == "0.00007"
