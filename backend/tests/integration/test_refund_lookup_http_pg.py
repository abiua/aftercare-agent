"""Exercise the production refund reader over TCP against an isolated PostgreSQL schema."""

import json
import os
import secrets
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.business.api import router
from aftercare.business.models import Approval, Execution, Proposal
from aftercare.business.service import BusinessService, initialize
from aftercare.config import ROOT, load_settings
from aftercare.db import Base, User, guarded_engine

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_REFUND_LOOKUP_HTTP_PG") != "1",
        reason="explicit isolated PostgreSQL and TCP acceptance required",
    ),
]
STAMP = datetime(2026, 11, 10, 8, tzinfo=UTC)
OWNER = UserContext(user_id="lookup-owner", role="customer", customer_id="C001")
OTHER = UserContext(user_id="lookup-other", role="customer", customer_id="C002")


class NoPaymentGateway:
    def __init__(self):
        self.submissions = self.lookups = 0

    def submit(self, *_):
        self.submissions += 1
        pytest.fail("read-only lookup submitted a payment")

    def lookup(self, *_):
        self.lookups += 1
        pytest.fail("read-only lookup contacted a payment provider")


@contextmanager
def loopback(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=None,
                log_level="critical",
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=2,
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 8
            while thread.is_alive() and not server.started and monotonic() < deadline:
                sleep(0.02)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def seed_orders(scoped):
    seed = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    template = next(order for order in seed["orders"] if order["items"])
    seed["orders"] = [
        {
            **template,
            "id": order_id,
            "customer_id": user.customer_id,
            "status": "DELIVERED",
            "payment_status": "CONFIRMED",
            "paid_at": (STAMP - timedelta(days=3)).isoformat(),
            "delivered_at": (STAMP - timedelta(days=2)).isoformat(),
        }
        for order_id, user in (
            ("LOOKUP-ORDER", OWNER),
            ("LOOKUP-SECOND", OWNER),
            ("LOOKUP-PRIVATE", OTHER),
        )
    ]
    seed["shipments"] = []
    initialize(scoped, seed)


def case(service, user, order_id, intent):
    return service.create_case(
        user,
        order_id=order_id,
        intent=intent,
        description="隔离验收使用的模拟售后订单",
        expected_version=service.order(order_id, user)["version"],
        idempotency_key=f"{user.user_id}-{order_id}-{intent}",
    )


def seed_execution(scoped, origin, action, status, *, has_receipt):
    """Seed a synthetic ledger state and valid relational parents, without a payment call."""
    execution_id, proposal_id, approval_id = (str(uuid4()) for _ in range(3))
    private_key = uuid4().hex * 2
    payload_hash = "a" * 64
    amount = 700 if action == "REFUND" else 0
    lines = [
        {"line_id": origin["line_ids"][0], "amount_cents": amount}
        if action == "REFUND"
        else {"line_id": origin["line_ids"][0], "quantity": 1}
    ]
    with Session(scoped) as db, db.begin():
        db.add(
            Proposal(
                id=proposal_id,
                case_id=origin["id"],
                version=1,
                action=action,
                lines=lines,
                notes="synthetic private proposal",
                policy_ids=[],
                binding={},
                payload_hash=payload_hash,
                amount_cents=amount,
                status="APPROVED",
                proposer_id="fixture-support",
                created_at=STAMP,
            )
        )
        db.flush()
        db.add(
            Approval(
                id=approval_id,
                case_id=origin["id"],
                proposal_id=proposal_id,
                proposal_version=1,
                payload_hash=payload_hash,
                decision="APPROVED",
                notes="synthetic private approval",
                approver_id="fixture-approver",
                expires_at=STAMP + timedelta(hours=1),
                created_at=STAMP,
            )
        )
        db.flush()
        db.add(
            Execution(
                id=execution_id,
                business_key=private_key,
                case_id=origin["id"],
                order_id=origin["order_id"],
                proposal_id=proposal_id,
                approval_id=approval_id,
                payload_hash=payload_hash,
                action=action,
                lines=lines,
                amount_cents=amount,
                status=status,
                receipt={"id": f"simulated-{private_key}", "mode": "SIMULATED"}
                if has_receipt
                else None,
                actor_id="fixture-executor",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
    return {
        "id": execution_id,
        "status": status,
        "amount_cents": amount,
        "created_at": STAMP.isoformat(),
        "receipt_confirmed": has_receipt,
    }


def fingerprints(engine, schema, tables):
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        return {
            table.name: tuple(
                conn.execute(
                    text(
                        "SELECT count(*), encode(sha256(convert_to(coalesce("
                        "string_agg(row_json, E'\\n' ORDER BY row_json), ''), 'UTF8')), 'hex') "
                        f'FROM (SELECT to_jsonb(t)::text AS row_json FROM "{schema}"."{table.name}" t) all_rows'
                    )
                ).one()
            )
            for table in tables
        }


def lookup_path(case_row):
    return f"/api/cases/{case_row['id']}/refund-lookup"


def verify(client, credentials, queries, executions):
    owner, other = (
        {"Authorization": f"Bearer {credentials[user.user_id]}"} for user in (OWNER, OTHER)
    )
    primary, private, non_refund = queries
    success, pending, other_order, replacement, private_record = executions
    path = lookup_path(primary)
    for expected in (success, pending):
        response = client.get(path, headers=owner, params={"execution_id": expected["id"].upper()})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "schema_version": "ac-v1",
            "status": "FOUND",
            "execution": expected,
        }
    assert (
        client.get(
            lookup_path(private), headers=other, params={"execution_id": private_record["id"]}
        ).status_code
        == 200
    )

    missing = client.get(path, headers=owner, params={"execution_id": str(uuid4())})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "NOT_FOUND"
    for candidate_path, credential, execution in (
        (path, owner, other_order),
        (path, owner, replacement),
        (path, owner, private_record),
        (lookup_path(private), owner, private_record),
        (path, other, success),
    ):
        response = client.get(
            candidate_path, headers=credential, params={"execution_id": execution["id"]}
        )
        assert response.status_code == 404 and response.json() == missing.json()
        assert response.headers["cache-control"] == "no-store"
        assert execution["id"] not in response.text

    assert client.get(path, params={"execution_id": success["id"]}).status_code == 401
    assert client.get(path, headers=owner).json()["detail"]["code"] == "VALIDATION_ERROR"
    for params in (
        [("execution_id", success["id"]), ("execution_id", pending["id"])],
        {"execution_id": success["id"][:8]},
        {"execution_id": ""},
    ):
        invalid = client.get(path, headers=owner, params=params)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "INVALID_REFUND_EXECUTION_ID"
    invalid_case = client.get(
        lookup_path(non_refund), headers=owner, params={"execution_id": success["id"]}
    )
    assert invalid_case.status_code == 422
    assert invalid_case.json()["detail"]["code"] == "REFUND_LOOKUP_INTENT_REQUIRED"


def test_real_http_refund_lookup_is_order_scoped_and_read_only():
    settings = load_settings("test")
    engine = guarded_engine(settings, test_only=True)
    schema = "refund_lookup_" + uuid4().hex
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name.startswith("business_") or table.name == "users"
    ]
    credentials = {user.user_id: secrets.token_urlsafe(32) for user in (OWNER, OTHER)}
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(scoped, tables=tables)
        seed_orders(scoped)
        with Session(scoped) as db, db.begin():
            for user in (OWNER, OTHER):
                db.add(
                    User(
                        id=user.user_id,
                        role=user.role,
                        customer_id=user.customer_id,
                        enabled=True,
                        display_name=user.user_id,
                        token_hash=token_hash(credentials[user.user_id]),
                    )
                )
        payments = NoPaymentGateway()
        service = BusinessService(
            scoped, settings, storage=object(), payments=payments, clock=lambda: STAMP
        )
        query = case(service, OWNER, "LOOKUP-ORDER", "refund_status")
        private_query = case(service, OTHER, "LOOKUP-PRIVATE", "refund_status")
        first = case(service, OWNER, "LOOKUP-ORDER", "damaged")
        executions = (
            seed_execution(scoped, first, "REFUND", "SUCCEEDED", has_receipt=True),
            seed_execution(
                scoped,
                case(service, OWNER, "LOOKUP-ORDER", "return"),
                "REFUND",
                "UNKNOWN",
                has_receipt=False,
            ),
            seed_execution(
                scoped,
                case(service, OWNER, "LOOKUP-SECOND", "damaged"),
                "REFUND",
                "SUCCEEDED",
                has_receipt=True,
            ),
            seed_execution(
                scoped,
                case(service, OWNER, "LOOKUP-ORDER", "wrong_item"),
                "REPLACE",
                "SUCCEEDED",
                has_receipt=True,
            ),
            seed_execution(
                scoped,
                case(service, OTHER, "LOOKUP-PRIVATE", "return"),
                "REFUND",
                "SUCCEEDED",
                has_receipt=True,
            ),
        )
        before = fingerprints(engine, schema, tables)
        app = FastAPI()
        app.state.business, app.state.engine = service, scoped
        app.include_router(router, prefix="/api")
        with (
            loopback(app) as base,
            httpx.Client(base_url=base, trust_env=False, timeout=5) as client,
        ):
            verify(client, credentials, (query, private_query, first), executions)
        assert fingerprints(engine, schema, tables) == before
        assert (payments.submissions, payments.lookups) == (0, 0)
        print(
            f"Verified full-row hashes for {len(tables)} isolated tables, 0 payment submissions or lookups."
        )
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                assert conn.scalar(
                    text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}
                )
        finally:
            engine.dispose()
