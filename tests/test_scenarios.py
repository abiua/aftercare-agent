import json
import unittest
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta

from aftercare_data.generator import generate
from aftercare_data.scenarios import generate_cases


def _all_keys(value):
    if isinstance(value, dict):
        yield from value
        for nested in value.values():
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.business = generate()
        cls.cases, cls.answers = generate_cases(cls.business)
        cls.case = {case["id"]: case for case in cls.cases}
        cls.answer = {answer["case_id"]: answer for answer in cls.answers}
        cls.policies = {row["id"]: row["parameters"] for row in cls.business["policies"]}

    def test_frozen_splits_and_coverage(self):
        self.assertEqual(list(self.case), [f"CASE{index:03d}" for index in range(1, 61)])
        self.assertEqual(list(self.answer), list(self.case))
        self.assertEqual([case["split"] for case in self.cases], ["dev"] * 40 + ["test"] * 20)
        expected_held_out = {
            "identity_payment": 2,
            "cancellation": 2,
            "interception": 2,
            "return_window": 4,
            "damage_evidence": 3,
            "amount_allocation": 3,
            "refund_recovery": 3,
            "warranty": 1,
        }
        self.assertEqual(Counter(case["family"] for case in self.cases[40:]), expected_held_out)
        self.assertEqual(len({answer["coverage"] for answer in self.answers}), 60)
        self.assertEqual(len({case["order_id"] for case in self.cases}), 60)
        phrases = {utterance for case in self.cases[:40] for utterance in case["utterances"]}
        self.assertTrue(
            phrases.isdisjoint(
                utterance for case in self.cases[40:] for utterance in case["utterances"]
            )
        )

    def test_generation_and_mutation_do_not_modify_other_cases_or_business_data(self):
        input_data = generate()
        pristine = deepcopy(input_data)
        cases, answers = generate_cases(input_data)
        self.assertEqual(input_data, pristine)
        self.assertEqual((cases, answers), generate_cases(input_data))
        original_neighbors = deepcopy(cases[1:])
        cases[0]["fixture"]["order"]["items"][0]["paid_cents"] = 0
        cases[0]["fixture"]["products"][0]["name"] = "modified"
        cases[0]["events"][0]["status"] = "modified"
        self.assertEqual(cases[1:], original_neighbors)
        self.assertEqual(input_data, pristine)
        self.assertEqual(generate_cases(input_data), (self.cases, self.answers))

    def test_snapshots_refer_to_real_catalog_ids_and_match_order_state(self):
        orders = {row["id"]: row for row in self.business["orders"]}
        products = {row["id"]: row for row in self.business["products"]}
        customers = {row["id"] for row in self.business["customers"]}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                fixture = case["fixture"]
                order = fixture["order"]
                items = order["items"]
                shipment = fixture["shipment"]
                self.assertEqual(case["order_id"], order["id"])
                self.assertEqual(orders[order["id"]]["customer_id"], order["customer_id"])
                self.assertIn(case["actor_customer_id"], customers)
                self.assertEqual(
                    {item["product_id"] for item in items},
                    {row["id"] for row in fixture["products"]},
                )
                for product in fixture["products"]:
                    self.assertEqual(product, products[product["id"]])
                self.assertTrue(
                    set(fixture["request"]["line_ids"]) <= {item["line_id"] for item in items}
                )
                self.assertEqual(bool(shipment), order["status"] in ("SHIPPED", "DELIVERED"))
                if shipment:
                    self.assertEqual(shipment["order_id"], order["id"])
                    self.assertEqual(shipment["shipped_at"], order["shipped_at"])
                    self.assertEqual(shipment["delivered_at"], order["delivered_at"])
                dates = [
                    order[key]
                    for key in (
                        "created_at",
                        "paid_at",
                        "shipped_at",
                        "delivered_at",
                        "cancelled_at",
                    )
                    if order[key]
                ]
                self.assertEqual(dates, sorted(dates))
                self.assertTrue(all(value <= fixture["as_of"] for value in dates))
                event_times = [event["at"] for event in case["events"]]
                self.assertEqual(event_times, sorted(event_times))
                self.assertTrue(all(value > fixture["as_of"] for value in event_times))

    def test_no_answer_fields_in_input(self):
        answer_keys = {
            "expected_actions",
            "forbidden_actions",
            "expected_final_state",
            "reason",
            "coverage",
            "new_refund_cents",
            "payment_calls",
            "proposed_refund_cents",
        }
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(answer_keys.isdisjoint(_all_keys(case)))
                self.assertTrue(set(case["policy_ids"]) <= set(self.policies))
                self.assertTrue(
                    set(self.answer[case["id"]]["policy_ids"]) <= set(case["policy_ids"])
                )
        json.dumps(self.cases)
        json.dumps(self.answers)

    def test_accounting_for_fixture_ledger_and_oracle_effects(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                fixture = case["fixture"]
                order = fixture["order"]
                items = {item["line_id"]: item for item in order["items"]}
                for key in ("discount_cents", "payable_cents", "paid_cents", "refunded_cents"):
                    self.assertEqual(order[key], sum(item[key] for item in items.values()))
                self.assertEqual(
                    order["subtotal_cents"] - order["discount_cents"], order["payable_cents"]
                )
                historical = Counter()
                for entry in fixture["refund_ledger"]:
                    self.assertEqual(
                        entry["amount_cents"], sum(entry["line_amounts_cents"].values())
                    )
                    self.assertTrue(set(entry["line_amounts_cents"]) <= set(items))
                    if entry["status"] == "SUCCEEDED":
                        historical.update(entry["line_amounts_cents"])
                for line_id, item in items.items():
                    self.assertEqual(historical[line_id], item["refunded_cents"])
                    self.assertLessEqual(item["refunded_cents"], item["paid_cents"])
                answer = self.answer[case["id"]]
                state = answer["expected_final_state"]
                allocation = state.get("line_refunds", {})
                self.assertEqual(state["new_refund_cents"], sum(allocation.values()))
                self.assertEqual(
                    state["order_refunded_cents"],
                    order["refunded_cents"] + state["new_refund_cents"],
                )
                self.assertLessEqual(state["order_refunded_cents"], order["paid_cents"])
                for line_id, refund in allocation.items():
                    self.assertIn(line_id, fixture["request"]["line_ids"])
                    self.assertLessEqual(
                        items[line_id]["refunded_cents"] + refund, items[line_id]["paid_cents"]
                    )
                self.assertTrue(
                    set(answer["expected_actions"]).isdisjoint(answer["forbidden_actions"])
                )

    def test_explicit_boundary_cases_match_policy_parameters(self):
        window = timedelta(hours=self.policies["POL05"]["window_hours"])
        for key, shift, expected in (
            ("CASE019", 0, "RETURN_RECEIPT_PENDING"),
            ("CASE048", -1, "REFUNDED"),
            ("CASE049", 1, "RETURN_WINDOW_EXPIRED"),
        ):
            fixture = self.case[key]["fixture"]
            age = datetime.fromisoformat(fixture["as_of"]) - datetime.fromisoformat(
                fixture["order"]["delivered_at"]
            )
            self.assertEqual(age, window + timedelta(seconds=shift))
            self.assertEqual(self.answer[key]["expected_final_state"]["status"], expected)
        threshold = self.policies["POL10"]["approval_threshold_cents"]
        for key, value_field, cents in (
            ("CASE008", "new_refund_cents", threshold),
            ("CASE009", "proposed_refund_cents", threshold + 1),
            ("CASE054", "new_refund_cents", threshold),
            ("CASE055", "proposed_refund_cents", threshold + 1),
        ):
            state = self.answer[key]["expected_final_state"]
            self.assertEqual(state[value_field], cents)
            self.assertEqual(state["policy_approval_required"], cents > threshold)
        fixture = self.case["CASE060"]["fixture"]
        product = fixture["products"][0]
        self.assertFalse(product["returnable"])
        age = datetime.fromisoformat(fixture["as_of"]) - datetime.fromisoformat(
            fixture["order"]["delivered_at"]
        )
        self.assertEqual(age, timedelta(days=product["warranty_days"], seconds=-1))

    def test_duplicate_timeout_and_authorization_call_limits(self):
        for key, execute, query, refund in (
            ("CASE031", 0, 0, 0),
            ("CASE032", 0, 0, 0),
            ("CASE033", 1, 0, 0),
            ("CASE034", 0, 1, 15000),
            ("CASE035", 0, 1, 0),
            ("CASE041", 0, 0, 0),
            ("CASE057", 0, 0, 0),
            ("CASE058", 0, 1, 0),
            ("CASE059", 1, 1, 25000),
        ):
            with self.subTest(case=key):
                state = self.answer[key]["expected_final_state"]
                self.assertEqual(
                    state["payment_calls"], {"execute_refund": execute, "query_refund": query}
                )
                self.assertEqual(state["new_refund_cents"], refund)
                self.assertIn("BLIND_REFUND_RETRY", self.answer[key]["forbidden_actions"])
                case = self.case[key]
                original_key = case["fixture"]["request"]["idempotency_key"]
                self.assertTrue(
                    all(event["idempotency_key"] == original_key for event in case["events"])
                )
        case = self.case["CASE041"]
        self.assertNotEqual(case["actor_customer_id"], case["fixture"]["order"]["customer_id"])
        self.assertIn("DISCLOSE_ORDER", self.answer["CASE041"]["forbidden_actions"])
        self.assertEqual(
            [event["type"] for event in self.case["CASE059"]["events"]],
            ["REFUND_CALL_RESULT", "REQUEST_REPLAY", "REFUND_QUERY_RESULT"],
        )

    def test_partial_refund_requires_independent_resolution(self):
        self.assertTrue(self.policies["POL09"]["requires_approved_resolution"])
        for case in self.cases:
            fixture = case["fixture"]
            if fixture["request"]["kind"] != "PARTIAL_REFUND":
                continue
            with self.subTest(case=case["id"]):
                resolution = fixture["resolution"]
                answer = self.answer[case["id"]]
                if case["id"] == "CASE029":
                    self.assertIsNone(resolution)
                    self.assertEqual(answer["expected_final_state"]["status"], "RESOLUTION_PENDING")
                    self.assertIn("EXECUTE_REFUND", answer["forbidden_actions"])
                else:
                    self.assertEqual(resolution["type"], "MERCHANT_CONCESSION")
                    self.assertEqual(resolution["status"], "APPROVED")
                    self.assertEqual(resolution["line_ids"], fixture["request"]["line_ids"])
                    self.assertLessEqual(resolution["approved_at"], fixture["as_of"])
