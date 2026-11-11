"""Build public inputs and evaluator-only answers into distinct directory trees."""

import argparse
import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from aftercare_data.generator import generate
from aftercare_data.scenarios import generate_cases
from aftercare_data.validation import validate_business, validate_cases


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def environment_case(case):
    result = deepcopy(case)
    result.pop("family", None)
    result.pop("policy_ids", None)
    result["fixture"]["request"].pop("kind", None)
    return result


def artifacts(seed=42):
    business = generate(seed=seed)
    cases, answers = generate_cases(business)
    counts = validate_business(business)
    case_counts = validate_cases(business, cases, answers)
    files = {"agent/business.json": encoded(business)}
    files["evaluation/private/case_index.json"] = encoded(
        [
            dict(
                {k: c[k] for k in ("id", "split", "family", "policy_ids")},
                request_kind=c["fixture"]["request"]["kind"],
            )
            for c in cases
        ]
    )
    for split in ("dev", "test"):
        files[f"agent/cases.{split}.json"] = encoded(
            [environment_case(case) for case in cases if case["split"] == split]
        )
        ids = {case["id"] for case in cases if case["split"] == split}
        files[f"evaluation/private/answers.{split}.json"] = encoded(
            [answer for answer in answers if answer["case_id"] in ids]
        )
    manifest = {
        "schema_version": "aftercare-data-v0.1",
        "synthetic": True,
        "seed": seed,
        "as_of": "2026-11-10T12:00:00+08:00",
        "counts": counts,
        "case_counts": case_counts,
        "human_reviewed": False,
        "agent_inputs": [name for name in files if name.startswith("agent/")],
        "files": {
            name: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            for name, body in files.items()
        },
    }
    files["manifest.json"] = encoded(manifest)
    return files


def build(destination, seed=42):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(
            "Output already exists; select a fresh directory to preserve frozen data"
        )
    files = artifacts(seed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".aftercare-build-", dir=destination.parent
    ) as temporary:
        stage = Path(temporary) / "dataset"
        stage.mkdir()
        for name, body in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            if name.startswith("evaluation/"):
                path.chmod(0o600)
        (stage / "evaluation/private").chmod(0o700)
        stage.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/v0.1"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    path = build(args.out, args.seed)
    print(f"Validated synthetic dataset written to {path.resolve()}")


if __name__ == "__main__":
    main()
