"""Opt-in PostgreSQL execution contention in a disposable business schema.

Material bytes are stored in memory. The simulated payment provider uses the same
isolated PostgreSQL schema and commits independently of the business ledger.
"""

import io
import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.models import Execution, Inventory, OrderLine, SimulatedPayment
from aftercare.business.payment import LocalPaymentGateway
from aftercare.business.service import BusinessService, initialize
from aftercare.config import load_settings
from aftercare.db import Base, guarded_engine

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_BUSINESS_CONTENTION_PG") != "1",
        reason="explicit isolated business contention acceptance",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
STAMP = datetime(2026, 11, 10, 10, tzinfo=UTC)
OWNER = UserContext(user_id="owner", role="customer", customer_id="C001")
SUPPORT = UserContext(user_id="support", role="support")
APPROVER = UserContext(user_id="approver", role="approver")
PAID = 10000
REQUESTED = 6000


class MemoryMaterials:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def get(self, key, expected_size):
        return self.objects[key]

    def presign(self, key, content_type):
        raise AssertionError("This test verifies stored bytes without generating URLs")


class PausedSimulatedPayment:
    def __init__(self, engine, timeout):
        self.provider = LocalPaymentGateway(engine)
        self.timeout = timeout
        self.committed = Event()
        self.release = Event()
        self.submits = []
        self.lookups = []

    def submit(self, *args):
        self.submits.append(args)
        outcome = self.provider.submit(*args)
        self.committed.set()
        if not self.release.wait(35):
            raise AssertionError("Test did not release the simulated provider")
        if self.timeout:
            raise TimeoutError("Simulated lost acknowledgement after provider commit")
        return outcome

    def lookup(self, *args):
        self.lookups.append(args)
        return self.provider.lookup(*args)


def seed_orders():
    baseline = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    item = {
        "line_id": "L01",
        "product_id": "P001",
        "quantity": 1,
        "unit_price_cents": PAID,
        "discount_cents": 0,
        "payable_cents": PAID,
        "paid_cents": PAID,
        "refunded_cents": 0,
    }
    baseline["orders"] = [
        {
            "id": order_id,
            "customer_id": OWNER.customer_id,
            "status": "DELIVERED",
            "payment_status": "CONFIRMED",
            "created_at": (STAMP - timedelta(days=4)).isoformat(),
            "paid_at": (STAMP - timedelta(days=3)).isoformat(),
            "shipped_at": (STAMP - timedelta(days=2)).isoformat(),
            "delivered_at": (STAMP - timedelta(days=1)).isoformat(),
            "cancelled_at": None,
            "subtotal_cents": PAID,
            "discount_cents": 0,
            "payable_cents": PAID,
            "paid_cents": PAID,
            "refunded_cents": 0,
            "items": [item],
        }
        for order_id in ("ORDER-A", "ORDER-B")
    ]
    baseline["shipments"] = []
    return baseline


@pytest.fixture
def service():
    settings = load_settings("test")
    engine = guarded_engine(settings, test_only=True)
    schema = "business_acceptance_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})

    @event.listens_for(scoped, "begin")
    def _bound_waits(conn):
        conn.exec_driver_sql("SET LOCAL lock_timeout = '12s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '25s'")

    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            tables = [
                table
                for table in Base.metadata.sorted_tables
                if table.schema is None and table.name.startswith("business_")
            ]
            Base.metadata.create_all(scoped, tables=tables)
            initialize(scoped, seed_orders())
            with Session(scoped) as db, db.begin():
                db.get_one(Inventory, "P001").available_quantity = 1
            yield BusinessService(scoped, settings, storage=MemoryMaterials(), clock=lambda: STAMP)
        finally:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()


def change(service, method, case_id, actor, key, **fields):
    version = service.context(case_id, actor)["case"]["version"]
    return method(case_id, actor, expected_version=version, idempotency_key=key, **fields)


def approved_plan(service, order_id, intent, action):
    case = service.create_case(
        OWNER,
        order_id=order_id,
        intent=intent,
        description="隔离 PostgreSQL 并发验收的模拟售后订单。",
        expected_version=service.order(order_id, OWNER)["version"],
        idempotency_key=f"case-{intent}",
        line_ids=["L01"],
    )
    case_id = case["id"]
    if intent == "return":
        change(
            service,
            service.record_facts,
            case_id,
            SUPPORT,
            "return-inspection",
            facts={"return_receipt_status": "RECEIVED", "inspection_status": "PASSED"},
            line_ids=["L01"],
            notes="模拟已收到并检查退件。",
        )
    else:
        kinds = ("ITEM", "PARCEL") if intent == "damaged" else ("WRONG_ITEM", "LABEL")
        for kind, color in zip(kinds, ("red", "blue"), strict=True):
            image = io.BytesIO()
            Image.new("RGB", (8, 8), color=color).save(image, format="PNG")
            change(
                service,
                service.upload,
                case_id,
                OWNER,
                f"upload-{kind}",
                kind=kind,
                content=image.getvalue(),
            )
        change(
            service,
            service.review_materials,
            case_id,
            SUPPORT,
            "material-review",
            decision="APPROVED",
            notes="模拟人工审核材料。",
        )
    if action == "REFUND":
        change(
            service,
            service.record_facts,
            case_id,
            APPROVER,
            "partial-approval",
            facts={"resolution_status": "APPROVED"},
            notes="模拟独立审批部分退款。",
        )
        line = {"line_id": "L01", "amount_cents": REQUESTED}
    else:
        line = {"line_id": "L01", "quantity": 1}
    proposal = change(
        service,
        service.propose,
        case_id,
        SUPPORT,
        "proposal",
        action=action,
        lines=[line],
        notes="模拟处置方案。",
        policy_ids=[],
    )
    change(
        service,
        service.approve,
        case_id,
        APPROVER,
        "approval",
        proposal_id=proposal["id"],
        decision="APPROVED",
        notes="模拟人工批准。",
    )
    return case_id, proposal["id"]


def execute(service, plan, key="execute"):
    case_id, proposal_id = plan
    try:
        return change(service, service.execute, case_id, SUPPORT, key, proposal_id=proposal_id)
    except BusinessError as failure:
        return {"code": failure.code, "case_id": case_id}


@contextmanager
def synchronize_row_lock(engine, table):
    """Rendezvous immediately before both real SELECT FOR UPDATE statements."""
    gate = Barrier(2, timeout=10)
    reached = []

    def meet(conn, cursor, statement, parameters, context, executemany):
        if table in statement and "FOR UPDATE" in statement and len(reached) < 2:
            reached.append(conn)
            gate.wait()

    event.listen(engine, "before_cursor_execute", meet)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", meet)
    assert len(reached) == 2 and reached[0] is not reached[1]


@pytest.mark.parametrize("timeout", [False, True], ids=["confirmed", "unknown"])
def test_overlapping_refunds_share_the_same_line_balance(service, timeout):
    damaged = approved_plan(service, "ORDER-A", "damaged", "REFUND")
    returned = approved_plan(service, "ORDER-A", "return", "REFUND")
    assert damaged[0] != returned[0]
    for case_id, _ in (damaged, returned):
        eligibility = service.context(case_id, OWNER)["eligibility"]
        assert eligibility["lines"][0]["available_cents"] == PAID
    payment = PausedSimulatedPayment(service.engine, timeout)
    service.payments = payment

    with (
        synchronize_row_lock(service.engine, "business_orders"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        pending = {pool.submit(execute, service, plan): plan for plan in (damaged, returned)}
        try:
            assert payment.committed.wait(15)
            completed, waiting = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
            assert len(completed) == len(waiting) == 1
            (loser_future,) = completed
            loser = pending[loser_future]
            assert loser_future.result() == {"case_id": loser[0], "code": "REFUND_CAP_EXCEEDED"}
            (winner_future,) = waiting
            winner = pending[winner_future]
            context = service.context(loser[0], OWNER)
            assert service.context(winner[0], OWNER)["ledger"][0]["status"] == "SUBMITTING"
            assert context["eligibility"]["lines"][0]["available_cents"] == PAID - REQUESTED
            assert service.order("ORDER-A", OWNER)["refunded_cents"] == 0
        finally:
            payment.release.set()
        result = winner_future.result(timeout=15)

    assert result["case_id"] == winner[0]
    assert result["status"] == ("UNKNOWN" if timeout else "SUCCEEDED")
    assert len(payment.submits) == 1
    if timeout:
        assert result["receipt"] is None
        assert service.order("ORDER-A", OWNER)["refunded_cents"] == 0
        assert (
            service.context(loser[0], OWNER)["eligibility"]["lines"][0]["available_cents"]
            == PAID - REQUESTED
        )
        assert execute(service, loser, "after-unknown")["code"] == "REFUND_CAP_EXCEEDED"
        assert execute(service, winner, "replayed-unknown")["id"] == result["id"]
        assert len(payment.submits) == 1 and payment.lookups == []
        resolved = change(
            service,
            lambda case_id, actor, **fields: service.reconcile(
                case_id, result["id"], actor, **fields
            ),
            winner[0],
            SUPPORT,
            "lookup-payment",
        )
        assert resolved["status"] == "SUCCEEDED"
        assert payment.lookups == payment.submits
    assert service.order("ORDER-A", OWNER)["refunded_cents"] == REQUESTED
    with Session(service.engine) as db:
        ledger = db.scalars(select(Execution)).all()
        assert [(row.case_id, row.status, row.amount_cents) for row in ledger] == [
            (winner[0], "SUCCEEDED", REQUESTED)
        ]
        line = db.get_one(OrderLine, ("ORDER-A", "L01"))
        assert 0 < line.refunded_cents == REQUESTED < line.paid_cents == PAID
        assert len(db.scalars(select(SimulatedPayment)).all()) == 1


def test_different_orders_share_one_last_replacement_unit(service):
    plans = [
        approved_plan(service, order, "wrong_item", "REPLACE") for order in ("ORDER-A", "ORDER-B")
    ]
    for case_id, _ in plans:
        assert (
            service.context(case_id, OWNER)["eligibility"]["lines"][0]["replace_available_quantity"]
            == 1
        )

    # Both orders passed initial validation before competing for the same SKU row.
    with (
        synchronize_row_lock(service.engine, "business_inventory"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first, second = [pool.submit(execute, service, plan) for plan in plans]
        results = [first.result(timeout=25), second.result(timeout=25)]

    successful = [row for row in results if row.get("status") == "SUCCEEDED"]
    rejected = [row for row in results if row.get("code") == "STOCK_INSUFFICIENT"]
    assert len(successful) == len(rejected) == 1
    assert successful[0]["receipt"]["result"] == "STOCK_RECORDED"
    assert successful[0]["case_id"] != rejected[0]["case_id"]
    with Session(service.engine) as db:
        assert db.get_one(Inventory, "P001").available_quantity == 0
        ledger = db.scalars(select(Execution)).all()
        assert [(row.case_id, row.action, row.status) for row in ledger] == [
            (successful[0]["case_id"], "REPLACE", "SUCCEEDED")
        ]
        assert db.scalars(select(SimulatedPayment)).all() == []
