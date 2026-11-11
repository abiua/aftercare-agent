import copy
import tempfile
import unittest
from pathlib import Path

from aftercare_data.build import artifacts, build
from aftercare_data.generator import generate
from aftercare_data.inputs import initial_user_input, load_inputs
from aftercare_data.validation import validate_business


class IntegrationTests(unittest.TestCase):
    def test_generated_bytes_reproducible(self):
        self.assertEqual(artifacts(42), artifacts(42))
        self.assertNotEqual(
            artifacts(42)["agent/business.json"], artifacts(43)["agent/business.json"]
        )

    def test_loader_and_output_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build(Path(tmp) / "dataset")
            inputs = load_inputs(path)
            self.assertEqual(len(inputs["cases"]), 40)
            self.assertEqual(
                set(initial_user_input(inputs["cases"][0])),
                {"actor_customer_id", "order_id", "utterances"},
            )
            self.assertNotIn("kind", inputs["cases"][0]["fixture"]["request"])
            self.assertEqual(len(load_inputs(path, "test")["cases"]), 20)
            for case in inputs["cases"]:
                for forbidden in (
                    "expected_actions",
                    "expected_final_state",
                    "reason",
                    "family",
                    "policy_ids",
                ):
                    self.assertNotIn(forbidden, case)
            # Oracle deletion has no effect on the agent input loader.
            for file in (path / "evaluation/private").glob("*.json"):
                file.unlink()
            self.assertEqual(inputs, load_inputs(path))
            with self.assertRaises(FileExistsError):
                build(path)
            business = path / "agent/business.json"
            business.write_text("{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_inputs(path)

    def test_independent_validator_rejects_corruption(self):
        business = generate()
        validate_business(business)
        for mutate in (
            lambda d: d["orders"][0].update(customer_id="NOT-A-CUSTOMER"),
            lambda d: d["orders"][0].update(paid_cents=99999999),
            lambda d: d["orders"][0]["items"][0].update(refunded_cents=99999999),
            lambda d: d["orders"][0].update(created_at="2027-01-01T00:00:00+08:00"),
            lambda d: d["orders"].append(d["orders"][0]),
        ):
            altered = copy.deepcopy(business)
            mutate(altered)
            with self.assertRaises(ValueError):
                validate_business(altered)


if __name__ == "__main__":
    unittest.main()
