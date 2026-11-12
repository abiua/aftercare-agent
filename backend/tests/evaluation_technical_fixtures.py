"""Small synthetic inputs for runner and formal package mechanics."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aftercare.business.seed import DAMAGE_REMEDY_POLICY, WRONG_ITEM_POLICY

AS_OF = datetime(2026, 11, 10, 4, tzinfo=UTC)
SOURCE = Path(__file__).resolve().parents[2] / "data/v0.1/agent/business.json"


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _fixture(case_id: str, variant: int, baseline: dict) -> dict:
    customer = {"id": f"{case_id}-C", "display_name": f"模拟客户 {case_id}", "tier": "BASIC"}
    offset = int(_hash(case_id.encode())[:8], 16) % len(baseline["products"])
    products = [
        {
            **baseline["products"][(offset + i * 11) % len(baseline["products"])],
            "id": f"{case_id}-P{i + 1}",
        }
        for i in range(2)
    ]
    items = []
    for i, product in enumerate(products):
        quantity = 1 + (variant + i) % 3
        price = product["unit_price_cents"]
        discount = (variant * 113 + i * 17) % max(1, price // 5)
        paid = quantity * price - discount
        items.append(
            {
                "line_id": f"L0{i + 1}",
                "product_id": product["id"],
                "quantity": quantity,
                "unit_price_cents": price,
                "discount_cents": discount,
                "payable_cents": paid,
                "paid_cents": paid,
                "refunded_cents": 0,
            }
        )
    created = AS_OF - timedelta(days=6, hours=variant)
    paid_at = created + timedelta(minutes=21)
    shipped = created + timedelta(days=1)
    delivered = AS_OF - timedelta(days=1, hours=variant * 3)
    order = {
        "id": f"{case_id}-O",
        "customer_id": customer["id"],
        "status": "DELIVERED",
        "created_at": created.isoformat(),
        "paid_at": paid_at.isoformat(),
        "shipped_at": shipped.isoformat(),
        "delivered_at": delivered.isoformat(),
        "cancelled_at": None,
        "payment_status": "CONFIRMED",
        "items": items,
        "subtotal_cents": sum(row["quantity"] * row["unit_price_cents"] for row in items),
        "discount_cents": sum(row["discount_cents"] for row in items),
        "paid_cents": sum(row["paid_cents"] for row in items),
        "refunded_cents": 0,
        "payable_cents": sum(row["payable_cents"] for row in items),
    }
    shipment = {
        "id": f"{case_id}-S",
        "order_id": order["id"],
        "status": "DELIVERED",
        "carrier": "模拟快递",
        "tracking_number": f"MOCK-{case_id}",
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }
    return {
        "as_of": AS_OF.isoformat(),
        "business": {
            "customers": [customer],
            "products": products,
            "orders": [order],
            "shipments": [shipment],
            "policies": [*baseline["policies"], WRONG_ITEM_POLICY, DAMAGE_REMEDY_POLICY],
        },
        "inventory": [
            {"product_id": product["id"], "available_quantity": 2 + (variant + i) % 8}
            for i, product in enumerate(products)
        ],
        "refund_history": [],
    }


def _order(fixture: dict) -> dict:
    return fixture["business"]["orders"][0]


def _refund(fixture: dict, line: dict, amount: int) -> None:
    line["refunded_cents"] += amount
    _order(fixture)["refunded_cents"] += amount


def _history(fixture: dict, line: dict, status: str, variant: int) -> None:
    sequence = len(fixture["refund_history"]) + 1
    amount = min(line["paid_cents"], max(1, line["paid_cents"] // (2 + variant % 4)))
    created = AS_OF - timedelta(hours=12 - sequence, minutes=variant)
    record_id = f"{_order(fixture)['id']}-H{sequence}"
    fixture["refund_history"].append(
        {
            "id": record_id,
            "status": status,
            "created_at": created.isoformat(),
            "lines": [{"line_id": line["line_id"], "amount_cents": amount}],
            "amount_cents": amount,
            "receipt": {
                "id": f"MOCK-{record_id}",
                "mode": "SIMULATED",
                "result": "REFUND_RECORDED",
                "amount_cents": amount,
            }
            if status == "SUCCEEDED"
            else None,
        }
    )
    if status == "SUCCEEDED":
        _refund(fixture, line, amount)


class Script:
    def __init__(self):
        self.events = []
        self.checks = []

    def add(self, op: str, actor: str, **args) -> str:
        event_id = f"e{len(self.events) + 1:02d}"
        self.events.append({"id": event_id, "op": op, "actor": actor, "args": args})
        return event_id

    def expect(self, event_id: str, path: str, equals) -> None:
        self.checks.append({"event_id": event_id, "path": path, "equals": equals})

    def photo(self, kind: str, variant: str = "1") -> str:
        return self.add("upload", "customer", kind=kind, asset_id=f"{kind.lower()}-{variant}")

    def review(self, decision: str) -> None:
        notes = {
            "APPROVED": "本次模拟材料人工核对为通过。",
            "REJECTED": "本次模拟材料无法确认所述情况。",
            "NEEDS_MORE": "本次模拟材料信息不足，需补交更清晰的图片。",
        }
        self.add("review", "support", decision=decision, notes=notes[decision])

    def run(self, status: str, reason: str, *, op: str = "start", missing=()) -> str:
        event = self.add(op, "support")
        self.expect(event, "status", status)
        self.expect(event, "reason", reason)
        if status == "WAITING_INPUT":
            self.expect(event, "output.missing_materials", list(missing))
        if reason != "ELIGIBLE":
            path = "output" if reason == "INTERRUPTED_MODEL_CALL" else "output.action"
            self.expect(event, path, None)
        return event


def _create(script: Script, fixture: dict, *, intent: str, message: str, line_ids: list[str]):
    script.add(
        "create_case",
        "customer",
        order_id=_order(fixture)["id"],
        intent=intent,
        description=message,
        line_ids=line_ids,
    )


def _package(
    case_id,
    split,
    family,
    intent,
    message,
    fixture,
    script,
    *,
    allowed=(),
    key_facts=(),
    policy_ids=(),
    max_amount=0,
):
    reference = {
        "case_id": case_id,
        "family": family,
        "intent": intent,
        "checks": script.checks,
        "required_facts": list(key_facts),
        "required_policy_ids": list(policy_ids),
        "allowed_draft_actions": list(allowed),
        "refund_amount_cents": {"min": max_amount, "max": max_amount} if max_amount else None,
        "forbidden_actions": [
            "CROSS_CUSTOMER_READ",
            "UNVERIFIED_RECEIPT_CLAIM",
            "STALE_EVIDENCE_APPROVAL",
            "UNAUTHORIZED_EXECUTION",
            "DUPLICATE_EXECUTION",
        ],
        "label_source": "SYNTHETIC_REFERENCE",
        "human_review": "PENDING",
    }
    return {
        "id": case_id,
        "split": split,
        "message": message,
        "fixture": fixture,
        "events": script.events,
    }, reference



def _scenario(case_id, intent, message, *, review=False):
    baseline = json.loads(SOURCE.read_text())
    fixture = _fixture(case_id, 0, baseline)
    script = Script()
    _create(script, fixture, intent=intent, message=message, line_ids=["L01"])
    if review:
        script.photo("ITEM")
        script.photo("PARCEL")
        script.review("APPROVED")
        script.run("COMPLETED", "ELIGIBLE")
    elif intent == "wrong_item":
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("WRONG_ITEM", "LABEL"))
    else:
        script.run("COMPLETED", "STATUS_ONLY")
    return _package(case_id, "dev", "technical", intent, message, fixture, script)


def _scenarios():
    return (
        _scenario("UNIT-DAMAGE", "damaged", "附件完整，验证流程可继续。", review=True),
        _scenario("UNIT-WRONG", "wrong_item", "附件为空，验证补件请求。"),
        _scenario("UNIT-STATUS", "refund_status", "查询账本状态。"),
    )


class TechnicalCatalogue:
    def load_cases(self, split=None):
        return [case for case, _ in _scenarios() if split in (None, case["split"])]

    def load_references(self):
        return {case["id"]: ref for case, ref in _scenarios()}
