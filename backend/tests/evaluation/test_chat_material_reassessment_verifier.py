"""Offline checks for the owned HTTP/Celery acceptance driver."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def verifier(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "chat_material_reassessment", scripts / "verify_chat_material_review_reassessment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_chain_requires_new_bound_turn_and_no_customer_message(verifier):
    old_snapshot = {"id": "old", "version": 2, "sha256": "a" * 64}
    before = {
        "turns": [{"generation": 2, "input_through_seq": 5}],
        "requests": [{"dependency_sha256": "old"}],
        "snapshots": [old_snapshot],
        "customer_messages": 2,
    }
    after = {
        "turns": [
            *before["turns"],
            {
                "id": "turn",
                "generation": 3,
                "input_through_seq": 6,
                "state": "PUBLISHED",
            },
        ],
        "requests": [
            *before["requests"],
            {
                "turn_id": "turn",
                "bound_turn_id": "turn",
                "state": "STARTED",
                "run_id": "run",
                "dependency_sha256": "new",
                "frozen_review_ids": ["review"],
                "previous": old_snapshot,
                "bound_previous": old_snapshot,
            },
        ],
        "snapshots": [
            old_snapshot,
            {
                "id": "new",
                "run_id": "run",
                "parent_id": None,
                "version": 3,
                "projection": "PUBLISHED",
            },
        ],
        "reviews": [{"id": "review", "decision": "APPROVED", "no_observations": True}],
        "customer_messages": 2,
    }
    assert verifier._stable_reassessment(before, after, "review", "new")
    assert not verifier._stable_reassessment(
        before, {**after, "customer_messages": 3}, "review", "new"
    )
    assert not verifier._stable_reassessment(before, after, "different-review", "new")


def test_native_audit_is_read_only_and_embedded_python_compiles(verifier, monkeypatch):
    monkeypatch.setattr(verifier, "owned_container", lambda *_: "owned-api")

    def compile_audit(label, name, code):
        compile(code, "<owned-pg-readback>", "exec")
        assert name == "owned-api"
        assert "SET TRANSACTION READ ONLY" in code
        assert "pg_control_system" in code
        return {"verified": True}

    stack = SimpleNamespace(json_exec=compile_audit)
    assert verifier._readback(stack, customer="C001") == {"verified": True}
