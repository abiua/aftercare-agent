import copy
import tempfile
import unittest
from pathlib import Path

from aftercare_data.build import artifacts, build
from aftercare_data.generator import generate
from aftercare_data.inputs import load_business
from aftercare_data.validation import validate_business


class IntegrationTests(unittest.TestCase):
    def test_generated_bytes_reproducible(self):
        self.assertEqual(artifacts(42), artifacts(42))
        self.assertNotEqual(
            artifacts(42)["agent/business.json"], artifacts(43)["agent/business.json"]
        )

    def test_build_emits_only_verified_business_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build(Path(tmp) / "dataset")
            self.assertEqual(
                {file.relative_to(path).as_posix() for file in path.rglob("*") if file.is_file()},
                {"agent/business.json", "manifest.json"},
            )
            self.assertEqual(load_business(path), generate())
            with self.assertRaises(FileExistsError):
                build(path)
            (path / "agent/business.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_business(path)

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
