from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aftercare.db import Base


class Customer(Base):
    __tablename__ = "business_customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class Product(Base):
    __tablename__ = "business_products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class Order(Base):
    __tablename__ = "business_orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey(Customer.id), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)


class OrderLine(Base):
    __tablename__ = "business_order_lines"
    __table_args__ = (
        CheckConstraint("paid_cents >= 0 AND refunded_cents BETWEEN 0 AND paid_cents"),
        CheckConstraint("quantity > 0"),
    )

    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), primary_key=True)
    line_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey(Product.id))
    data: Mapped[dict] = mapped_column(JSON)
    quantity: Mapped[int] = mapped_column(Integer)
    paid_cents: Mapped[int] = mapped_column(Integer)
    refunded_cents: Mapped[int] = mapped_column(Integer)


class Inventory(Base):
    __tablename__ = "business_inventory"
    __table_args__ = (CheckConstraint("available_quantity >= 0"),)

    product_id: Mapped[str] = mapped_column(ForeignKey(Product.id), primary_key=True)
    available_quantity: Mapped[int] = mapped_column(Integer)


class Policy(Base):
    __tablename__ = "business_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class Case(Base):
    __tablename__ = "business_cases"
    __table_args__ = (
        UniqueConstraint("order_id", "intent", "scope_hash"),
        CheckConstraint(
            "(decision_conversation_id IS NULL) = (decision_issue_id IS NULL) "
            "AND (decision_conversation_id IS NULL) = (decision_legacy_through_version IS NULL)",
            name="ck_business_cases_decision_binding",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(36), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey(Customer.id), index=True)
    intent: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    scope_hash: Mapped[str] = mapped_column(String(64))
    line_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    version: Mapped[int] = mapped_column(default=1)
    materials_version: Mapped[int] = mapped_column(default=0)
    facts_version: Mapped[int] = mapped_column(default=0)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_conversation_id: Mapped[str | None] = mapped_column(String(36))
    decision_issue_id: Mapped[str | None] = mapped_column(String(36))
    decision_legacy_through_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObjectUpload(Base):
    __tablename__ = "business_object_uploads"

    object_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Material(Base):
    __tablename__ = "business_materials"
    __table_args__ = (UniqueConstraint("case_id", "kind", "sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    object_key: Mapped[str] = mapped_column(ForeignKey(ObjectUpload.object_key), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MaterialReview(Base):
    __tablename__ = "business_material_reviews"
    __table_args__ = (UniqueConstraint("case_id", "materials_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), index=True)
    materials_version: Mapped[int] = mapped_column(Integer)
    material_ids: Mapped[list] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String(24))
    notes: Mapped[str] = mapped_column(Text)
    reviewer_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Proposal(Base):
    __tablename__ = "business_proposals"
    __table_args__ = (UniqueConstraint("case_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    lines: Mapped[list] = mapped_column(JSON)
    notes: Mapped[str] = mapped_column(Text)
    policy_ids: Mapped[list] = mapped_column(JSON)
    binding: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    proposer_id: Mapped[str] = mapped_column(String(128))
    decision_binding: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "business_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey(Proposal.id), index=True)
    proposal_version: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text)
    approver_id: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Execution(Base):
    __tablename__ = "business_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True)
    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey(Proposal.id), unique=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey(Approval.id))
    payload_hash: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    lines: Mapped[list] = mapped_column(JSON)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SimulatedPayment(Base):
    __tablename__ = "business_simulated_payments"

    business_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    receipt: Mapped[dict] = mapped_column(JSON)


class Idempotency(Base):
    __tablename__ = "business_idempotency"

    actor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "business_events"

    case_id: Mapped[str] = mapped_column(ForeignKey(Case.id), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
