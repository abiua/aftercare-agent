import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from aftercare.business.errors import BusinessError
from aftercare.evaluation import workbench_jobs as jobs

TESTER = SimpleNamespace(role="tester", user_id="one")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERCARE_BENCHMARK_JOBS", str(tmp_path))
    return tmp_path


def selection(**changes):
    return jobs.Selection(
        **({"task_ids": ["5"], "architectures": ["S1"], "max_calls": 2, "max_usd": 0.20} | changes)
    )


def test_preview_freezes_code_without_launching(workspace):
    view = jobs.preview(TESTER, selection(), False)
    path = workspace / view["id"]
    assert view["state"] == "preview" and view["planned"] == 1
    assert "启用模型的实时服务" in view["missing"] and "owner" not in view
    assert (path / "source/scripts/workbench_campaign.py").is_file()
    assert not (path / "started").exists()
    assert jobs.list_jobs(TESTER, False)["items"] == [view]
    with pytest.raises(BusinessError, match="实时服务"):
        jobs.launch(TESTER, UUID(view["id"]), False)
    other = SimpleNamespace(role="tester", user_id="other")
    assert jobs.list_jobs(other, True)["items"] == []
    with pytest.raises(BusinessError, match="找不到"):
        jobs.launch(other, UUID(view["id"]), True)
    with pytest.raises(BusinessError, match="仅测试员"):
        jobs.preview(SimpleNamespace(role="support"), selection(), True)


@pytest.mark.parametrize(
    "changes",
    [
        {"architectures": ["S0"]},
        {"max_calls": 0},
        {"max_usd": -1},
        {"max_usd": float("nan")},
        {"task_ids": ["../"]},
    ],
)
def test_invalid_input(changes):
    with pytest.raises(ValidationError):
        selection(**changes)


def test_unknown_ids_duplicates_and_unconfirmed_launch(workspace):
    for ids in (["999"], ["5", "5"]):
        with pytest.raises(BusinessError, match="选择"):
            jobs.preview(TESTER, selection(task_ids=ids), False)
    with pytest.raises(ValidationError):
        jobs.LaunchConfirmation(confirm_paid=False)


def test_background_once_only_and_result_readback(workspace, monkeypatch):
    # Substitute the whole provider-facing executable. The child exercises the real file
    # lock, persisted receipts, aggregation and polling, with no model runtime or keys.
    view = jobs.preview(TESTER, selection(), False)
    identifier = UUID(view["id"])
    child = workspace / "offline_fixture.py"
    child.write_text("""
import sys, time
from pathlib import Path
p = Path(sys.argv[1])
sys.path.insert(0, str(p / 'source/scripts'))
from workbench_campaign import finish
from tau_official_summary import write_json
time.sleep(.15)
r = p / 'run'
(r / 'rows').mkdir(parents=True)
(r / 'official').mkdir()
item = dict(task_id='5', architecture='S1', seed=42)
write_json(r/'manifest.json', dict(revision='fixture', split='test', task_ids=['5'],
    trials_per_task=1, schedule=[item]))
write_json(r/'rows/5-S1.json', dict(item, status='completed', official_reward=1, duration_seconds=3,
    agent_calls=1, user_calls=0, review_calls=0))
write_json(r/'official/5-S1.json', dict(task_id='5', seed=42, messages=[{'role':'assistant'}]))
(r/'calls.jsonl').write_text('{"call_id":1,"row":"5-S1","reserve_usd":"0.02"}\\n'
    '{"call_id":1,"actual_usd":"0.01","input_tokens":10,"output_tokens":2,"cached_input_tokens":0}\\n')
write_json(p/'outcome.json', finish(p))
""")
    real_popen = subprocess.Popen
    launches = []

    def offline_worker(command, **kwargs):
        launches.append(command)
        return real_popen([sys.executable, str(child), str(workspace / str(identifier))], **kwargs)

    monkeypatch.setattr(jobs, "runtime_gaps", lambda arms, enabled: [])
    monkeypatch.setattr(subprocess, "Popen", offline_worker)
    assert jobs.launch(TESTER, identifier, True)["state"] == "running"
    assert jobs.launch(TESTER, identifier, True)["state"] in ("running", "completed")
    deadline = time.monotonic() + 10
    while (current := jobs.list_jobs(TESTER, True)["items"][0])[
        "state"
    ] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert current["state"] == "completed" and current["finished"] == 1
    group = current["result"]["groups"][0]
    assert group["passed"] == group["avg_steps"] == 1 and group["output_tokens"] == 2
    assert group["cost_per_success"] == 0.01
    assert jobs.launch(TESTER, identifier, True)["state"] == "completed" and len(launches) == 1


def test_dead_worker_is_visible_and_never_replayed(workspace):
    view = jobs.preview(TESTER, selection(), False)
    directory = workspace / view["id"]
    (directory / "lock").touch()
    (directory / "started").touch()
    assert jobs.launch(TESTER, UUID(view["id"]), False)["state"] == "interrupted"


def test_http_preview_and_offline_launch_gate(workspace):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aftercare.auth import current_user
    from aftercare.evaluation.api import router

    app = FastAPI()
    app.state.settings = SimpleNamespace(model_enabled=False)
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    base = "/api/evaluation/campaign-jobs"
    assert client.get(base).status_code == 401
    app.dependency_overrides[current_user] = lambda: TESTER
    assert len(client.get(base + "/catalog").json()["tasks"]) == 40
    created = client.post(base + "/preview", json=selection().model_dump())
    assert created.status_code == 201
    path = base + "/" + created.json()["id"] + "/start"
    assert client.post(path, json={}).status_code == 422
    assert client.post(path, json={"confirm_paid": True}).status_code == 503
    saved = client.get(base)
    assert saved.headers["Cache-Control"] == "no-store"
    assert saved.json()["items"][0]["state"] == "preview"
