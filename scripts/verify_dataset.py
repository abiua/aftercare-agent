"""Verify frozen artifacts, checksums, structural invariants and regeneration equality."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aftercare_data.build import artifacts
from aftercare_data.validation import require, validate_business, validate_cases


def verify(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    expected_names = {
        "agent/business.json",
        "agent/cases.dev.json",
        "agent/cases.test.json",
        "evaluation/private/case_index.json",
        "evaluation/private/answers.dev.json",
        "evaluation/private/answers.test.json",
    }
    require(set(manifest["files"]) == expected_names, "Unexpected artifact paths")
    loaded = {}
    for name, record in manifest["files"].items():
        path = root / name
        require(not path.is_symlink(), "Symlink artifact rejected")
        body = path.read_bytes()
        require(
            hashlib.sha256(body).hexdigest() == record["sha256"], "Artifact hash mismatch: " + name
        )
        require(len(body) == record["bytes"], "Artifact size mismatch: " + name)
        loaded[name] = json.loads(body)
    business = loaded["agent/business.json"]
    cases = loaded["agent/cases.dev.json"] + loaded["agent/cases.test.json"]
    index = {case["id"]: case for case in loaded["evaluation/private/case_index.json"]}
    for case in cases:
        require(
            not {"expected_actions", "expected_final_state", "reason", "family", "policy_ids"}
            & case.keys(),
            "Oracle metadata in agent input",
        )
    reconstructed = [
        dict(case, family=index[case["id"]]["family"], policy_ids=index[case["id"]]["policy_ids"])
        for case in cases
    ]
    answers = (
        loaded["evaluation/private/answers.dev.json"]
        + loaded["evaluation/private/answers.test.json"]
    )
    counts = validate_business(business)
    case_counts = validate_cases(business, reconstructed, answers)
    expected = artifacts(manifest["seed"])
    require(
        all((root / name).read_bytes() == body for name, body in expected.items()),
        "Regeneration differs from frozen artifacts",
    )
    return {
        "passed": True,
        "scope": "synthetic-data-structure-and-reproducibility",
        "counts": counts,
        "cases": case_counts,
        "hashes_verified": len(expected_names),
        "byte_reproduction": True,
        "human_oracle_review": "NOT_RUN",
        "real_agent_execution": "NOT_RUN",
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
