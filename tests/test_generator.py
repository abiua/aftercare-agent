import json
import unittest
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise

from aftercare_data.generator import OBSERVED_AT, _allocate_discount, generate


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = generate()

    def test_deterministic_and_independent(self):
        baseline = json.dumps(self.data, ensure_ascii=False)
        regenerated = generate(42)
        self.assertEqual(baseline, json.dumps(regenerated, ensure_ascii=False))
        self.assertNotEqual(self.data["orders"], generate(43)["orders"])
        regenerated["orders"][0]["items"].clear()
        regenerated["policies"][5]["parameters"]["required_photo_kinds"].clear()
        self.assertEqual(baseline, json.dumps(generate(), ensure_ascii=False))

    def test_identifiers_and_relationships(self):
        products = {product["id"]: product for product in self.data["products"]}
        customers = self.data["customers"]
        orders = self.data["orders"]
        self.assertEqual(list(products), [f"P{i:03d}" for i in range(1, 31)])
        self.assertEqual(
            [customer["id"] for customer in customers], [f"C{i:03d}" for i in range(1, 101)]
        )
        self.assertEqual([order["id"] for order in orders], [f"O{i:04d}" for i in range(1, 301)])
        self.assertEqual(
            Counter(order["customer_id"] for order in orders),
            Counter({customer["id"]: 3 for customer in customers}),
        )
        used_products = set()
        for order in orders:
            line_ids = [item["line_id"] for item in order["items"]]
            self.assertEqual(len(line_ids), len(set(line_ids)))
            for item in order["items"]:
                product = products[item["product_id"]]
                self.assertEqual(item["unit_price_cents"], product["unit_price_cents"])
                used_products.add(product["id"])
        self.assertEqual(used_products, set(products))
        self.assertTrue(
            any(not p["returnable"] and p["warranty_days"] == 365 for p in products.values())
        )

    def test_discount_conservation_and_cent_ties(self):
        self.assertEqual(_allocate_discount([100, 100, 100], 2), [1, 1, 0])
        self.assertEqual(_allocate_discount([100, 150, 250], 7), [1, 2, 4])
        self.assertEqual(_allocate_discount([31, 66], 0), [0, 0])

    def test_money_and_refund_coverage(self):
        refunds = Counter()
        multi_item_discounts = 0
        for order in self.data["orders"]:
            with self.subTest(order_id=order["id"]):
                items = order["items"]
                paid = order["payment_status"] == "CONFIRMED"
                gross = 0
                for item in items:
                    for key in (
                        "quantity",
                        "unit_price_cents",
                        "discount_cents",
                        "payable_cents",
                        "paid_cents",
                        "refunded_cents",
                    ):
                        self.assertIs(type(item[key]), int)
                    self.assertGreater(item["quantity"], 0)
                    line_gross = item["quantity"] * item["unit_price_cents"]
                    gross += line_gross
                    self.assertLessEqual(0, item["discount_cents"])
                    self.assertLess(item["discount_cents"], line_gross)
                    self.assertEqual(item["payable_cents"], line_gross - item["discount_cents"])
                    self.assertEqual(item["paid_cents"], item["payable_cents"] if paid else 0)
                    self.assertLessEqual(0, item["refunded_cents"])
                    self.assertLessEqual(item["refunded_cents"], item["paid_cents"])
                self.assertEqual(order["subtotal_cents"], gross)
                self.assertEqual(order["payable_cents"], gross - order["discount_cents"])
                for field in ("discount_cents", "payable_cents", "paid_cents", "refunded_cents"):
                    self.assertIs(type(order[field]), int)
                    self.assertEqual(order[field], sum(item[field] for item in items))
                if len(items) > 1 and order["discount_cents"]:
                    multi_item_discounts += 1
                if order["refunded_cents"]:
                    self.assertEqual(order["status"], "DELIVERED")
                    refunds[
                        "full" if order["refunded_cents"] == order["paid_cents"] else "partial"
                    ] += 1
                else:
                    refunds["none"] += 1
        self.assertGreater(multi_item_discounts, 0)
        self.assertEqual(set(refunds), {"none", "partial", "full"})

    def test_fulfillment_times_and_shipments(self):
        orders = self.data["orders"]
        shipment_rows = self.data["shipments"]
        shipments = {shipment["order_id"]: shipment for shipment in shipment_rows}
        self.assertEqual(len(shipment_rows), len(shipments))
        self.assertEqual(len(shipment_rows), len({s["tracking_number"] for s in shipment_rows}))
        self.assertEqual(len(shipment_rows), len({s["id"] for s in shipment_rows}))
        required_times = {
            "UNPAID": (),
            "CANCELLED": ("cancelled_at",),
            "PAID": ("paid_at",),
            "SHIPPED": ("paid_at", "shipped_at"),
            "DELIVERED": ("paid_at", "shipped_at", "delivered_at"),
        }
        self.assertEqual({order["status"] for order in orders}, set(required_times))
        optional_times = ("paid_at", "shipped_at", "delivered_at", "cancelled_at")
        for order in orders:
            with self.subTest(order_id=order["id"]):
                fields = required_times[order["status"]]
                parsed = [datetime.fromisoformat(order[key]) for key in ("created_at", *fields)]
                for key in optional_times:
                    if key not in fields:
                        self.assertIsNone(order[key])
                for timestamp in parsed:
                    self.assertEqual(timestamp.utcoffset(), timedelta(hours=8))
                    self.assertLessEqual(timestamp, OBSERVED_AT)
                for earlier, later in pairwise(parsed):
                    self.assertLess(earlier, later)
                self.assertEqual(
                    order["payment_status"], "CONFIRMED" if "paid_at" in fields else "UNPAID"
                )
                shipment = shipments.pop(order["id"], None)
                if "shipped_at" in fields:
                    self.assertIsNotNone(shipment)
                    self.assertEqual(shipment["shipped_at"], order["shipped_at"])
                    self.assertEqual(shipment["delivered_at"], order["delivered_at"])
                    self.assertEqual(
                        shipment["status"],
                        "DELIVERED" if "delivered_at" in fields else "IN_TRANSIT",
                    )
                else:
                    self.assertIsNone(shipment)
        self.assertFalse(shipments)

    def test_policies_have_machine_parameters_and_stable_ids(self):
        policies = self.data["policies"]
        self.assertEqual(
            [policy["id"] for policy in policies], [f"POL{i:02d}" for i in range(1, 13)]
        )
        for policy in policies:
            self.assertEqual(policy["version"], "1.0")
            self.assertLess(datetime.fromisoformat(policy["effective_at"]), OBSERVED_AT)
            self.assertTrue(policy["text"])
            self.assertTrue(policy["parameters"])
        self.assertEqual(policies[4]["parameters"]["window_hours"], 168)
        self.assertTrue(policies[4]["parameters"]["inclusive"])
        self.assertEqual(
            policies[9]["parameters"], {"approval_threshold_cents": 50000, "operator": "gt"}
        )
        self.assertFalse(policies[11]["parameters"]["allow_blind_retry"])

    def test_partial_refund_requires_resolution_and_caps_amount(self):
        rule = next(policy for policy in self.data["policies"] if policy["id"] == "POL09")
        self.assertIs(rule["parameters"]["requires_approved_resolution"], True)
        self.assertEqual(rule["parameters"]["cap"], "ITEM_REMAINING_PAID")
        self.assertIs(rule["parameters"]["include_fees"], False)
