from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, undefer

from aftercare.auth import UserContext
from aftercare.config import Settings
from aftercare.intelligence.policy_versions import resolve_policies

from .contracts import MaterialReviewObservations
from .errors import BusinessError
from .models import (
    Approval,
    Case,
    Event,
    Execution,
    Idempotency,
    Inventory,
    Material,
    MaterialReview,
    ObjectUpload,
    Order,
    OrderLine,
    Policy,
    Product,
    Proposal,
)
from .payment import LocalPaymentGateway, PaymentGateway, PaymentResult
from .refund_lookup import canonical_execution_id, locate_refund
from .rules import (
    INTENTS,
    MATERIAL_KINDS,
    PENDING_EXECUTION,
    digest,
    eligibility,
    now,
    scoped_policy_documents,
    utc,
)
from .seed import initialize
from .storage import ObjectStore, S3ObjectStore, validate_image, verify_object

__all__ = ["BusinessService", "initialize"]

APPROVAL_TTL = timedelta(minutes=15)
_REVIEW_OBSERVATIONS = TypeAdapter(MaterialReviewObservations)
FACT_VALUES = {
    "fulfillment_status": {"UNSHIPPED", "SHIPPED", "UNVERIFIED"},
    "interception_status": {"PENDING", "CONFIRMED", "FAILED", "UNVERIFIED"},
    "return_receipt_status": {"NOT_RECEIVED", "RECEIVED"},
    "inspection_status": {"NOT_STARTED", "PASSED", "FAILED"},
    "resolution_status": {"APPROVED", "REJECTED"},
}
FACT_INTENTS = {
    "fulfillment_status": {"cancel"},
    "interception_status": {"cancel"},
    "return_receipt_status": {"return"},
    "inspection_status": {"return"},
    "resolution_status": {"damaged", "wrong_item", "return"},
}


def _id() -> str:
    return str(uuid4())


def _stamp(value: datetime) -> str:
    return utc(value).isoformat()


def _row(row: Any, *fields: str) -> dict:
    data = {}
    for field in fields:
        value = getattr(row, field)
        data[field] = _stamp(value) if isinstance(value, datetime) else value
    return data


def _case_json(row: Case) -> dict:
    return _row(
        row,
        "id",
        "ticket_id",
        "order_id",
        "customer_id",
        "intent",
        "description",
        "status",
        "version",
        "materials_version",
        "line_ids",
        "created_at",
    )


def _material_json(row: Material) -> dict:
    return _row(row, "id", "kind", "sha256", "content_type", "size", "created_at")


def _review_json(row: MaterialReview, *, include_observations=False) -> dict:
    result = _row(
        row,
        "id",
        "materials_version",
        "material_ids",
        "decision",
        "notes",
        "reviewer_id",
        "created_at",
    )
    if include_observations and row.observations is not None:
        result["observations"] = row.observations
    return result


def _review_observations(observations):
    if observations is None:
        return None
    try:
        parsed = _REVIEW_OBSERVATIONS.validate_python(observations)
    except ValidationError:
        raise BusinessError(
            "INVALID_REVIEW_OBSERVATIONS", "材料观察格式或数量无效。", 422
        ) from None
    if len({item.material_id for item in parsed}) != len(parsed):
        raise BusinessError("INVALID_REVIEW_OBSERVATIONS", "同一材料只能登记一条观察。", 422)
    return [
        item.model_dump(exclude_none=True)
        for item in sorted(parsed, key=lambda item: item.material_id)
    ]


def _proposal_json(row: Proposal) -> dict:
    return _row(
        row,
        "id",
        "version",
        "action",
        "lines",
        "notes",
        "policy_ids",
        "binding",
        "payload_hash",
        "status",
        "amount_cents",
        "created_at",
    )


def _approval_json(row: Approval) -> dict:
    return _row(
        row,
        "id",
        "proposal_id",
        "proposal_version",
        "payload_hash",
        "decision",
        "notes",
        "approver_id",
        "expires_at",
        "created_at",
    )


def _execution_json(row: Execution) -> dict:
    return _row(
        row,
        "id",
        "case_id",
        "order_id",
        "proposal_id",
        "approval_id",
        "action",
        "status",
        "amount_cents",
        "lines",
        "receipt",
        "business_key",
        "created_at",
        "updated_at",
    )


def _require_role(user: UserContext, *roles: str) -> None:
    if user.role not in roles:
        raise BusinessError("FORBIDDEN", "当前角色没有此项操作权限。", 403)


def _authorize_customer(user: UserContext, customer_id: str) -> None:
    if user.role == "customer" and user.customer_id != customer_id:
        raise BusinessError("NOT_FOUND", "资源不存在。", 404)


def _version(actual: int, expected: int) -> None:
    if type(expected) is not int or actual != expected:
        raise BusinessError("VERSION_CONFLICT", "记录已变更，请刷新后再操作。")


def _read_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 128
        or any(not 33 <= ord(char) <= 126 for char in key)
    ):
        raise BusinessError("INVALID_IDEMPOTENCY_KEY", "操作必须带有效的 Idempotency-Key。", 422)
    return key


class BusinessService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        storage: ObjectStore | None = None,
        payments: PaymentGateway | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.settings = settings
        self.storage = storage if storage is not None else S3ObjectStore(settings)
        self.payments = payments if payments is not None else LocalPaymentGateway(engine)

    @contextmanager
    def _snapshot(self) -> Iterator[Session]:
        with Session(self.engine) as db:
            if self.engine.dialect.name == "postgresql":
                db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            yield db

    @staticmethod
    def _order(db: Session, order_id: str, user: UserContext, *, lock=False) -> Order:
        query = select(Order).where(Order.id == order_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        order = db.scalar(query)
        if order is None:
            raise BusinessError("NOT_FOUND", "订单不存在。", 404)
        _authorize_customer(user, order.customer_id)
        return order

    def _case(
        self,
        db: Session,
        case_id: str,
        user: UserContext,
        *,
        lock=False,
    ) -> tuple[Case, Order]:
        row = db.get(Case, case_id)
        if row is None:
            raise BusinessError("NOT_FOUND", "工单不存在。", 404)
        _authorize_customer(user, row.customer_id)
        # Writers always serialize by order before touching case rows or balances.
        order = self._order(db, row.order_id, user, lock=lock)
        if lock:
            db.refresh(row, with_for_update=True)
        return row, order

    @staticmethod
    def _lines(db: Session, order_id: str) -> list[OrderLine]:
        return list(
            db.scalars(
                select(OrderLine).where(OrderLine.order_id == order_id).order_by(OrderLine.line_id)
            )
        )

    def _order_json(self, db: Session, order: Order) -> dict:
        rows = self._lines(db, order.id)
        items = [
            {**row.data, **_row(row, "paid_cents", "refunded_cents", "quantity")} for row in rows
        ]
        products = list(
            db.scalars(
                select(Product)
                .where(Product.id.in_({row.product_id for row in rows}))
                .order_by(Product.id)
            )
        )
        return {
            **order.data,
            "version": order.version,
            "items": items,
            "products": [row.data for row in products],
            "paid_cents": sum(row.paid_cents for row in rows) if rows else None,
            "refunded_cents": sum(row.refunded_cents for row in rows) if rows else None,
        }

    def orders(self, user: UserContext, *, limit=100, offset=0) -> list[dict]:
        with Session(self.engine) as db:
            query = select(Order)
            if user.role == "customer":
                query = query.where(Order.customer_id == user.customer_id)
            return [
                self._order_json(db, row)
                for row in db.scalars(query.order_by(Order.id).offset(offset).limit(limit)).all()
            ]

    def order(self, order_id: str, user: UserContext) -> dict:
        with Session(self.engine) as db:
            return self._order_json(db, self._order(db, order_id, user))

    def cases(self, user: UserContext, *, limit=100, offset=0) -> list[dict]:
        with Session(self.engine) as db:
            query = select(Case)
            if user.role == "customer":
                query = query.where(Case.customer_id == user.customer_id)
            return [
                _case_json(row)
                for row in db.scalars(
                    query.order_by(Case.created_at.desc(), Case.id).offset(offset).limit(limit)
                )
            ]

    @staticmethod
    def _replay(
        db: Session,
        user: UserContext,
        scope: str,
        key: str,
        request_hash: str,
    ) -> dict | None:
        entry = db.get(Idempotency, (user.user_id, scope, _read_key(key)))
        if entry is None:
            return None
        if entry.request_hash != request_hash:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "同一幂等键已用于不同参数。")
        return entry.response

    @staticmethod
    def _remember(
        db: Session,
        user: UserContext,
        scope: str,
        key: str,
        request_hash: str,
        response: dict,
    ) -> dict:
        db.add(
            Idempotency(
                actor_id=user.user_id,
                scope=scope,
                key=key,
                request_hash=request_hash,
                response=response,
            )
        )
        return response

    def _event(
        self,
        db: Session,
        case: Case,
        event_type: str,
        user: UserContext,
        payload: dict,
        *,
        initial=False,
    ) -> None:
        if not initial:
            case.version += 1
        db.add(
            Event(
                case_id=case.id,
                sequence=case.version,
                type=event_type,
                payload={**payload, "version": case.version, "actor_role": user.role},
                created_at=self.clock(),
            )
        )

    def create_case(
        self,
        user: UserContext,
        *,
        order_id: str,
        intent: str,
        description: str,
        expected_version: int,
        idempotency_key: str,
        line_ids: list[str] | None = None,
    ) -> dict:
        _require_role(user, "customer", "support")
        description = description.strip()
        if intent not in INTENTS or not 5 <= len(description) <= 2000:
            raise BusinessError("INVALID_CASE", "请选择售后类型并填写 5 至 2000 字的描述。", 422)
        if line_ids is not None and (not line_ids or len(set(line_ids)) != len(line_ids)):
            raise BusinessError("INVALID_LINES", "请提供有效且不重复的订单行。", 422)
        scope = f"create:{order_id}"
        signature = digest([intent, description, expected_version, sorted(line_ids or [])])
        with Session(self.engine) as db, db.begin():
            order = self._order(db, order_id, user, lock=True)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            available = {item.line_id for item in self._lines(db, order.id)}
            claimed = sorted(available if line_ids is None else line_ids)
            if not claimed or not set(claimed) <= available:
                raise BusinessError("INVALID_LINES", "所选商品不在订单内。", 422)
            row = self._create_case_for_order(
                db, user, order, intent, description, claimed, expected_version
            )
            return self._remember(db, user, scope, idempotency_key, signature, _case_json(row))

    def _create_case_for_order(
        self,
        db,
        user,
        order,
        intent,
        description,
        claimed,
        expected_version,
        *,
        allow_existing=True,
        allow_closed=True,
    ):
        """Caller holds the order lock and has validated customer and order-line scope."""
        scope_hash = digest(claimed)
        row = db.scalar(
            select(Case).where(
                Case.order_id == order.id, Case.intent == intent, Case.scope_hash == scope_hash
            )
        )
        if row is not None:
            if not allow_existing or not allow_closed and row.status in {"RESOLVED", "CLOSED"}:
                raise BusinessError("CASE_SCOPE_EXISTS", "该商品已有同类工单，请由客服核对。")
            return row
        if expected_version is not None:
            _version(order.version, expected_version)
        row = Case(
            id=_id(),
            ticket_id=_id(),
            order_id=order.id,
            customer_id=order.customer_id,
            intent=intent,
            description=description,
            scope_hash=scope_hash,
            line_ids=claimed,
            status="OPEN",
            version=1,
            materials_version=0,
            facts_version=0,
            facts={},
            created_at=self.clock(),
        )
        db.add(row)
        db.flush()
        self._event(db, row, "CASE_CREATED", user, {"intent": intent}, initial=True)
        return row

    @staticmethod
    def _policy_catalogue(db: Session, at: datetime) -> list[dict]:
        rows = db.scalars(
            select(Policy)
            .where(Policy.effective_at <= at)
            .order_by(
                Policy.id,
                Policy.effective_at.desc(),
                Policy.version,
            )
        )
        latest: dict[str, Policy] = {}
        conflicts: set[str] = set()
        for row in rows:
            prior = latest.setdefault(row.id, row)
            if prior is not row and prior.effective_at == row.effective_at:
                conflicts.add(row.id)
        return [row.data for key, row in latest.items() if key not in conflicts]

    def policy_catalogue(self) -> list[dict]:
        with Session(self.engine) as db:
            return self._policy_catalogue(db, self.clock())

    @staticmethod
    def _all_policy_versions(db: Session) -> list[dict]:
        return [row.data for row in db.scalars(select(Policy).order_by(Policy.id, Policy.version))]

    def all_policy_versions(self) -> list[dict]:
        with Session(self.engine) as db:
            return self._all_policy_versions(db)

    def _case_policies(
        self, db: Session, case: Case, order_data: dict, *, policy_documents=None, decision_at=None
    ) -> tuple[dict, list[dict]]:
        sku_ids = {
            item["product_id"] for item in order_data["items"] if item["line_id"] in case.line_ids
        }
        by_id = {item["id"]: item for item in order_data["products"]}
        products = [by_id.get(sku, {"id": sku}) for sku in sorted(sku_ids)]
        resolution = resolve_policies(
            self._all_policy_versions(db) if policy_documents is None else policy_documents,
            intent=case.intent,
            products=products,
            timestamps={
                "decision_at": self.clock() if decision_at is None else decision_at,
                "case_created_at": utc(case.created_at),
                "order_created_at": order_data.get("created_at"),
                "paid_at": order_data.get("paid_at"),
                "delivered_at": order_data.get("delivered_at"),
            },
        ).to_dict()
        documents = scoped_policy_documents(resolution, case.intent, order_data["status"])
        resolution["blockers"] = sorted(set(resolution["blockers"]))
        resolution["snapshot"] = digest(
            {
                "by_product": resolution["by_product"],
                "blockers": resolution["blockers"],
            }
        )
        return resolution, documents

    @staticmethod
    def _materials(db: Session, case: Case) -> list[Material]:
        return list(
            db.scalars(
                select(Material)
                .where(
                    Material.case_id == case.id,
                )
                .order_by(Material.created_at, Material.id)
            )
        )

    @staticmethod
    def _current_review(db: Session, case: Case) -> MaterialReview | None:
        return db.scalar(
            select(MaterialReview).where(
                MaterialReview.case_id == case.id,
                MaterialReview.materials_version == case.materials_version,
            )
        )

    @staticmethod
    def _ledger(db: Session, order: Order) -> list[Execution]:
        return list(
            db.scalars(
                select(Execution)
                .where(
                    Execution.order_id == order.id,
                )
                .order_by(Execution.created_at, Execution.id)
            )
        )

    @staticmethod
    def _balances(
        db: Session,
        case: Case,
        order: dict,
        ledger: list[Execution],
    ) -> list[dict]:
        reserved: dict[str, int] = {}
        for record in ledger:
            if record.action == "REFUND" and record.status in PENDING_EXECUTION:
                for line in record.lines:
                    line_id = line["line_id"]
                    reserved[line_id] = reserved.get(line_id, 0) + line["amount_cents"]
        stocks = {
            row.product_id: row.available_quantity
            for row in db.scalars(
                select(Inventory).where(
                    Inventory.product_id.in_({line["product_id"] for line in order["items"]})
                )
            )
        }
        return [
            {
                "line_id": item["line_id"],
                "product_id": item["product_id"],
                "available_cents": max(
                    0,
                    item["paid_cents"] - item["refunded_cents"] - reserved.get(item["line_id"], 0),
                ),
                "replace_available_quantity": (
                    min(item["quantity"], stocks[item["product_id"]])
                    if item["product_id"] in stocks
                    else None
                ),
            }
            for item in order["items"]
            if item["line_id"] in case.line_ids
        ]

    @staticmethod
    def _binding(case: Case, order: Order, policy_snapshot: str) -> dict:
        return {
            "order": order.version,
            "materials": case.materials_version,
            "facts": case.facts_version,
            "policies": policy_snapshot,
        }

    def _context(
        self,
        db: Session,
        case: Case,
        order: Order,
        *,
        policy_documents=None,
        decision_at=None,
        include_review_observations=False,
    ) -> dict:
        order_data = self._order_json(db, order)
        resolution, policies = self._case_policies(
            db, case, order_data, policy_documents=policy_documents, decision_at=decision_at
        )
        materials = [_material_json(row) for row in self._materials(db, case)]
        review = self._current_review(db, case)
        ledger = self._ledger(db, order)
        balances = self._balances(db, case, order_data, ledger)
        binding = self._binding(case, order, resolution["snapshot"])
        review_rows = (
            select(MaterialReview)
            .where(MaterialReview.case_id == case.id)
            .order_by(MaterialReview.materials_version)
        )
        if include_review_observations:
            review_rows = review_rows.options(undefer(MaterialReview.observations))
        return {
            "case": _case_json(case),
            "order": order_data,
            "policies": resolution["documents"],
            "policy_resolution": resolution,
            "materials": materials,
            "reviews": [
                {
                    **_review_json(row, include_observations=include_review_observations),
                    "effective": row.materials_version == case.materials_version,
                }
                for row in db.scalars(review_rows)
            ],
            "ledger": [
                _execution_json(row)
                if row.case_id == case.id
                else {
                    **_row(row, "id", "action", "status", "amount_cents", "lines", "created_at"),
                    "receipt": row.receipt,
                }
                for row in ledger
                if row.case_id == case.id
                or (case.intent == "refund_status" and row.action == "REFUND")
            ],
            "versions": {"case": case.version, **binding},
            "eligibility": eligibility(
                case=_case_json(case),
                order=order_data,
                policies=policies,
                materials=materials,
                review=_review_json(review) if review else None,
                facts=case.facts,
                balances=balances,
                policy_blockers=resolution["blockers"],
            ),
            "facts": case.facts,
        }

    def context(
        self, case_id: str, user: UserContext, *, include_review_observations=False
    ) -> dict:
        with self._snapshot() as db:
            case, order = self._case(db, case_id, user)
            return self._context(
                db,
                case,
                order,
                include_review_observations=include_review_observations and user.role == "support",
            )

    def refund_lookup(self, case_id: str, user: UserContext, execution_ids: list[str]) -> dict:
        with self._snapshot() as db:
            try:
                case, order = self._case(db, case_id, user)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
                raise BusinessError("NOT_FOUND", "未找到可查看的退款执行记录。", 404) from None
            if case.intent != "refund_status":
                raise BusinessError(
                    "REFUND_LOOKUP_INTENT_REQUIRED", "请在退款进度工单中查询。", 422
                )
            execution_id = (
                canonical_execution_id(execution_ids[0]) if len(execution_ids) == 1 else None
            )
            if execution_id is None:
                raise BusinessError(
                    "INVALID_REFUND_EXECUTION_ID", "请输入一个完整的退款执行编号。", 422
                )
            rows = db.scalars(
                select(Execution).where(
                    Execution.order_id == order.id,
                    Execution.action == "REFUND",
                    Execution.id == execution_id,
                )
            )
            lookup = locate_refund(
                [
                    _row(row, "id", "action", "status", "amount_cents", "created_at", "receipt")
                    for row in rows
                ],
                execution_id,
            )
            if lookup["status"] != "FOUND":
                raise BusinessError("NOT_FOUND", "未找到可查看的退款执行记录。", 404)
            return {"schema_version": "ac-v1", **lookup}

    def detail(self, case_id: str, user: UserContext) -> dict:
        with self._snapshot() as db:
            case, order = self._case(db, case_id, user)
            data = self._context(
                db, case, order, include_review_observations=user.role == "support"
            )
            if case.intent == "refund_status":
                data["refund_lookup_available"] = True
            proposals = list(
                db.scalars(
                    select(Proposal)
                    .where(
                        Proposal.case_id == case.id,
                    )
                    .order_by(Proposal.version)
                )
            )
            current_binding = {
                key: value for key, value in data["versions"].items() if key != "case"
            }
            valid = {
                row.id
                for row in proposals
                if row.status in {"PROPOSED", "APPROVED"} and row.binding == current_binding
            }
            approvals = list(
                db.scalars(
                    select(Approval)
                    .where(
                        Approval.case_id == case.id,
                    )
                    .order_by(Approval.created_at, Approval.id)
                )
            )
            data["proposals"] = [
                {
                    **_proposal_json(row),
                    "status": (
                        "STALE"
                        if row.status in {"PROPOSED", "APPROVED"} and row.id not in valid
                        else row.status
                    ),
                }
                for row in proposals
            ]
            data["approvals"] = [
                {
                    **_approval_json(row),
                    "effective": (
                        row.id == approvals[-1].id
                        and row.proposal_id in valid
                        and row.decision == "APPROVED"
                        and utc(row.expires_at) > self.clock()
                    ),
                }
                for row in approvals
            ]
            if data["case"]["status"] in {"PROPOSED", "APPROVED"} and not valid:
                data["case"]["status"] = "HUMAN_REVIEW"
            if user.role == "customer":
                for row in data["proposals"] + data["approvals"]:
                    row.pop("notes", None)
                    row.pop("approver_id", None)
            return data

    @staticmethod
    def _editable(case: Case) -> None:
        if case.status in {"EXECUTION_PENDING", "RESOLVED", "EXECUTION_FAILED"}:
            raise BusinessError("CASE_NOT_EDITABLE", "案件已有执行记录，请先查询处理回执。")

    @staticmethod
    def _refresh_status(case: Case, assessment: dict) -> None:
        case.status = "WAITING_INPUT" if assessment["status"] == "WAITING_INPUT" else "HUMAN_REVIEW"

    def upload(
        self,
        case_id: str,
        user: UserContext,
        *,
        kind: str,
        content: bytes,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        return self._upload(
            case_id,
            user,
            kind=kind,
            content=content,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def _upload(
        self,
        case_id,
        user,
        *,
        kind,
        content,
        expected_version,
        idempotency_key,
        chat_source=None,
    ):
        _require_role(user, "customer", "support")
        if kind not in MATERIAL_KINDS:
            raise BusinessError("INVALID_MATERIAL_KIND", "请选择有效的材料类型。", 422)
        # Validate the caller's resource access before decoding any untrusted file.
        with Session(self.engine) as db:
            self._case(db, case_id, user)
        content_type, checksum = validate_image(content)
        object_key = f"cases/{case_id}/{kind}/{checksum}"
        signature = digest([kind, checksum, expected_version])
        scope = f"upload:{case_id}"
        if chat_source is not None:
            _require_role(user, "support")
            chat_source.verify_bytes(checksum, content_type, len(content))
            scope, signature = chat_source.mutation_scope, chat_source.mutation_signature
        with Session(self.engine) as db, db.begin():
            if chat_source is not None:
                chat_source.lock(db)
            case, _ = self._case(db, case_id, user, lock=True)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            if chat_source is not None:
                chat_source.validate(db, case)
            existing = db.scalar(select(Material).where(Material.object_key == object_key))
            if existing is None:
                self._editable(case)
                _version(case.version, expected_version)
            if db.get(ObjectUpload, object_key) is None:
                db.add(
                    ObjectUpload(
                        object_key=object_key,
                        case_id=case.id,
                        sha256=checksum,
                        created_at=self.clock(),
                    )
                )

        # An interruption may leave a private staged object. The deterministic key
        # permits the same content to be retried without creating a second object.
        self.storage.put(object_key, content, content_type)
        verify_object(self.storage.get(object_key, len(content)), len(content), checksum)

        with Session(self.engine) as db, db.begin():
            if chat_source is not None:
                chat_source.lock(db)
            case, order = self._case(db, case_id, user, lock=True)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            if chat_source is not None:
                chat_source.validate(db, case)
            existing = db.scalar(select(Material).where(Material.object_key == object_key))
            if existing is not None:
                response = _material_json(existing)
            else:
                self._editable(case)
                _version(case.version, expected_version)
                material = Material(
                    id=_id(),
                    case_id=case.id,
                    kind=kind,
                    object_key=object_key,
                    sha256=checksum,
                    content_type=content_type,
                    size=len(content),
                    uploader_id=user.user_id,
                    created_at=self.clock(),
                )
                db.add(material)
                case.materials_version += 1
                self._refresh_status(case, self._context(db, case, order)["eligibility"])
                self._event(
                    db,
                    case,
                    "MATERIAL_UPLOADED",
                    user,
                    {
                        "material_id": material.id,
                        "kind": kind,
                        "materials_version": case.materials_version,
                    },
                )
                response = _material_json(material)
            if chat_source is not None:
                response = chat_source.record(db, case, response)
            return self._remember(db, user, scope, idempotency_key, signature, response)

    def _get_material(self, case_id: str, material_id: str, user: UserContext) -> Material:
        with Session(self.engine) as db:
            case, _ = self._case(db, case_id, user)
            row = db.get(Material, material_id)
            if row is None or row.case_id != case.id:
                raise BusinessError("NOT_FOUND", "材料不存在。", 404)
            return row

    def _verified_content(self, row: Material) -> bytes:
        content = self.storage.get(row.object_key, row.size)
        verify_object(content, row.size, row.sha256)
        return content

    def material_content(
        self, case_id: str, material_id: str, user: UserContext
    ) -> tuple[bytes, str]:
        row = self._get_material(case_id, material_id, user)
        return self._verified_content(row), row.content_type

    def material_link(self, case_id: str, material_id: str, user: UserContext) -> dict:
        row = self._get_material(case_id, material_id, user)
        self._verified_content(row)
        return {"url": self.storage.presign(row.object_key, row.content_type), "expires_in": 60}

    def review_materials(
        self,
        case_id: str,
        user: UserContext,
        *,
        decision: str,
        notes: str,
        expected_version: int,
        idempotency_key: str,
        observations: list[dict] | None = None,
    ) -> dict:
        _require_role(user, "support")
        notes = notes.strip()
        if decision not in {"APPROVED", "REJECTED", "NEEDS_MORE"} or not 1 <= len(notes) <= 2000:
            raise BusinessError("INVALID_REVIEW", "请选择材料审核意见并填写理由。", 422)
        observations = _review_observations(observations)
        scope = f"material-review:{case_id}"
        request = [decision, notes, expected_version]
        if observations is not None:
            request.append(observations)
        signature = digest(request)
        with Session(self.engine) as db:
            case, _ = self._case(db, case_id, user)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            self._editable(case)
            _version(case.version, expected_version)
            snapshot = self._materials(db, case)
            if not snapshot:
                raise BusinessError("MATERIALS_REQUIRED", "请先登记材料再审核。")
        for material in snapshot:
            self._verified_content(material)
        with Session(self.engine) as db, db.begin():
            case, order = self._case(db, case_id, user, lock=True)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            self._editable(case)
            _version(case.version, expected_version)
            if self._current_review(db, case) is not None:
                raise BusinessError("MATERIAL_REVIEW_EXISTS", "当前材料版本已有人工审核意见。")
            if observations is not None:
                self._validate_material_review_scope(db, case, order, snapshot, observations)
            if decision == "APPROVED" and case.intent in {"damaged", "wrong_item"}:
                _, documents = self._case_policies(db, case, self._order_json(db, order))
                policies = {row["id"]: row for row in documents}
                policy = policies.get("POL06" if case.intent == "damaged" else "POL13", {})
                required = policy.get("parameters", {}).get("required_photo_kinds", [])
                groups = [{row.sha256 for row in snapshot if row.kind == kind} for kind in required]
                if (
                    not groups
                    or any(not group for group in groups)
                    or len(set().union(*groups)) < len(groups)
                ):
                    raise BusinessError("MATERIALS_REQUIRED", "材料类型未齐或重复使用同一照片。")
            row = MaterialReview(
                id=_id(),
                case_id=case.id,
                materials_version=case.materials_version,
                material_ids=[material.id for material in snapshot],
                decision=decision,
                notes=notes,
                observations=(
                    [{**item, "source": "HUMAN_MATERIAL_REVIEW"} for item in observations]
                    if observations is not None
                    else None
                ),
                reviewer_id=user.user_id,
                created_at=self.clock(),
            )
            db.add(row)
            self._refresh_status(case, self._context(db, case, order)["eligibility"])
            self._event(
                db,
                case,
                "MATERIALS_REVIEWED",
                user,
                {
                    "review_id": row.id,
                    "decision": decision,
                    "materials_version": case.materials_version,
                },
            )
            return self._remember(
                db,
                user,
                scope,
                idempotency_key,
                signature,
                _review_json(row, include_observations=True),
            )

    def _validate_material_review_scope(self, db, case, order, materials, observations):
        material_ids = {row.id for row in materials}
        scoped_lines = set(case.line_ids)
        quantities = {
            row.line_id: row.quantity
            for row in self._lines(db, order.id)
            if row.line_id in scoped_lines
        }
        confirmed = {}
        for item in observations:
            line = item.get("line_id")
            quantity = item.get("affected_quantity")
            if item["material_id"] not in material_ids or (
                line is not None and line not in quantities
            ):
                raise BusinessError(
                    "INVALID_REVIEW_OBSERVATIONS", "材料观察超出当前材料或订单行范围。", 422
                )
            if quantity is not None and (
                quantity > quantities[line] or confirmed.setdefault(line, quantity) != quantity
            ):
                raise BusinessError(
                    "INVALID_REVIEW_OBSERVATIONS",
                    "受影响总数超出订单数量或同一订单行记录不一致。",
                    422,
                )

    def record_facts(
        self,
        case_id: str,
        user: UserContext,
        *,
        facts: dict,
        notes: str,
        expected_version: int,
        idempotency_key: str,
        line_ids: list[str] | None = None,
    ) -> dict:
        _require_role(user, "support", "approver")
        notes = notes.strip()
        if (
            not facts
            or not 1 <= len(notes) <= 2000
            or any(
                key not in FACT_VALUES
                or not isinstance(value, str)
                or value not in FACT_VALUES[key]
                for key, value in facts.items()
            )
        ):
            raise BusinessError("INVALID_FACTS", "请提交可核对的履约、退件或处置状态及说明。", 422)
        required_role = "approver" if "resolution_status" in facts else "support"
        _require_role(user, required_role)
        if required_role == "approver" and len(facts) != 1:
            raise BusinessError("INVALID_FACTS", "处置批准与客服履约复核需分别提交。", 422)
        scope = f"facts:{case_id}"
        signature = digest([facts, notes, expected_version, sorted(line_ids or [])])
        with Session(self.engine) as db, db.begin():
            case, order = self._case(db, case_id, user, lock=True)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            self._editable(case)
            _version(case.version, expected_version)
            if any(case.intent not in FACT_INTENTS[key] for key in facts):
                raise BusinessError("INVALID_FACTS", "当前案件类型不适用这些核对项。", 422)
            returning = bool({"return_receipt_status", "inspection_status"} & facts.keys())
            if line_ids is not None and (
                not returning
                or not line_ids
                or len(set(line_ids)) != len(line_ids)
                or not set(line_ids) <= set(case.line_ids)
            ):
                raise BusinessError("INVALID_LINES", "退件核对项须对应案件中的有效订单行。", 422)
            affected = sorted(line_ids or case.line_ids) if returning else []
            combined = dict(case.facts)
            if returning:
                verified = dict(combined.get("return_lines", {}))
                for line_id in affected:
                    updated = {**verified.get(line_id, {}), **facts}
                    if (
                        updated.get("inspection_status") in {"PASSED", "FAILED"}
                        and updated.get("return_receipt_status") != "RECEIVED"
                    ):
                        raise BusinessError(
                            "INVALID_FACTS",
                            "需先确认对应退件收到才可记录验货结果。",
                            422,
                        )
                    verified[line_id] = updated
                combined["return_lines"] = verified
            else:
                combined.update(facts)
            if "fulfillment_status" in facts or "interception_status" in facts:
                combined["fulfillment_order_version"] = order.version
            if "resolution_status" in facts:
                combined["resolution_materials_version"] = case.materials_version
                combined["resolution_order_version"] = order.version
                resolution, _ = self._case_policies(db, case, self._order_json(db, order))
                combined["resolution_policy_version"] = resolution["snapshot"]
            case.facts = combined
            case.facts_version += 1
            self._refresh_status(case, self._context(db, case, order)["eligibility"])
            self._event(
                db,
                case,
                "FACTS_VERIFIED",
                user,
                {
                    "facts": facts,
                    "line_ids": affected,
                    "audit_notes": notes,
                    "actor_id": user.user_id,
                },
            )
            return self._remember(db, user, scope, idempotency_key, signature, _case_json(case))

    def events(self, case_id: str, user: UserContext, after: int, *, limit=100) -> list[dict]:
        with Session(self.engine) as db:
            case, _ = self._case(db, case_id, user)
            if after > case.version:
                raise BusinessError(
                    "INVALID_EVENT_CURSOR", "事件游标超出当前版本，请刷新案件。", 409
                )
            rows = db.scalars(
                select(Event)
                .where(
                    Event.case_id == case.id,
                    Event.sequence > after,
                )
                .order_by(Event.sequence)
                .limit(limit)
            )
            return [
                {
                    **_row(row, "sequence", "type", "created_at"),
                    "payload": {
                        key: value
                        for key, value in row.payload.items()
                        if user.role != "customer" or key not in {"actor_id", "audit_notes"}
                    },
                }
                for row in rows
            ]

    @staticmethod
    def _normalise_lines(action: str, lines: list[dict]) -> list[dict]:
        if action not in {"REFUND", "REPLACE"} or not 1 <= len(lines) <= 30:
            raise BusinessError("INVALID_PROPOSAL", "请选择有效方案动作及订单行。", 422)
        key = "amount_cents" if action == "REFUND" else "quantity"
        seen = set()
        for row in lines:
            if (
                not isinstance(row, dict)
                or set(row) != {"line_id", key}
                or not isinstance(row["line_id"], str)
                or not 1 <= len(row["line_id"]) <= 64
                or row["line_id"] in seen
                or type(row[key]) is not int
                or not 0 < row[key] <= 2_147_483_647
            ):
                raise BusinessError("INVALID_LINES", "方案订单行或整数金额／数量无效。", 422)
            seen.add(row["line_id"])
        return sorted(lines, key=lambda row: row["line_id"])

    def _validate_plan(
        self,
        db: Session,
        case: Case,
        order: Order,
        action: str,
        lines: list[dict],
        *,
        policy_ids: list[str] | None = None,
        expected_policy_snapshot: str | None = None,
    ) -> dict:
        context = self._context(db, case, order)
        if (
            expected_policy_snapshot is not None
            and context["versions"]["policies"] != expected_policy_snapshot
        ):
            raise BusinessError(
                "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE",
                "调查引用的政策版本与当前适用版本不一致。",
            )
        assessment = context["eligibility"]
        by_id = {row["line_id"]: row for row in assessment["lines"]}
        if not {row["line_id"] for row in lines} <= by_id.keys():
            raise BusinessError("INVALID_LINES", "方案商品超出案件申请范围。", 422)
        if action not in assessment["actions"]:
            raise BusinessError("ACTION_NOT_ELIGIBLE", assessment["reason"])
        allowed_policy_ids = set(assessment["policy_ids_by_action"][action])
        if policy_ids and not set(policy_ids) <= allowed_policy_ids:
            raise BusinessError("POLICY_NOT_APPLICABLE", "方案包含过期或不适用的政策引用。", 422)

        total = 0
        if action == "REFUND":
            if case.intent == "cancel" and any(
                row.status in PENDING_EXECUTION for row in self._ledger(db, order)
            ):
                raise BusinessError("REFUND_PENDING", "全单有退款待核对，请确认原结果后再取消。")
            for row in lines:
                cap = by_id[row["line_id"]]["available_cents"]
                if cap is None or row["amount_cents"] > cap:
                    raise BusinessError("REFUND_CAP_EXCEEDED", "拟退金额超过当前订单行剩余额度。")
                total += row["amount_cents"]
            if total > min(2_147_483_647, assessment["available_cents"]):
                raise BusinessError("REFUND_CAP_EXCEEDED", "拟退合计超过当前订单剩余额度。")
            if total < assessment["available_cents"]:
                if case.intent == "cancel":
                    raise BusinessError(
                        "CANCEL_REQUIRES_FULL_BALANCE", "取消退款需覆盖全单剩余额度。"
                    )
                if (
                    case.facts.get("resolution_status") != "APPROVED"
                    or case.facts.get("resolution_materials_version") != case.materials_version
                    or case.facts.get("resolution_order_version") != order.version
                    or case.facts.get("resolution_policy_version")
                    != context["versions"]["policies"]
                ):
                    raise BusinessError(
                        "PARTIAL_RESOLUTION_REQUIRED", "部分退款须先获得本版本处置决定。"
                    )
        else:
            quantities: dict[str, int] = {}
            for row in lines:
                balance = by_id[row["line_id"]]
                if row["quantity"] > (balance["replace_available_quantity"] or 0):
                    raise BusinessError(
                        "STOCK_INSUFFICIENT", "拟换发数量超过订单行数量或可用库存。"
                    )
                sku = balance["product_id"]
                quantities[sku] = quantities.get(sku, 0) + row["quantity"]
            for sku, quantity in quantities.items():
                inventory = db.get(Inventory, sku)
                if inventory is None or inventory.available_quantity < quantity:
                    raise BusinessError(
                        "STOCK_INSUFFICIENT", "同一商品的合计换发数量超过可用库存。"
                    )
        binding = {key: value for key, value in context["versions"].items() if key != "case"}
        return {
            "amount_cents": total,
            "binding": binding,
            "policy_ids": sorted(allowed_policy_ids),
        }

    def validate_draft(
        self,
        case_id: str,
        user: UserContext,
        *,
        expected_version: int,
        action: str,
        lines: list[dict],
        policy_ids: list[str],
    ) -> dict:
        """Validate a candidate against trusted facts without creating a proposal."""
        lines = self._normalise_lines(action, lines)
        with self._snapshot() as db:
            case, order = self._case(db, case_id, user)
            self._editable(case)
            _version(case.version, expected_version)
            self._reject_duplicate_matter(db, case, order, lines)
            return self._validate_plan(db, case, order, action, lines, policy_ids=policy_ids)

    def propose(
        self,
        case_id: str,
        user: UserContext,
        *,
        expected_version: int,
        idempotency_key: str,
        action: str,
        lines: list[dict],
        notes: str,
        policy_ids: list[str],
    ) -> dict:
        from aftercare.conversations.decisions import ConversationDecisions, lock_case

        _require_role(user, "support")
        notes = notes.strip()
        if not 1 <= len(notes) <= 2000:
            raise BusinessError("INVALID_PROPOSAL", "请填写方案说明。", 422)
        lines = self._normalise_lines(action, lines)
        scope = f"proposal:{case_id}"
        signature = digest([expected_version, action, lines, notes, sorted(set(policy_ids))])
        with Session(self.engine) as db, db.begin():
            case, order, conversation = lock_case(self, db, case_id, user)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            self._editable(case)
            _version(case.version, expected_version)
            self._reject_duplicate_matter(db, case, order, lines)
            plan = self._validate_plan(db, case, order, action, lines, policy_ids=policy_ids)
            if case.decision_conversation_id is not None:
                plan["decision_binding"] = ConversationDecisions(self).accepted(
                    db,
                    user,
                    case,
                    order,
                    conversation,
                    action=action,
                    lines=lines,
                    policy_ids=plan["policy_ids"],
                )
            for old in db.scalars(
                select(Proposal).where(
                    Proposal.case_id == case.id,
                    Proposal.status.in_({"PROPOSED", "APPROVED"}),
                )
            ):
                old.status = "SUPERSEDED"
            version = case.version + 1
            payload = {
                "case_id": case.id,
                "version": version,
                "action": action,
                "lines": lines,
                "notes": notes,
                **plan,
            }
            row = Proposal(
                id=_id(),
                **payload,
                payload_hash=digest(payload),
                status="PROPOSED",
                proposer_id=user.user_id,
                created_at=self.clock(),
            )
            db.add(row)
            case.status = "PROPOSED"
            self._event(
                db,
                case,
                "PROPOSAL_CREATED",
                user,
                {
                    "proposal_id": row.id,
                    "proposal_version": row.version,
                    "actor_id": user.user_id,
                },
            )
            return self._remember(db, user, scope, idempotency_key, signature, _proposal_json(row))

    @staticmethod
    def _proposal(db: Session, case: Case, proposal_id: str) -> Proposal:
        row = db.get(Proposal, proposal_id)
        if row is None or row.case_id != case.id:
            raise BusinessError("NOT_FOUND", "方案不存在。", 404)
        return row

    def _check_proposal(
        self,
        db: Session,
        case: Case,
        order: Order,
        row: Proposal,
        user: UserContext,
        conversation,
    ) -> None:
        resolution, _ = self._case_policies(db, case, self._order_json(db, order))
        if row.status not in {"PROPOSED", "APPROVED"} or row.binding != self._binding(
            case,
            order,
            resolution["snapshot"],
        ):
            raise BusinessError("STALE_PROPOSAL", "订单、政策、事实或材料已变更，请重新提交方案。")
        current = self._validate_plan(
            db, case, order, row.action, row.lines, policy_ids=row.policy_ids
        )
        from aftercare.conversations.decisions import check_proposal

        binding = check_proposal(self, db, user, case, order, conversation, row)
        if binding is not None:
            current["decision_binding"] = binding
        payload = {
            "case_id": row.case_id,
            "version": row.version,
            "action": row.action,
            "lines": row.lines,
            "notes": row.notes,
            **current,
        }
        if row.payload_hash != digest(payload):
            raise BusinessError("STALE_PROPOSAL", "当前可执行方案与原版本不一致。")

    @staticmethod
    def _last_approval(db: Session, row: Proposal) -> Approval | None:
        return db.scalar(
            select(Approval)
            .where(Approval.proposal_id == row.id)
            .order_by(
                Approval.created_at.desc(),
                Approval.id.desc(),
            )
            .limit(1)
        )

    def approve(
        self,
        case_id: str,
        user: UserContext,
        *,
        expected_version: int,
        idempotency_key: str,
        proposal_id: str,
        decision: str,
        notes: str,
    ) -> dict:
        from aftercare.conversations.decisions import lock_case

        _require_role(user, "approver")
        notes = notes.strip()
        if decision not in {"APPROVED", "REJECTED"} or not 1 <= len(notes) <= 2000:
            raise BusinessError("INVALID_APPROVAL", "请选择审批结果并填写理由。", 422)
        scope = f"approval:{case_id}"
        signature = digest([expected_version, proposal_id, decision, notes])
        with Session(self.engine) as db, db.begin():
            case, order, conversation = lock_case(self, db, case_id, user)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return replay
            self._editable(case)
            _version(case.version, expected_version)
            row = self._proposal(db, case, proposal_id)
            if row.proposer_id == user.user_id:
                raise BusinessError("SEPARATION_REQUIRED", "提交人与审批人必须分开。", 403)
            self._check_proposal(db, case, order, row, user, conversation)
            at = self.clock()
            last = self._last_approval(db, row)
            if (
                last is not None
                and utc(last.expires_at) > at
                and last.decision == "APPROVED"
                and decision == "APPROVED"
            ):
                raise BusinessError("APPROVAL_EXISTS", "已有有效批准。")
            approval = Approval(
                id=_id(),
                case_id=case.id,
                proposal_id=row.id,
                proposal_version=row.version,
                payload_hash=row.payload_hash,
                decision=decision,
                notes=notes,
                approver_id=user.user_id,
                created_at=at,
                expires_at=at + APPROVAL_TTL,
            )
            db.add(approval)
            row.status = decision
            case.status = decision
            self._event(
                db,
                case,
                "PROPOSAL_DECIDED",
                user,
                {
                    "proposal_id": row.id,
                    "approval_id": approval.id,
                    "decision": decision,
                    "actor_id": user.user_id,
                },
            )
            return self._remember(
                db, user, scope, idempotency_key, signature, _approval_json(approval)
            )

    @staticmethod
    def _existing_execution(db: Session, case: Case, proposal: Proposal) -> Execution | None:
        row = db.scalar(select(Execution).where(Execution.case_id == case.id))
        if row is not None and row.payload_hash != proposal.payload_hash:
            raise BusinessError("DUPLICATE_MATTER", "同一案件已有处置账本，请先查询原回执。")
        return row

    @staticmethod
    def _reject_duplicate_matter(db: Session, case: Case, order: Order, lines: list[dict]) -> None:
        claimed = {line["line_id"] for line in lines}
        executions = db.scalars(
            select(Execution)
            .join(Case, Case.id == Execution.case_id)
            .where(
                Execution.order_id == order.id,
                Case.intent == case.intent,
                Case.id != case.id,
            )
        )
        if any(claimed & {line["line_id"] for line in prior.lines} for prior in executions):
            raise BusinessError(
                "DUPLICATE_MATTER", "相同类型和商品已有处置账本，请查询原处理记录。"
            )

    @staticmethod
    def _reserve_stock(db: Session, order: Order, lines: list[dict]) -> None:
        products = {row.line_id: row.product_id for row in BusinessService._lines(db, order.id)}
        quantities: dict[str, int] = {}
        for line in lines:
            sku = products[line["line_id"]]
            quantities[sku] = quantities.get(sku, 0) + line["quantity"]
        inventory = list(
            db.scalars(
                select(Inventory)
                .where(
                    Inventory.product_id.in_(quantities),
                )
                .order_by(Inventory.product_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(inventory) != len(quantities) or any(
            row.available_quantity < quantities[row.product_id] for row in inventory
        ):
            raise BusinessError("STOCK_INSUFFICIENT", "执行前库存已变更，请重新核对。")
        for row in inventory:
            row.available_quantity -= quantities[row.product_id]

    def execute(
        self,
        case_id: str,
        user: UserContext,
        *,
        expected_version: int,
        idempotency_key: str,
        proposal_id: str,
    ) -> dict:
        from aftercare.conversations.decisions import lock_case

        _require_role(user, "support")
        scope = f"execute:{case_id}"
        signature = digest([expected_version, proposal_id])
        with Session(self.engine) as db:
            case, _ = self._case(db, case_id, user)
            proposal = self._proposal(db, case, proposal_id)
            self._replay(db, user, scope, idempotency_key, signature)
            existing = self._existing_execution(db, case, proposal)
            if existing is None:
                _version(case.version, expected_version)
                if proposal.status != "APPROVED":
                    raise BusinessError("APPROVAL_REQUIRED", "当前方案需要有效人工批准。")
                snapshot = (
                    self._materials(db, case) if case.intent in {"wrong_item", "damaged"} else []
                )
            else:
                snapshot = []
        for material in snapshot:
            self._verified_content(material)

        with Session(self.engine) as db, db.begin():
            case, order, conversation = lock_case(self, db, case_id, user)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            proposal = self._proposal(db, case, proposal_id)
            existing = self._existing_execution(db, case, proposal)
            if existing is not None:
                result = _execution_json(existing)
                if replay is None:
                    self._remember(db, user, scope, idempotency_key, signature, result)
                return result
            self._editable(case)
            _version(case.version, expected_version)
            self._reject_duplicate_matter(db, case, order, proposal.lines)
            self._check_proposal(db, case, order, proposal, user, conversation)
            approval = self._last_approval(db, proposal)
            at = self.clock()
            if (
                approval is None
                or approval.decision != "APPROVED"
                or approval.payload_hash != proposal.payload_hash
                or approval.proposal_version != proposal.version
                or utc(approval.expires_at) <= at
            ):
                raise BusinessError("APPROVAL_REQUIRED", "当前方案需要有效人工批准。")
            business_key = digest([case.id, proposal.action])
            row = Execution(
                id=_id(),
                business_key=business_key,
                case_id=case.id,
                order_id=order.id,
                proposal_id=proposal.id,
                approval_id=approval.id,
                payload_hash=proposal.payload_hash,
                action=proposal.action,
                lines=proposal.lines,
                amount_cents=proposal.amount_cents,
                status="SUBMITTING",
                receipt=None,
                actor_id=user.user_id,
                created_at=at,
                updated_at=at,
            )
            db.add(row)
            if row.action == "REPLACE":
                self._reserve_stock(db, order, row.lines)
                row.status = "SUCCEEDED"
                row.receipt = {
                    "id": f"sim-stock-{business_key}",
                    "mode": "SIMULATED",
                    "result": "STOCK_RECORDED",
                    "lines": row.lines,
                }
                proposal.status = "EXECUTED"
                case.status = "RESOLVED"
            else:
                case.status = "EXECUTION_PENDING"
            self._event(
                db,
                case,
                "EXECUTION_RECORDED",
                user,
                {
                    "execution_id": row.id,
                    "action": row.action,
                    "status": row.status,
                    "actor_id": user.user_id,
                },
            )
            result = self._remember(
                db,
                user,
                scope,
                idempotency_key,
                signature,
                _execution_json(row),
            )
            execution_id, payload_hash, amount = row.id, row.payload_hash, row.amount_cents

        if result["status"] == "SUCCEEDED":
            return result
        # Provider submission occurs after the reservation has durably committed.
        # Only the transaction that created the reservation reaches this point.
        try:
            outcome = self.payments.submit(business_key, payload_hash, amount)
        except (TimeoutError, OSError):
            outcome = PaymentResult("UNKNOWN")
        with Session(self.engine) as db, db.begin():
            case, order = self._case(db, case_id, user, lock=True)
            row = db.get_one(Execution, execution_id)
            return self._apply_payment_result(db, case, order, row, user, outcome)

    def _apply_payment_result(
        self,
        db: Session,
        case: Case,
        order: Order,
        row: Execution,
        user: UserContext,
        outcome: PaymentResult,
    ) -> dict:
        if row.status not in PENDING_EXECUTION:
            return _execution_json(row)
        receipt = outcome.receipt
        valid_receipt = (
            isinstance(receipt, dict)
            and receipt.get("mode") == "SIMULATED"
            and receipt.get("amount_cents") == row.amount_cents
            and bool(receipt.get("id"))
        )
        status = (
            outcome.status
            if valid_receipt and outcome.status in {"SUCCEEDED", "FAILED"}
            else "UNKNOWN"
        )
        if status == row.status:
            return _execution_json(row)
        if status == "SUCCEEDED":
            order_lines = {item.line_id: item for item in self._lines(db, order.id)}
            for line in row.lines:
                order_lines[line["line_id"]].refunded_cents += line["amount_cents"]
            order.version += 1
            if case.intent == "cancel" and all(
                line.refunded_cents == line.paid_cents for line in order_lines.values()
            ):
                order.data = {
                    **order.data,
                    "status": "CANCELLED",
                    "cancelled_at": _stamp(self.clock()),
                }
        row.status = status
        row.receipt = receipt if status in {"SUCCEEDED", "FAILED"} else None
        row.updated_at = self.clock()
        case.status = {
            "SUCCEEDED": "RESOLVED",
            "FAILED": "EXECUTION_FAILED",
            "UNKNOWN": "EXECUTION_PENDING",
        }[status]
        if status in {"SUCCEEDED", "FAILED"}:
            proposal = db.get_one(Proposal, row.proposal_id)
            proposal.status = "EXECUTED" if status == "SUCCEEDED" else "EXECUTION_FAILED"
        self._event(
            db,
            case,
            "EXECUTION_UPDATED",
            user,
            {
                "execution_id": row.id,
                "status": status,
                "actor_id": user.user_id,
            },
        )
        return _execution_json(row)

    def reconcile(
        self,
        case_id: str,
        execution_id: str,
        user: UserContext,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        _require_role(user, "support")
        scope = f"reconcile:{case_id}:{execution_id}"
        signature = digest([expected_version])
        with Session(self.engine) as db:
            case, _ = self._case(db, case_id, user)
            row = db.get(Execution, execution_id)
            if row is None or row.case_id != case.id:
                raise BusinessError("NOT_FOUND", "执行记录不存在。", 404)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return _execution_json(row)
            _version(case.version, expected_version)
            lookup = (
                (row.business_key, row.payload_hash, row.amount_cents)
                if row.action == "REFUND" and row.status in PENDING_EXECUTION
                else None
            )
        outcome = PaymentResult("UNKNOWN")
        if lookup is not None:
            try:
                outcome = self.payments.lookup(*lookup)
            except (TimeoutError, OSError):
                pass

        with Session(self.engine) as db, db.begin():
            case, order = self._case(db, case_id, user, lock=True)
            row = db.get_one(Execution, execution_id)
            replay = self._replay(db, user, scope, idempotency_key, signature)
            if replay is not None:
                return _execution_json(row)
            if row.status in PENDING_EXECUTION:
                _version(case.version, expected_version)
            result = self._apply_payment_result(db, case, order, row, user, outcome)
            return self._remember(db, user, scope, idempotency_key, signature, result)
