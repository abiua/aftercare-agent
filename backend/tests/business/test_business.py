from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, current_user
from aftercare.business.api import router
from aftercare.business.errors import BusinessError
from aftercare.business.models import Approval, Inventory, Order, Policy
from aftercare.business.payment import LocalPaymentGateway
from aftercare.business.rules import now
from aftercare.business.service import BusinessService, initialize
from aftercare.db import Base

ROOT = Path(__file__).resolve().parents[3]
OWNER = UserContext(user_id="owner", role="customer", customer_id="C001")
OTHER = UserContext(user_id="other", role="customer", customer_id="C002")
SUPPORT = UserContext(user_id="support", role="support")
APPROVER = UserContext(user_id="approver", role="approver")
TESTER = UserContext(user_id="tester", role="tester")


class MemoryObjects:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def get(self, key, expected_size):
        if key not in self.objects:
            raise BusinessError("MATERIAL_OBJECT_MISSING", "Object missing", 503)
        return self.objects[key]

    def presign(self, key, content_type):
        return "https://storage.test/object?signed=true"


class TimeoutAfterPayment:
    def __init__(self, engine):
        self.gateway = LocalPaymentGateway(engine)
        self.submissions = 0
        self.lookups = 0

    def submit(self, *args):
        self.submissions += 1
        self.gateway.submit(*args)
        raise TimeoutError()

    def lookup(self, *args):
        self.lookups += 1
        return self.gateway.lookup(*args)


@pytest.fixture
def system(tmp_path):
    seed = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    timestamp = now() - timedelta(days=1)
    items = [
        {
            "line_id": "L01",
            "product_id": "P001",
            "quantity": 2,
            "unit_price_cents": 19900,
            "discount_cents": 0,
            "payable_cents": 39800,
            "paid_cents": 39800,
            "refunded_cents": 0,
        },
        {
            "line_id": "L02",
            "product_id": "P002",
            "quantity": 1,
            "unit_price_cents": 50000,
            "discount_cents": 0,
            "payable_cents": 50000,
            "paid_cents": 50000,
            "refunded_cents": 0,
        },
    ]
    order = {
        "id": "ORDER",
        "customer_id": OWNER.customer_id,
        "status": "DELIVERED",
        "payment_status": "CONFIRMED",
        "created_at": (timestamp - timedelta(days=4)).isoformat(),
        "paid_at": (timestamp - timedelta(days=3)).isoformat(),
        "shipped_at": (timestamp - timedelta(days=2)).isoformat(),
        "delivered_at": timestamp.isoformat(),
        "cancelled_at": None,
        "subtotal_cents": 89800,
        "discount_cents": 0,
        "payable_cents": 89800,
        "paid_cents": 89800,
        "refunded_cents": 0,
        "items": items,
    }
    seed["orders"] = [order]
    seed["shipments"] = []
    engine = create_engine(f"sqlite:///{tmp_path / 'business.db'}")

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    initialize(engine, seed)
    objects = MemoryObjects()
    settings = SimpleNamespace(profile="test")
    service = BusinessService(engine, settings, storage=objects)
    yield SimpleNamespace(
        engine=engine, seed=seed, service=service, objects=objects, settings=settings
    )
    engine.dispose()


def _photo(color):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color=color).save(out, format="PNG")
    return out.getvalue()


def _create(service, intent="damaged", lines=("L01",)):
    return service.create_case(
        OWNER,
        order_id="ORDER",
        intent=intent,
        description="模拟订单出现售后问题",
        expected_version=service.order("ORDER", OWNER)["version"],
        idempotency_key=f"case-{intent}-{'-'.join(lines)}",
        line_ids=list(lines),
    )["id"]


def _change(service, case_id, actor, method, key, **values):
    context = service.context(case_id, actor)
    return method(
        case_id, actor, expected_version=context["case"]["version"], idempotency_key=key, **values
    )


def _evidence(service, case_id, kinds=("ITEM", "PARCEL")):
    for kind, color in zip(kinds, ("green", "orange"), strict=True):
        _change(
            service,
            case_id,
            OWNER,
            service.upload,
            f"upload-{kind}",
            kind=kind,
            content=_photo(color),
        )
    return _change(
        service,
        case_id,
        SUPPORT,
        service.review_materials,
        "review",
        decision="APPROVED",
        notes="已人工核对材料。",
    )


def _propose(service, case_id, *, action="REFUND", lines=None, key="proposal"):
    return _change(
        service,
        case_id,
        SUPPORT,
        service.propose,
        key,
        action=action,
        lines=lines or [{"line_id": "L01", "amount_cents": 39800}],
        notes="申请模拟处置",
        policy_ids=[],
    )


def _approve(service, case_id, proposal, key="approve"):
    return _change(
        service,
        case_id,
        APPROVER,
        service.approve,
        key,
        proposal_id=proposal["id"],
        decision="APPROVED",
        notes="核对同意",
    )


def _execute(service, case_id, proposal, key="execute"):
    return _change(service, case_id, SUPPORT, service.execute, key, proposal_id=proposal["id"])


def _code(exc, expected):
    assert exc.value.code == expected


def test_ownership_roles_and_scope(system):
    service = system.service
    assert service.orders(OTHER) == []
    case_id = _create(service)
    assert service.cases(OTHER) == []
    assert service.cases(TESTER)[0]["id"] == case_id
    for call in (
        lambda: service.order("ORDER", OTHER),
        lambda: service.context(case_id, OTHER),
        lambda: service.events(case_id, OTHER, 0),
    ):
        with pytest.raises(BusinessError) as exc:
            call()
        _code(exc, "NOT_FOUND")
    with pytest.raises(BusinessError) as exc:
        _change(service, case_id, TESTER, service.upload, "bad", kind="ITEM", content=_photo("red"))
    _code(exc, "FORBIDDEN")


def test_case_replay_and_version_conflict(system):
    service = system.service
    values = {
        "order_id": "ORDER",
        "intent": "damaged",
        "description": "模拟订单损坏描述",
        "expected_version": 1,
        "idempotency_key": "same",
    }
    first = service.create_case(OWNER, **values)
    assert service.create_case(OWNER, **values) == first
    assert service.create_case(OWNER, **{**values, "idempotency_key": "other"}) == first
    with pytest.raises(BusinessError) as exc:
        service.create_case(OWNER, **{**values, "description": "别的模拟描述"})
    _code(exc, "IDEMPOTENCY_CONFLICT")
    with pytest.raises(BusinessError) as exc:
        service.create_case(
            OWNER,
            **{**values, "idempotency_key": "stale", "intent": "return", "expected_version": 2},
        )
    _code(exc, "VERSION_CONFLICT")
    assert [row["type"] for row in service.events(first["id"], OWNER, 0)] == ["CASE_CREATED"]


def test_upload_validation_duplicate_ownership_and_storage_integrity(system):
    service = system.service
    case_id = _create(service)
    assert service.context(case_id, OWNER)["eligibility"]["missing_materials"] == ["ITEM", "PARCEL"]
    with pytest.raises(BusinessError) as exc:
        _change(service, case_id, OWNER, service.upload, "invalid", kind="ITEM", content=b"<html>")
    _code(exc, "INVALID_IMAGE")
    content = _photo("red")
    first = service.upload(
        case_id, OWNER, kind="ITEM", content=content, expected_version=1, idempotency_key="one"
    )
    for key in ("one", "different"):
        assert (
            service.upload(
                case_id,
                OWNER,
                kind="ITEM",
                content=content,
                expected_version=1,
                idempotency_key=key,
            )
            == first
        )
    assert service.context(case_id, OWNER)["case"]["version"] == 2
    assert service.material_content(case_id, first["id"], OWNER) == (content, "image/png")
    with pytest.raises(BusinessError) as exc:
        service.material_content(case_id, first["id"], OTHER)
    _code(exc, "NOT_FOUND")
    system.objects.objects[next(iter(system.objects.objects))] = b"corrupted"
    with pytest.raises(BusinessError) as exc:
        service.material_content(case_id, first["id"], OWNER)
    _code(exc, "MATERIAL_INTEGRITY_ERROR")
    with pytest.raises(BusinessError) as exc:
        _change(
            service,
            case_id,
            SUPPORT,
            service.review_materials,
            "corrupt",
            decision="NEEDS_MORE",
            notes="待补",
        )
    _code(exc, "MATERIAL_INTEGRITY_ERROR")


def test_material_approval_does_not_execute_and_new_photo_invalidates_plan(system):
    service = system.service
    case_id = _create(service)
    _evidence(service, case_id)
    proposal = _propose(service, case_id)
    with pytest.raises(BusinessError) as exc:
        _execute(service, case_id, proposal)
    _code(exc, "APPROVAL_REQUIRED")
    _approve(service, case_id, proposal)
    _change(
        service, case_id, OWNER, service.upload, "updated", kind="ITEM", content=_photo("yellow")
    )
    detail = service.detail(case_id, SUPPORT)
    assert not detail["reviews"][0]["effective"]
    assert not detail["approvals"][0]["effective"]
    assert detail["proposals"][0]["status"] == "STALE"
    with pytest.raises(BusinessError) as exc:
        _execute(service, case_id, proposal)
    _code(exc, "STALE_PROPOSAL")
    assert detail["ledger"] == []


@pytest.mark.parametrize("change", ["order", "policy", "expiry"])
def test_fact_policy_and_approval_bindings(system, change):
    service = system.service
    case_id = _create(service)
    _evidence(service, case_id)
    proposal = _propose(service, case_id)
    approval = _approve(service, case_id, proposal)
    with Session(system.engine) as db, db.begin():
        if change == "order":
            db.get_one(Order, "ORDER").version += 1
        elif change == "policy":
            old = db.get_one(Policy, ("POL14", "1.0"))
            at = now() - timedelta(days=1)
            db.add(
                Policy(
                    id=old.id,
                    version="1.1",
                    effective_at=at,
                    data={**old.data, "version": "1.1", "effective_at": at.isoformat()},
                )
            )
        else:
            db.get_one(Approval, approval["id"]).expires_at = now() - timedelta(seconds=1)
    with pytest.raises(BusinessError) as exc:
        _execute(service, case_id, proposal)
    _code(exc, "APPROVAL_REQUIRED" if change == "expiry" else "STALE_PROPOSAL")
    assert service.context(case_id, OWNER)["ledger"] == []


def test_partial_and_high_refund_separate_authorizations(system):
    service = system.service
    case_id = _create(service, lines=("L01", "L02"))
    _evidence(service, case_id)
    with pytest.raises(BusinessError) as exc:
        _propose(service, case_id)
    _code(exc, "PARTIAL_RESOLUTION_REQUIRED")
    _change(
        service,
        case_id,
        APPROVER,
        service.record_facts,
        "resolution",
        facts={"resolution_status": "APPROVED"},
        notes="独立同意部分补偿",
    )
    proposal = _propose(
        service,
        case_id,
        lines=[
            {"line_id": "L01", "amount_cents": 39800},
            {"line_id": "L02", "amount_cents": 40000},
        ],
    )
    assert proposal["amount_cents"] == 79800
    with pytest.raises(BusinessError) as exc:
        _change(
            service,
            case_id,
            SUPPORT,
            service.approve,
            "wrong-role",
            proposal_id=proposal["id"],
            decision="APPROVED",
            notes="不能自己审批",
        )
    _code(exc, "FORBIDDEN")
    _approve(service, case_id, proposal)
    result = _execute(service, case_id, proposal)
    assert result["status"] == "SUCCEEDED"
    assert result["receipt"]["amount_cents"] == 79800
    assert service.order("ORDER", OWNER)["refunded_cents"] == 79800
    initialize(system.engine, system.seed)
    assert service.order("ORDER", OWNER)["refunded_cents"] == 79800


def test_pol09_current_line_balance_includes_exact_cap_and_rejects_one_cent_over(system):
    service = system.service
    case_id = _create(service)
    _evidence(service, case_id)
    context = service.context(case_id, OWNER)
    cap = context["eligibility"]["lines"][0]["available_cents"]
    assert cap == 39800

    def validate(amount):
        return service.validate_draft(
            case_id,
            OWNER,
            expected_version=context["case"]["version"],
            action="REFUND",
            lines=[{"line_id": "L01", "amount_cents": amount}],
            policy_ids=["POL09"],
        )

    with pytest.raises(BusinessError) as exc:
        validate(cap + 1)
    _code(exc, "REFUND_CAP_EXCEEDED")
    assert validate(cap)["amount_cents"] == cap
    assert service.detail(case_id, OWNER)["proposals"] == []


@pytest.mark.parametrize("amount", [50000, 50001])
def test_pol10_simulated_execution_requires_approval_at_and_above_threshold(system, amount):
    service = system.service
    case_id = _create(service, lines=("L01", "L02"))
    _evidence(service, case_id)
    _change(
        service,
        case_id,
        APPROVER,
        service.record_facts,
        "resolution",
        facts={"resolution_status": "APPROVED"},
        notes="独立同意部分补偿。",
    )
    proposal = _propose(
        service,
        case_id,
        lines=[
            {"line_id": "L01", "amount_cents": 39800},
            {"line_id": "L02", "amount_cents": amount - 39800},
        ],
    )
    assert "POL10" in proposal["policy_ids"] and proposal["amount_cents"] == amount
    with pytest.raises(BusinessError) as exc:
        _execute(service, case_id, proposal)
    _code(exc, "APPROVAL_REQUIRED")
    assert service.detail(case_id, OWNER)["ledger"] == []
    _approve(service, case_id, proposal)
    result = _execute(service, case_id, proposal)
    assert result["status"] == "SUCCEEDED"
    assert result["receipt"]["amount_cents"] == amount


def test_provider_commit_timeout_unknown_reservation_and_restart_reconcile(system):
    service = system.service
    fault = TimeoutAfterPayment(system.engine)
    service.payments = fault
    case_id = _create(service)
    _evidence(service, case_id)
    proposal = _propose(service, case_id)
    _approve(service, case_id, proposal)
    expected = service.context(case_id, OWNER)["case"]["version"]
    result = _execute(service, case_id, proposal)
    assert result["status"] == "UNKNOWN" and result["receipt"] is None
    assert service.order("ORDER", OWNER)["refunded_cents"] == 0
    assert service.context(case_id, OWNER)["eligibility"]["available_cents"] == 0
    assert (
        service.execute(
            case_id,
            SUPPORT,
            expected_version=expected,
            idempotency_key="execute",
            proposal_id=proposal["id"],
        )
        == result
    )
    assert _execute(service, case_id, proposal, "other-key") == result
    assert fault.submissions == 1 and fault.lookups == 0

    status_case = _create(service, "refund_status")
    assert service.context(status_case, OWNER)["ledger"][0]["status"] == "UNKNOWN"
    restarted = BusinessService(system.engine, system.settings, storage=system.objects)
    status = restarted.reconcile(
        case_id,
        result["id"],
        SUPPORT,
        expected_version=service.context(case_id, OWNER)["case"]["version"],
        idempotency_key="lookup",
    )
    assert status["status"] == "SUCCEEDED" and status["receipt"]["mode"] == "SIMULATED"
    assert restarted.context(status_case, OWNER)["ledger"][0]["status"] == "SUCCEEDED"
    assert restarted.order("ORDER", OWNER)["refunded_cents"] == 39800
    assert _execute(restarted, case_id, proposal, "another-key") == status


def test_replacement_stock_and_overlapping_case_dedup(system):
    service = system.service
    case_id = _create(service, "wrong_item")
    _evidence(service, case_id, ("WRONG_ITEM", "LABEL"))
    with pytest.raises(BusinessError) as exc:
        _propose(service, case_id, action="REPLACE", lines=[{"line_id": "L01", "quantity": 3}])
    _code(exc, "STOCK_INSUFFICIENT")
    proposal = _propose(
        service, case_id, action="REPLACE", lines=[{"line_id": "L01", "quantity": 2}]
    )
    _approve(service, case_id, proposal)
    result = _execute(service, case_id, proposal)
    assert result["receipt"]["result"] == "STOCK_RECORDED"
    assert service.order("ORDER", OWNER)["refunded_cents"] == 0
    with Session(system.engine) as db:
        assert db.get_one(Inventory, "P001").available_quantity == 8
    other = _create(service, "wrong_item", ("L01", "L02"))
    _evidence(service, other, ("WRONG_ITEM", "LABEL"))
    with pytest.raises(BusinessError) as exc:
        _propose(service, other, action="REPLACE", lines=[{"line_id": "L01", "quantity": 1}])
    _code(exc, "DUPLICATE_MATTER")


def test_fastapi_business_and_sse_cursor(system):
    service = system.service
    app = FastAPI()
    app.state.business = service
    app.dependency_overrides[current_user] = lambda: OWNER
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        body = {
            "order_id": "ORDER",
            "intent": "damaged",
            "description": "模拟订单损坏问题",
            "expected_version": 1,
        }
        response = client.post("/api/cases", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_IDEMPOTENCY_KEY"
        response = client.post("/api/cases", json=body, headers={"Idempotency-Key": "case"})
        assert response.status_code == 201
        case_url = f"/api/cases/{response.json()['id']}"
        response = client.post(
            f"{case_url}/materials",
            files={"file": ("photo.png", _photo("red"), "image/png")},
            data={"expected_version": 1, "kind": "ITEM"},
            headers={"Idempotency-Key": "upload"},
        )
        assert response.status_code == 201, response.text
        response = client.get(f"{case_url}/events?follow=false", headers={"Last-Event-ID": "1"})
        assert response.status_code == 200
        assert "MATERIAL_UPLOADED" in response.text and "CASE_CREATED" not in response.text
        assert response.text.startswith("id: 2\nevent: business\n")
        assert client.get(f"{case_url}/events?after=3&follow=false").status_code == 409
        app.dependency_overrides[current_user] = lambda: OTHER
        assert client.get(case_url).status_code == 404
        assert client.get(f"{case_url}/events?follow=false").status_code == 404


def test_cancel_requires_fulfillment_and_whole_balance(system):
    service = system.service
    with Session(system.engine) as db, db.begin():
        order = db.get_one(Order, "ORDER")
        order.data = {**order.data, "status": "PAID", "shipped_at": None, "delivered_at": None}
    case_id = _create(service, "cancel", ("L01", "L02"))
    with pytest.raises(BusinessError) as exc:
        _propose(service, case_id)
    _code(exc, "ACTION_NOT_ELIGIBLE")
    _change(
        service,
        case_id,
        SUPPORT,
        service.record_facts,
        "verify",
        facts={"fulfillment_status": "UNSHIPPED"},
        notes="模拟核对，订单未交运。",
    )
    with pytest.raises(BusinessError) as exc:
        _propose(service, case_id)
    _code(exc, "CANCEL_REQUIRES_FULL_BALANCE")
    proposal = _propose(
        service,
        case_id,
        lines=[
            {"line_id": "L01", "amount_cents": 39800},
            {"line_id": "L02", "amount_cents": 50000},
        ],
    )
    _approve(service, case_id, proposal)
    assert _execute(service, case_id, proposal)["status"] == "SUCCEEDED"
    assert service.order("ORDER", OWNER)["status"] == "CANCELLED"


def test_return_verification_is_per_line_and_draft_does_not_write(system):
    service = system.service
    case_id = _create(service, "return", ("L01", "L02"))
    with pytest.raises(BusinessError) as exc:
        _change(
            service,
            case_id,
            SUPPORT,
            service.record_facts,
            "bad",
            facts={"inspection_status": "PASSED"},
            line_ids=["L02"],
            notes="模拟验货。",
        )
    _code(exc, "INVALID_FACTS")
    _change(
        service,
        case_id,
        SUPPORT,
        service.record_facts,
        "verified",
        facts={"inspection_status": "PASSED", "return_receipt_status": "RECEIVED"},
        line_ids=["L01"],
        notes="仅收到第一行并已验货。",
    )
    context = service.context(case_id, OWNER)
    assert [line["rule_status"] for line in context["eligibility"]["lines"]] == [
        "RETURN_VERIFIED",
        "RETURN_VERIFICATION_REQUIRED",
    ]
    assert context["eligibility"]["available_cents"] == 39800
    result = service.validate_draft(
        case_id,
        OWNER,
        expected_version=context["case"]["version"],
        action="REFUND",
        lines=[{"line_id": "L01", "amount_cents": 39800}],
        policy_ids=["POL07"],
    )
    assert result["amount_cents"] == 39800
    assert service.detail(case_id, OWNER)["proposals"] == []
    assert service.context(case_id, OWNER)["case"]["version"] == context["case"]["version"]
    with pytest.raises(BusinessError) as exc:
        _propose(service, case_id, lines=[{"line_id": "L02", "amount_cents": 50000}])
    _code(exc, "REFUND_CAP_EXCEEDED")


def test_unknown_provider_result_never_resubmits(system):
    service = system.service
    original = LocalPaymentGateway(system.engine)
    count = 0

    class TimeoutBeforePayment:
        def submit(self, *args):
            nonlocal count
            count += 1
            raise TimeoutError()

        def lookup(self, *args):
            return original.lookup(*args)

    service.payments = TimeoutBeforePayment()
    case_id = _create(service)
    _evidence(service, case_id)
    proposal = _propose(service, case_id)
    _approve(service, case_id, proposal)
    execution = _execute(service, case_id, proposal)
    assert execution["status"] == "UNKNOWN"
    assert (
        _change(
            service,
            case_id,
            SUPPORT,
            lambda cid, actor, **kw: service.reconcile(cid, execution["id"], actor, **kw),
            "lookup",
        )["status"]
        == "UNKNOWN"
    )
    assert _execute(service, case_id, proposal, "new-key") == execution
    assert count == 1
    assert service.context(case_id, OWNER)["eligibility"]["available_cents"] == 0

    other = _create(service, "return")
    _change(
        service,
        other,
        SUPPORT,
        service.record_facts,
        "received",
        facts={"inspection_status": "PASSED", "return_receipt_status": "RECEIVED"},
        notes="模拟核对退件。",
    )
    with pytest.raises(BusinessError) as exc:
        _propose(service, other)
    _code(exc, "ACTION_NOT_ELIGIBLE")
    assert service.order("ORDER", OWNER)["refunded_cents"] == 0


def test_multipart_stream_size_limit_without_content_length(system):
    service = system.service
    case_id = _create(service)
    app = FastAPI()
    app.state.business = service
    app.dependency_overrides[current_user] = lambda: OWNER
    app.include_router(router, prefix="/api")

    def chunks():
        yield b'--bound\r\nContent-Disposition: form-data; name="file"; filename="photo.png"\r\n\r\n'
        for _ in range(9):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--bound--\r\n"

    with TestClient(app) as client:
        response = client.post(
            f"/api/cases/{case_id}/materials",
            content=chunks(),
            headers={
                "Content-Type": "multipart/form-data; boundary=bound",
                "Idempotency-Key": "oversized",
            },
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "MATERIAL_TOO_LARGE"
    assert service.context(case_id, OWNER)["materials"] == []
    assert system.objects.objects == {}


def test_sku_policy_version_is_scoped_and_mixed_terms_fail_closed(system):
    service = system.service
    sku = service.order("ORDER", OWNER)["items"][0]["product_id"]
    with Session(system.engine) as db, db.begin():
        base = db.get_one(Policy, ("POL06", "1.0"))
        policy = {
            **base.data,
            "version": "sku-version",
            "product_ids": [sku],
            "time_basis": "delivered_at",
            "parameters": {"required_photo_kinds": ["ITEM"]},
        }
        db.add(
            Policy(
                id=base.id,
                version=policy["version"],
                effective_at=base.effective_at,
                data=policy,
            )
        )
    first = _create(service, lines=("L01",))
    mixed = _create(service, lines=("L01", "L02"))
    one = service.context(first, OWNER)
    all_lines = service.context(mixed, OWNER)
    assert one["eligibility"]["missing_materials"] == ["ITEM"]
    assert any(
        row["version"] == "sku-version" for row in one["policy_resolution"]["by_product"][sku]
    )
    assert all_lines["eligibility"]["status"] == "HUMAN_REVIEW"
    assert all_lines["eligibility"]["reason_code"] == "UNRESOLVED_POLICY"
    assert "INCOMPATIBLE_SCOPED_TERMS:POL06" in all_lines["policy_resolution"]["blockers"]


def test_refund_lookup_http_is_order_scoped_and_read_only(system):
    service = system.service
    additional_orders = [
        {**system.seed["orders"][0], "id": "SECOND-ORDER"},
        {**system.seed["orders"][0], "id": "PRIVATE-ORDER", "customer_id": OTHER.customer_id},
    ]
    initialize(system.engine, {**system.seed, "orders": additional_orders})
    query = _create(service, "refund_status")

    def create(order_id, user, intent):
        return service.create_case(
            user,
            order_id=order_id,
            intent=intent,
            description="请核对这件订单商品",
            expected_version=service.order(order_id, user)["version"],
            idempotency_key=f"{order_id}-{intent}",
            line_ids=["L01"],
        )["id"]

    def refund(case_id, amount):
        _evidence(service, case_id)
        plan = _propose(
            service,
            case_id,
            lines=[
                {
                    "line_id": service.context(case_id, SUPPORT)["case"]["line_ids"][0],
                    "amount_cents": amount,
                }
            ],
        )
        _approve(service, case_id, plan)
        return _execute(service, case_id, plan)

    first_case = _create(service)
    first = refund(first_case, 39800)
    other_order = refund(create("SECOND-ORDER", OWNER, "damaged"), 39800)
    other_query = create("PRIVATE-ORDER", OTHER, "refund_status")
    fault = TimeoutAfterPayment(system.engine)
    service.payments = fault
    second = refund(_create(service, lines=("L02",)), 50000)

    def snapshot():
        with system.engine.connect() as connection:
            names = (
                "business_cases",
                "business_orders",
                "business_order_lines",
                "business_approvals",
                "business_proposals",
                "business_executions",
                "business_simulated_payments",
                "business_events",
                "business_idempotency",
            )
            return {
                name: [
                    dict(row)
                    for row in connection.execute(
                        Base.metadata.tables[name]
                        .select()
                        .order_by(*Base.metadata.tables[name].primary_key)
                    ).mappings()
                ]
                for name in names
            }

    before = snapshot()
    app = FastAPI()
    app.state.business = service
    app.include_router(router)
    actor = [OWNER]
    app.dependency_overrides[current_user] = lambda: actor[0]
    with TestClient(app) as client:
        path = f"/cases/{query}/refund-lookup"
        for execution, confirmed in ((first, True), (second, False)):
            result = client.get(path, params={"execution_id": execution["id"].upper()})
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            assert result.json() == {
                "schema_version": "ac-v1",
                "status": "FOUND",
                "execution": {
                    **{
                        key: execution[key]
                        for key in ("id", "status", "amount_cents", "created_at")
                    },
                    "receipt_confirmed": confirmed,
                },
            }
        not_found = client.get(
            path, params={"execution_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
        )
        assert not_found.status_code == 404
        for other_path, execution_id in (
            (path, other_order["id"]),
            (f"/cases/{other_query}/refund-lookup", first["id"]),
            ("/cases/not-an-existing-case/refund-lookup", first["id"]),
        ):
            result = client.get(other_path, params={"execution_id": execution_id})
            assert result.status_code == 404 and result.json() == not_found.json()
            assert result.headers["cache-control"] == "no-store"
        actor[0] = OTHER
        result = client.get(
            f"/cases/{other_query}/refund-lookup", params={"execution_id": first["id"]}
        )
        assert result.status_code == 404 and result.json() == not_found.json()
        actor[0] = OWNER
        missing = client.get(path)
        assert missing.status_code == 422 and missing.json()["detail"]["code"] == "VALIDATION_ERROR"
        for params in (
            {"execution_id": ""},
            {"execution_id": first["id"][:8]},
            [("execution_id", first["id"]), ("execution_id", second["id"])],
        ):
            invalid = client.get(path, params=params)
            assert invalid.status_code == 422
            assert invalid.json()["detail"]["code"] == "INVALID_REFUND_EXECUTION_ID"
        detail = client.get(f"/cases/{query}")
        assert detail.json()["refund_lookup_available"] is True
        wrong_intent = client.get(
            f"/cases/{first_case}/refund-lookup", params={"execution_id": first["id"]}
        )
        assert wrong_intent.status_code == 422
        assert wrong_intent.json()["detail"]["code"] == "REFUND_LOOKUP_INTENT_REQUIRED"
    del app.dependency_overrides[current_user]
    with TestClient(app) as client:
        anonymous = client.get(path, params={"execution_id": first["id"]})
        assert anonymous.status_code == 401
        assert anonymous.headers["cache-control"] == "no-store"
    assert snapshot() == before
    assert (fault.submissions, fault.lookups) == (1, 0)


def test_refund_lookup_rejects_same_order_replacement(system):
    service = system.service
    wrong = _create(service, "wrong_item")
    _evidence(service, wrong, kinds=("WRONG_ITEM", "LABEL"))
    plan = _propose(service, wrong, action="REPLACE", lines=[{"line_id": "L01", "quantity": 1}])
    _approve(service, wrong, plan)
    execution = _execute(service, wrong, plan)
    assert execution["status"] == "SUCCEEDED" and execution["action"] == "REPLACE"
    query = _create(service, "refund_status")
    with pytest.raises(BusinessError) as error:
        service.refund_lookup(query, OWNER, [execution["id"]])
    assert error.value.code == "NOT_FOUND" and error.value.status == 404
