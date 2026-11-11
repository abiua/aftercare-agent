from datetime import datetime
from typing import cast

from sqlalchemy import Engine, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql.selectable import FromClause

from .errors import BusinessError
from .models import Customer, Inventory, Order, OrderLine, Policy, Product

WRONG_ITEM_POLICY: dict = {
    "id": "POL13",
    "version": "1.0",
    "title": "错发举证与换发",
    "text": "签收后发现错发时提交实物照和包裹标签照。人工审核确认错发后，按原商品可用库存审核换发；退款按已付款且有核准的处置方案办理。",
    "effective_at": "2026-01-01T00:00:00+08:00",
    "parameters": {"required_photo_kinds": ["WRONG_ITEM", "LABEL"]},
}
DAMAGE_REMEDY_POLICY: dict = {
    "id": "POL14",
    "version": "1.0",
    "title": "损坏处置",
    "text": "损坏材料经人工审核确认后，可提交该订单行的商品退款处置方案。退款仍须核实实付及余额并经财务审批。",
    "effective_at": "2026-01-01T00:00:00+08:00",
    "parameters": {"allowed_action": "REFUND", "requires_material_review": True},
}


def initialize(engine: Engine, data: dict) -> None:
    """Import a frozen simulated baseline without resetting live history or stock."""
    shipments = {row["order_id"]: row for row in data["shipments"]}
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert

    with engine.begin() as conn:

        def add(table: FromClause, rows: list[dict]) -> None:
            if rows:
                conn.execute(insert(cast(Table, table)).on_conflict_do_nothing(), rows)

        add(Customer.__table__, [{"id": row["id"], "data": row} for row in data["customers"]])
        add(Product.__table__, [{"id": row["id"], "data": row} for row in data["products"]])
        add(
            Inventory.__table__,
            [{"product_id": row["id"], "available_quantity": 10} for row in data["products"]],
        )
        add(
            Order.__table__,
            [
                {
                    "id": row["id"],
                    "customer_id": row["customer_id"],
                    "data": {
                        **{key: value for key, value in row.items() if key != "items"},
                        "shipment": shipments.get(row["id"]),
                    },
                }
                for row in data["orders"]
            ],
        )
        add(
            OrderLine.__table__,
            [
                {
                    "order_id": order["id"],
                    "line_id": item["line_id"],
                    "product_id": item["product_id"],
                    "quantity": item["quantity"],
                    "paid_cents": item["paid_cents"],
                    "refunded_cents": item["refunded_cents"],
                    "data": item,
                }
                for order in data["orders"]
                for item in order["items"]
            ],
        )
        policies: dict[str, dict] = {
            row["id"]: row for row in [WRONG_ITEM_POLICY, DAMAGE_REMEDY_POLICY]
        }
        for row in data["policies"]:
            if row["id"] in policies and row != policies[row["id"]]:
                raise BusinessError("POLICY_CONFLICT", "模拟政策与扩展基线编号冲突。")
            policies[row["id"]] = row
        for row in policies.values():
            key = (Policy.id == row["id"], Policy.version == row["version"])
            add(
                Policy.__table__,
                [
                    {
                        "id": row["id"],
                        "version": row["version"],
                        "effective_at": datetime.fromisoformat(row["effective_at"]),
                        "data": row,
                    }
                ],
            )
            stored = conn.execute(Policy.__table__.select().where(*key)).mappings().one()
            if stored["data"] != row:
                raise BusinessError("POLICY_CONFLICT", "已存在同编号同版本的不同政策。")
