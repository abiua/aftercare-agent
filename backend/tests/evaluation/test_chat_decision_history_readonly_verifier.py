"""Pure tests for the read-only history pagination acceptance."""

import importlib.util
from pathlib import Path

import pytest


def test_native_history_requires_exact_cursor_and_scope(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "history_readonly_verifier", scripts / "verify_chat_decision_history_readonly.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    entries = [
        {"id": str(version), "version": version, "origin": "AGENT", "created_at": "timestamp"}
        for version in (3, 2, 1)
    ]
    native = {
        "issue_id": "issue",
        "snapshots": [{**entry, "sha256": "a" * 64} for entry in entries],
    }
    calls = []

    def fetch(params):
        calls.append(params)
        visible = [item for item in entries if item["version"] < params.get("before_version", 4)]
        return {
            "issue_id": "issue",
            "items": visible[:1],
            "next_before_version": visible[0]["version"] if len(visible) > 1 else None,
        }

    harness._verify_pages(fetch, native)
    assert calls == [
        {"limit": 1},
        {"limit": 1, "before_version": 3},
        {"limit": 1, "before_version": 2},
        {"limit": 1, "before_version": 1},
    ]
    with pytest.raises(ValueError, match="PAGINATION_MISMATCH"):
        harness._verify_pages(lambda params: {**fetch(params), "issue_id": "other"}, native)
