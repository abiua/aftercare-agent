"""Independent structural checks; these do not certify natural-language answer quality."""

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


def validate_cases(data, cases, answers):
    require(len(cases) == len(answers) == 60, "Expected 60 cases and 60 answers")
    case_ids = {case["id"] for case in cases}
    require(len(case_ids) == 60, "Duplicate case ID")
    require({answer["case_id"] for answer in answers} == case_ids, "Missing or duplicate oracle")
    require(sum(case["split"] == "dev" for case in cases) == 40, "Expected 40 development cases")
    require(sum(case["split"] == "test" for case in cases) == 20, "Expected 20 held-out cases")
    policies = {p["id"] for p in data["policies"]}
    products = {p["id"]: p for p in data["products"]}
    customers = {p["id"]: p for p in data["customers"]}
    for case in cases:
        require(case["actor_customer_id"] in customers, "Unknown actor")
        require(bool(case["utterances"]), "Empty utterances")
        require(set(case["policy_ids"]) <= policies, "Unknown scenario policy")
        require(
            not (
                {"expected_actions", "forbidden_actions", "expected_final_state", "reason"}
                & case.keys()
            ),
            "Oracle leaked into case",
        )
        order = case["fixture"]["order"]
        require(order["id"] == case["order_id"], "Case order identity mismatch")
        validate_order(order, products, customers)
    by_id = {case["id"]: case for case in cases}
    for answer in answers:
        case = by_id[answer["case_id"]]
        order = case["fixture"]["order"]
        state = answer["expected_final_state"]
        new_refund = state.get("new_refund_cents", 0)
        money(new_refund)
        require(
            new_refund <= order["paid_cents"] - order["refunded_cents"], "Oracle over-refunds order"
        )
        require(
            state["order_refunded_cents"] == order["refunded_cents"] + new_refund,
            "Oracle final balance mismatch",
        )
        calls = state["payment_calls"]
        require(calls["execute_refund"] in (0, 1), "Oracle repeats payment execution")
        if case["actor_customer_id"] != order["customer_id"]:
            require(
                calls["execute_refund"] == calls["query_refund"] == 0 and new_refund == 0,
                "Foreign actor changes financial state",
            )
        lines = {line["line_id"]: line for line in order["items"]}
        for line_id, amount in state.get("line_refunds", {}).items():
            require(line_id in lines, "Oracle refunds unknown line")
            require(
                0 <= amount <= lines[line_id]["paid_cents"] - lines[line_id]["refunded_cents"],
                "Oracle over-refunds line",
            )
        ledger = case["fixture"]["refund_ledger"]
        for entry in ledger:
            require(
                sum(entry["line_amounts_cents"].values()) == entry["amount_cents"],
                "Ledger line sum mismatch",
            )
            require(
                set(entry["line_amounts_cents"]) <= set(lines), "Ledger references unknown line"
            )
        for line_id, line in lines.items():
            historical = sum(
                entry["line_amounts_cents"].get(line_id, 0)
                for entry in ledger
                if entry["status"] == "SUCCEEDED"
            )
            require(
                historical == line["refunded_cents"], "Ledger and recorded refund balance differ"
            )
        require(isinstance(answer["expected_final_state"], dict), "Oracle state must be structured")
        require(bool(answer["reason"]) and bool(answer["expected_actions"]), "Empty oracle")
        require(set(answer["policy_ids"]) <= policies, "Unknown oracle policy")
        require(
            not (set(answer["expected_actions"]) & set(answer["forbidden_actions"])),
            "Contradictory oracle actions",
        )
    return {"cases": 60, "dev": 40, "test": 20, "oracle_review": "SYNTHETIC_NOT_HUMAN_REVIEWED"}
