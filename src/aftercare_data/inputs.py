"""Allowlisted loader for agent input. This module never imports the oracle generator."""

import hashlib
import json
from pathlib import Path


def load_inputs(dataset_root, split="dev"):
    if split not in {"dev", "test"}:
        raise ValueError("Unknown split")
    root = Path(dataset_root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    loaded = []
    for name in ("agent/business.json", f"agent/cases.{split}.json"):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root / "agent"):
            raise ValueError("Input path leaves agent directory")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError("Frozen input checksum mismatch")
        loaded.append(json.loads(body))
    return {"business": loaded[0], "cases": loaded[1]}


def initial_user_input(case):
    """Minimal model-facing request. Fixtures/events stay in the simulated tool environment."""
    return {key: case[key] for key in ("actor_customer_id", "order_id", "utterances")}
