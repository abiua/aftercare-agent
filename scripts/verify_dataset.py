"""Verify frozen artifacts, checksums, structural invariants and regeneration equality."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aftercare_data.build import artifacts
from aftercare_data.inputs import BUSINESS_ARTIFACT, load_business
from aftercare_data.validation import require, validate_business


def verify(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    require(set(manifest["files"]) == {BUSINESS_ARTIFACT}, "Unexpected artifact paths")
    counts = validate_business(load_business(root))
    expected = artifacts(manifest["seed"])
    require(
        all((root / name).read_bytes() == body for name, body in expected.items()),
        "Regeneration differs from frozen artifacts",
    )
    return {
        "passed": True,
        "scope": "synthetic-business-data-structure-and-reproducibility",
        "counts": counts,
        "hashes_verified": len(manifest["files"]),
        "byte_reproduction": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", nargs="?", default=str(ROOT / "data/v0.1"))
    parser.add_argument("--report")
    args = parser.parse_args()
    report = json.dumps(verify(args.dataset), ensure_ascii=False, indent=2) + "\n"
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report)
    print(report)


if __name__ == "__main__":
    main()
