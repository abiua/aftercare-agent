"""Independent structural checks for synthetic business data."""

from datetime import datetime

AS_OF = datetime.fromisoformat("2026-11-10T12:00:00+08:00")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "Timestamp must include timezone")
    require(result <= AS_OF, "Business event is later than observation time")
    return result


def money(value):
    require(type(value) is int and value >= 0, "Amounts must be nonnegative integer cents")


def validate_order(order, products, customers):
    require(order["customer_id"] in customers, "Order references missing customer")
    require(
        order["status"] in {"UNPAID", "CANCELLED", "PAID", "SHIPPED", "DELIVERED"},
        "Unknown order status",
    )
    require(bool(order["items"]), "Order has no items")
    subtotal = discount = paid = refunded = 0
    for item in order["items"]:
        require(item["product_id"] in products, "Order references missing product")
        require(type(item["quantity"]) is int and item["quantity"] > 0, "Invalid quantity")
        for key in ("unit_price_cents", "discount_cents", "paid_cents", "refunded_cents"):
            money(item[key])
        gross = item["unit_price_cents"] * item["quantity"]
        require(item["discount_cents"] <= gross, "Discount exceeds line value")
        require(
            item["payable_cents"] == gross - item["discount_cents"],
            "Line payable allocation mismatch",
        )
        require(
            item["paid_cents"] == (gross - item["discount_cents"] if order.get("paid_at") else 0),
            "Line settlement does not conserve cents",
        )
        require(item["refunded_cents"] <= item["paid_cents"], "Line over-refund")
        subtotal += gross
        discount += item["discount_cents"]
        paid += item["paid_cents"]
        refunded += item["refunded_cents"]
    for key, value in (
        ("subtotal_cents", subtotal),
        ("discount_cents", discount),
        ("paid_cents", paid),
        ("refunded_cents", refunded),
    ):
        money(order[key])
        require(order[key] == value, "Order/line mismatch: " + key)
    require(order["payable_cents"] == subtotal - discount, "Order payable allocation mismatch")
    dates = {
        k: stamp(order.get(k))
        for k in ("created_at", "paid_at", "shipped_at", "delivered_at", "cancelled_at")
    }
    require(dates["created_at"] is not None, "Missing creation time")
    prior = dates["created_at"]
    for key in ("paid_at", "shipped_at", "delivered_at"):
        if dates[key] is not None:
            require(dates[key] >= prior, "Order timeline is not monotonic")
            prior = dates[key]
    if order["status"] == "UNPAID":
        require(
            dates["paid_at"] is None
            and dates["shipped_at"] is None
            and dates["delivered_at"] is None,
            "Unpaid order has fulfillment events",
        )
        require(refunded == 0, "Unpaid order has refund")
    if order["status"] in {"PAID", "SHIPPED", "DELIVERED"}:
        require(dates["paid_at"] is not None, "Paid order lacks payment time")
    if order["status"] in {"SHIPPED", "DELIVERED"}:
        require(dates["shipped_at"] is not None, "Shipped order lacks shipment time")
    if order["status"] == "DELIVERED":
        require(dates["delivered_at"] is not None, "Delivered order lacks delivery time")
    if dates["cancelled_at"] is not None:
        require(dates["cancelled_at"] >= dates["created_at"], "Cancellation predates order")


def validate_business(data):
    counts = {"products": 30, "customers": 100, "orders": 300, "policies": 12}
    for table, count in counts.items():
        require(len(data[table]) == count, "Unexpected table size: " + table)
    indexes = {}
    for table in ("products", "customers", "orders", "shipments", "policies"):
        indexes[table] = {row["id"]: row for row in data[table]}
        require(len(indexes[table]) == len(data[table]), "Duplicate ID: " + table)
    for order in data["orders"]:
        validate_order(order, indexes["products"], indexes["customers"])
    seen = set()
    for shipment in data["shipments"]:
        require(shipment["order_id"] in indexes["orders"], "Orphan shipment")
        require(shipment["order_id"] not in seen, "Duplicate shipment")
        seen.add(shipment["order_id"])
        order = indexes["orders"][shipment["order_id"]]
        require(order["status"] in {"SHIPPED", "DELIVERED"}, "Shipment for unfulfilled order")
        require(shipment["shipped_at"] == order["shipped_at"], "Shipment time mismatch")
        require(shipment["delivered_at"] == order["delivered_at"], "Delivery time mismatch")
    require(
        seen == {o["id"] for o in data["orders"] if o["status"] in {"SHIPPED", "DELIVERED"}},
        "Missing shipment",
    )
    for policy in data["policies"]:
        require(
            bool(policy["version"]) and bool(policy["text"]) and bool(policy["parameters"]),
            "Incomplete policy",
        )
        stamp(policy["effective_at"])
    return {key: len(value) for key, value in data.items() if isinstance(value, list)}
