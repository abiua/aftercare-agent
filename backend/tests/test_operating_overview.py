"""Regression checks for source gaps, billing and tester-scoped batch windows."""

import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aftercare.auth import current_user
from aftercare.operating_metrics import BenchmarkBilling
from aftercare.operations import install


def test_billing_tails_complete_rows_and_switches_to_settled_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERCARE_BENCHMARK_JOBS", str(tmp_path))
    directory = tmp_path / "example"
    (directory / "run").mkdir(parents=True)
    (directory / "started").write_text('{"time":1,"scope":"host-test"}')
    ledger = directory / "run/calls.jsonl"
    ledger.write_text('{"call_id":1,"reserve_usd":"0.1"}\n{"call_id":1,')
    billing = BenchmarkBilling()
    assert billing.collect("host-test") == (0, 1)
    with ledger.open("a") as stream:
        stream.write('"actual_usd":"0.025"}\n')
    paid, pending = billing.collect("host-test")
    assert str(paid) == "0.025" and pending == 0
    assert billing.collect("host-test") == (paid, 0)
    (directory / "outcome.json").write_text('{"billing":{"known_usd":"0.025","unsettled":0}}')
    ledger.unlink()
    assert billing.collect("host-test") == (paid, 0)


def test_batch_windows_are_owned_and_require_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERCARE_BENCHMARK_JOBS", str(tmp_path))
    monkeypatch.setenv("AFTERCARE_PROMETHEUS_URL", "http://127.0.0.1:1")
    app = FastAPI()
    app.state.settings = SimpleNamespace(profile="test")
    install(app)
    client = TestClient(app)
    base = "/api/operations"
    assert client.get(base + "/batches").status_code == 401
    user = SimpleNamespace(user_id="alice", role="support")
    app.dependency_overrides[current_user] = lambda: user
    assert client.get(base + "/timeline").status_code == 403
    directory = tmp_path / str(uuid4())
    directory.mkdir()
    (directory / "plan.json").write_text(json.dumps({"owner": "alice", "architectures": ["S1"]}))
    user.role = "tester"
    assert client.get(base + "/batches").json()["items"] == []
    (directory / "started").write_text('{"time":1,"scope":"host-test"}')
    (directory / "lock").touch()
    url = base + "/timeline?batch=" + directory.name
    assert client.get(url).status_code == 409
    (directory / "outcome.json").write_text('{"state":"completed"}')
    item = client.get(base + "/batches").json()["items"][0]
    assert item["start"] == 1
    assert item["end"] == (directory / "outcome.json").stat().st_mtime
    assert client.get(url).status_code == 503
    app.state.settings.profile = "app"
    assert not client.get(base + "/batches").json()["items"]
    assert client.get(url).status_code == 409
    user.user_id = "bob"
    assert client.get(url).status_code == 404
    assert not client.get(base + "/batches").json()["items"]
