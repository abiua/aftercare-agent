"""Load and verify the frozen business seed."""

import hashlib
import json
from pathlib import Path


BUSINESS_ARTIFACT = "agent/business.json"


def load_business(dataset_root):
    root = Path(dataset_root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    path = root / BUSINESS_ARTIFACT
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Invalid business input path")
    body = path.read_bytes()
    record = manifest["files"][BUSINESS_ARTIFACT]
    if hashlib.sha256(body).hexdigest() != record["sha256"]:
        raise ValueError("Frozen business input checksum mismatch")
    if len(body) != record["bytes"]:
        raise ValueError("Frozen business input size mismatch")
    return json.loads(body)
