"""Construct a clean local P5 source for each development case, without a model or HTTP."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.models import Approval, Case, Execution, Proposal
from aftercare.business.rules import digest
from aftercare.business.seed import initialize
from aftercare.business.service import BusinessService
from aftercare.business.storage import verify_object
from aftercare.conversations.materials import candidates, confirm
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import Conversation
from aftercare.db import Base, User
from aftercare.evaluation.chat.runner import FrozenCase

ROOT = Path(__file__).resolve().parents[3]
PACK_PATH = "data/chat-eval-v1/development/cases.json"
OWNER = UserContext(user_id="owner", role="customer", customer_id="C001")
SUPPORT = UserContext(user_id="support", role="support")
APPROVER = UserContext(user_id="approver", role="approver")


def load_development_cases(root=ROOT):
    return json.loads((root / PACK_PATH).read_bytes())


def _images(root, pack, case):
    path = root / pack["asset_manifest"]
    manifest = path.read_bytes()
    if sha256(manifest).hexdigest() != pack["asset_manifest_sha256"]:
        raise ValueError("Development asset manifest changed")
    assets = {row["id"]: row for row in json.loads(manifest)["assets"]}
    for asset_id in case["attachments"]:
        row = assets[asset_id]
        if row["source_kind"] != "AI_GENERATED_SYNTHETIC":
            raise ValueError("Development fixture requires synthetic images")
        content = path.with_name(row["file"]).read_bytes()
        verify_object(content, row["bytes"], row["sha256"])
        yield row["file"], content


def _seed(root, pack, case, decision_at):
    specs = [case]
    if "additional_line" in case:
        specs.append(case["additional_line"])
    items = []
    for index, spec in enumerate(specs, 1):
        paid = spec["quantity"] * spec["unit_paid_cents"]
        items.append(
            {
                "line_id": f"L{index:02d}",
                "product_id": f"DEV-MUG-{index}",
                "title": ("白色马克杯", "带盖马克杯")[index - 1],
                "package_id": spec["package_id"],
                "quantity": spec["quantity"],
                "unit_price_cents": spec["unit_paid_cents"],
                "discount_cents": 0,
                "payable_cents": paid,
                "paid_cents": paid,
                "refunded_cents": case.get("history", {}).get("amount_cents", 0)
                if index == 1
                else 0,
            }
        )
    paid = sum(item["paid_cents"] for item in items)
    refunded = sum(item["refunded_cents"] for item in items)
    order = {
        "id": case["order_id"],
        "customer_id": OWNER.customer_id,
        "status": "DELIVERED",
        "payment_status": "CONFIRMED",
        "created_at": (decision_at - timedelta(days=6)).isoformat(),
        "paid_at": (decision_at - timedelta(days=5)).isoformat(),
        "shipped_at": (decision_at - timedelta(days=4)).isoformat(),
        "delivered_at": (decision_at - timedelta(days=3)).isoformat(),
        "cancelled_at": None,
        "subtotal_cents": paid,
        "discount_cents": 0,
        "payable_cents": paid,
        "paid_cents": paid,
        "refunded_cents": refunded,
        "items": items,
    }
    policy_data = (root / pack["policy_source"]).read_bytes()
    if sha256(policy_data).hexdigest() != pack["policy_source_sha256"]:
        raise ValueError("Development policy source changed")
    baseline = json.loads(policy_data)
    return {
        "customers": [{"id": OWNER.customer_id, "name": "合成客户"}],
        "products": [
            {
                "id": item["product_id"],
                "name": item["title"],
                "category": "家居",
                "unit_price_cents": item["unit_price_cents"],
                "returnable": True,
            }
            for item in items
        ],
        "orders": [order],
        "shipments": [],
        "policies": baseline["policies"],
    }


def _seed_history(engine, case, decision_at):
    """Import simulated prior business records directly, without invoking payment execution."""
    history = case["history"]
    if history["provenance"] != "SIMULATED":
        raise ValueError("Only simulated development history is allowed")
    case_id = "40000000-0000-4000-8000-000000000001"
    proposal_id = "40000000-0000-4000-8000-000000000002"
    approval_id = "40000000-0000-4000-8000-000000000004"
    stamp = decision_at - timedelta(days=2)
    lines = [{"line_id": history["line_id"], "amount_cents": history["amount_cents"]}]
    checksum = digest(lines)
    notes = "SIMULATED 历史导入；先前一件受损商品的退款"
    with Session(engine) as db, db.begin():
        db.add(
            Case(
                id=case_id,
                ticket_id=case_id,
                order_id=case["order_id"],
                customer_id=OWNER.customer_id,
                intent="damaged",
                description=notes,
                scope_hash=digest(["historic", lines]),
                line_ids=[history["line_id"]],
                status="COMPLETED",
                created_at=stamp,
            )
        )
        db.flush()
        db.add(
            Proposal(
                id=proposal_id,
                case_id=case_id,
                version=1,
                action="REFUND",
                lines=lines,
                notes=notes,
                policy_ids=["POL14"],
                binding={},
                payload_hash=checksum,
                amount_cents=history["amount_cents"],
                status="EXECUTED",
                proposer_id=SUPPORT.user_id,
                created_at=stamp,
            )
        )
        db.flush()
        db.add(
            Approval(
                id=approval_id,
                case_id=case_id,
                proposal_id=proposal_id,
                proposal_version=1,
                payload_hash=checksum,
                decision="APPROVED",
                notes=notes,
                approver_id=APPROVER.user_id,
                expires_at=stamp + timedelta(days=1),
                created_at=stamp,
            )
        )
        db.flush()
        db.add(
            Execution(
                id=history["execution_id"],
                business_key=digest(["SIMULATED", history]),
                case_id=case_id,
                order_id=case["order_id"],
                proposal_id=proposal_id,
                approval_id=approval_id,
                payload_hash=checksum,
                action="REFUND",
                lines=lines,
                amount_cents=history["amount_cents"],
                status="SUCCEEDED",
                receipt={
                    "id": "SIMULATED-HISTORICAL-REFUND",
                    "provenance": "SIMULATED",
                    "quantity": history["quantity"],
                    "package_id": history["package_id"],
                },
                actor_id=SUPPORT.user_id,
                created_at=stamp,
                updated_at=stamp,
            )
        )


def replay_development_case(engine, storage, settings, root, case_id, *, repetition=1, fresh):
    """Populate an isolated empty source or read back an already admitted source."""
    if settings.profile != "test" or settings.model_enabled:
        raise ValueError("Development replay requires test and offline settings")
    pack = load_development_cases(root)
    case = next(item for item in pack["cases"] if item["id"] == case_id)
    files = tuple(_images(root, pack, case))
    decision_at = datetime.fromisoformat(pack["decision_at"])
    seed = _seed(root, pack, case, decision_at)
    business = BusinessService(engine, settings, storage=storage, clock=lambda: decision_at)
    if fresh:
        if settings.chat_analysis_enabled:
            raise ValueError("Development replay must not schedule automatic analysis")
        initialize(engine, seed)
        if "history" in case:
            _seed_history(engine, case, decision_at)
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    id=SUPPORT.user_id,
                    display_name="合成验收客服",
                    role="support",
                    enabled=True,
                    token_hash=digest(["development-not-a-credential", SUPPORT.user_id]),
                )
            )
        chat = ConversationService(business)
        ident = chat.create(OWNER, "development-chat-create")["id"]
        posted = chat.message(
            ident,
            OWNER,
            "40000000-0000-4000-8000-000000000005",
            case["customer_message"],
            files,
            "development-chat-originals",
        )
        linked = chat.context(
            ident,
            OWNER,
            order_id=case["order_id"],
            line_ids=[item["line_id"] for item in seed["orders"][0]["items"]],
            new_issue=False,
            expected_revision=posted["conversation"]["revision"],
            key="development-chat-context",
        )
        case_id_db = linked["conversation"]["active_case_id"]
        issue = candidates(business, SUPPORT, ident)
        for kind, original in zip(("ITEM", "PARCEL"), issue["items"], strict=True):
            result = confirm(
                business,
                SUPPORT,
                ident,
                attachment_id=original["attachment_id"],
                kind=kind,
                case_id=case_id_db,
                issue_id=issue["issue_id"],
                version=issue["materials_version"],
                key=f"development-confirm-{kind}",
            )
            issue["materials_version"] = result["materials_version"]
        business.review_materials(
            case_id_db,
            SUPPORT,
            decision="APPROVED",
            notes=case["review"],
            expected_version=business.context(case_id_db, SUPPORT)["case"]["version"],
            idempotency_key="development-material-review",
        )
    with Session(engine) as db:
        conversation = db.scalars(select(Conversation)).one()
        if conversation.active_order_id != case["order_id"]:
            raise ValueError("Development source order binding mismatch")
        return FrozenCase(
            case_id,
            repetition,
            business,
            SUPPORT,
            conversation.id,
            conversation.analysis_issue_id,
            decision_at,
            f"{pack['provenance']}:{pack['source_family']}:{digest(pack)}",
        )


@contextmanager
def development_source(directory, case_id, *, repetition=1):
    from test_business import MemoryObjects

    database = directory / f"{case_id}-{repetition}.sqlite"
    database.touch(mode=0o600, exist_ok=False)
    engine = create_engine(f"sqlite:///{database}")

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        Base.metadata.create_all(engine)
        settings = SimpleNamespace(
            profile="test",
            model_enabled=False,
            embedding_enabled=False,
            chat_analysis_enabled=False,
            chat_vision_enabled=False,
            chat_decisions_enabled=True,
            chat_investigation_enabled=True,
        )
        yield replay_development_case(
            engine,
            MemoryObjects(),
            settings,
            ROOT,
            case_id,
            repetition=repetition,
            fresh=True,
        )
    finally:
        engine.dispose()
