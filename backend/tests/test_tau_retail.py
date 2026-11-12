import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/tau_retail.py"
SPEC = importlib.util.spec_from_file_location("tau_retail", SCRIPT)
tau = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tau)


@pytest.fixture
def source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    database = {
        "products": {str(i): {"product_id": str(i)} for i in range(50)},
        "users": {str(i): {"user_id": str(i)} for i in range(500)},
        "orders": {
            str(i): {"order_id": str(i), "user_id": str(i % 500), "status": "delivered"}
            for i in range(1000)
        },
    }
    ids = list(map(str, range(114)))
    tasks = [
        {
            "id": task_id,
            "user_scenario": {"instructions": "hidden customer"},
            "evaluation_criteria": {"actions": ["hidden reference"]},
            "initial_state": None,
        }
        for task_id in ids
    ]
    assets = {
        "db.json": json.dumps(database),
        "tasks.json": json.dumps(tasks),
        "split_tasks.json": json.dumps({"base": ids, "train": ids[:74], "test": ids[74:]}),
        "policy.md": "Retail policy",
        "LICENSE": "Fixture license",
    }
    manifest = {"revision": "fixture", "files": {}}
    for name, data in assets.items():
        value = data.encode()
        (source / name).write_bytes(value)
        manifest["files"][name] = {"size_bytes": len(value), "sha256": tau.digest(value)}
    derived = {
        "runtime/task_index.json": {
            task_id: "train" if i < 74 else "test" for i, task_id in enumerate(ids)
        },
        "simulator/scenarios.json": {task["id"]: task["user_scenario"] for task in tasks},
        "evaluation/references.json": {task["id"]: task["evaluation_criteria"] for task in tasks},
    }
    manifest["derived_files"] = {}
    for name, payload in derived.items():
        value = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        manifest["derived_files"][name] = {"size_bytes": len(value), "sha256": tau.digest(value)}
    manifest_path = tmp_path / "manifest.json"
    tau.write_json(manifest_path, manifest)
    monkeypatch.setattr(tau, "MANIFEST", manifest_path)
    return source


def test_retail_session_lifecycle_and_access(source, tmp_path):
    workspace = tmp_path / "runtime"
    initial_import = tau.import_source(source, workspace)
    assert initial_import["splits"] == {"train": 74, "test": 40}
    with pytest.raises(ValueError, match="Unknown retail task ID"):
        tau.initialize(workspace, "first", "unknown")
    assert not (workspace / "sessions").exists()

    first = tau.initialize(workspace, "first", "0")
    second = tau.initialize(workspace, "second", "0")
    agent = tau.RetailAgentTools(workspace, "first", "0")
    other_agent = tau.RetailAgentTools(workspace, "second", "0")
    assert agent.policy() == "Retail policy"
    assert agent.get_product_details("0") == {"product_id": "0"}
    assert agent.get_user_details("0") == {"user_id": "0"}
    assert agent.get_user_details("1") is None
    assert agent.get_order_details("1") is None
    assert agent.get_order_details("0")["status"] == "delivered"

    changed = tau.probe_exchange(workspace, "first", "0")
    assert changed["state_sha256"] != first["state_sha256"]
    assert agent.get_order_details("0")["status"] == tau.PROBE_ORDER_STATUS
    assert other_agent.get_order_details("0")["status"] == "delivered"
    assert tau.initialize(workspace, "first", "0")["state_sha256"] == changed["state_sha256"]
    assert tau.import_source(source, workspace) == initial_import
    assert agent.get_order_details("0")["status"] == tau.PROBE_ORDER_STATUS
    with pytest.raises(ValueError, match="different retail task"):
        tau.initialize(workspace, "first", "1")

    assert tau.reset(workspace, "first")["state_sha256"] == first["state_sha256"]
    assert tau.reset(workspace, "first")["state_sha256"] == second["state_sha256"]
    assert agent.get_order_details("0")["status"] == "delivered"
    assert json.loads((source / "db.json").read_text())["orders"]["0"]["status"] == "delivered"

    bundle = workspace / "bundle"
    assert list((bundle / "agent").iterdir()) == [bundle / "agent/policy.md"]
    assert "hidden customer" in (bundle / "simulator/scenarios.json").read_text()
    assert "hidden reference" in (bundle / "evaluation/references.json").read_text()
    with sqlite3.connect(workspace / "sessions/first.sqlite3") as db:
        dump = "\n".join(db.iterdump())
    assert "hidden customer" not in dump
    assert "hidden reference" not in dump


def test_hash_mismatch_leaves_runtime_untouched(source, tmp_path):
    workspace = tmp_path / "runtime"
    (source / "policy.md").write_text("corrupted")
    with pytest.raises(ValueError, match="policy.md"):
        tau.import_source(source, workspace)
    assert not workspace.exists()


@pytest.mark.parametrize(
    "tamper",
    ["agent/extra", "agent/policy.md", "runtime/baseline.json", "simulator/scenarios.json"],
)
def test_bundle_tampering_rejected(source, tmp_path, tamper):
    workspace = tmp_path / "runtime"
    tau.import_source(source, workspace)
    tau.initialize(workspace, "first", "0")
    (workspace / "bundle" / tamper).write_text("tampered")
    with pytest.raises(ValueError):
        tau.initialize(workspace, "second", "0")
    with pytest.raises(ValueError):
        tau.reset(workspace, "first")
    with pytest.raises(ValueError):
        tau.RetailAgentTools(workspace, "first", "0").policy()
    with pytest.raises(ValueError):
        tau.import_source(source, workspace)


def test_session_symlink_rejected(source, tmp_path):
    workspace = tmp_path / "runtime"
    tau.import_source(source, workspace)
    tau.initialize(workspace, "first", "0")
    (workspace / "sessions/linked.sqlite3").symlink_to("first.sqlite3")
    with pytest.raises(ValueError, match="must be local"):
        tau.reset(workspace, "linked")
