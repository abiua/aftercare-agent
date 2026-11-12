"""Build the frozen synthetic business seed and its manifest."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from aftercare_data.generator import generate
from aftercare_data.validation import validate_business


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def artifacts(seed=42):
    business = generate(seed=seed)
    counts = validate_business(business)
    files = {"agent/business.json": encoded(business)}
    manifest = {
        "schema_version": "aftercare-data-v0.1",
        "synthetic": True,
        "seed": seed,
        "as_of": "2026-11-10T12:00:00+08:00",
        "counts": counts,
        "human_reviewed": False,
        "agent_inputs": list(files),
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
        stage.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/v0.1"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    path = build(args.out, args.seed)
    print(f"Validated synthetic business seed written to {path.resolve()}")


if __name__ == "__main__":
    main()
