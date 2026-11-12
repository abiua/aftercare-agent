import json
from types import SimpleNamespace

import pytest

from aftercare.business.errors import BusinessError
from aftercare.evaluation.campaigns import REPORT_PATH, campaigns, read_campaign


TESTER = SimpleNamespace(role="tester")


@pytest.mark.parametrize("repaired", [False, True])
def test_official_campaign_projects_public_fields(tmp_path, repaired):
    report_path = tmp_path / REPORT_PATH
    report_path.parent.mkdir(parents=True)
    usage = {
        "agent_calls": 2,
        "user_calls": 1,
        "review_calls": 0,
        "agent_cost_usd": None,
        "user_cost_usd": "0.01",
        "review_cost_usd": 0,
    }
    row = {
        **usage,
        "task_id": "retail-task",
        "architecture": "strong_single",
        "seed": 42,
        "status": "completed",
        "official_reward": 1,
        "task_summary": "Customer needs to check their order.",
        "official_components": {"reference": "private"},
        "trace": "private",
    }
    source = {
        "schema_version": 1,
        "benchmark": "tau2-bench",
        "domain": "retail",
        "split": "test",
        "scope": "smoke",
        "revision": "official-revision",
        "agent_model": "agent-model",
        "user_model": "user-model",
        "task_ids": ["retail-task"],
        "trials_per_task": 1,
        "complete": False,
        "architectures": [{
            **usage,
            "architecture": "strong_single",
            "scheduled": 1,
            "completed": 1,
            "passed": 1,
            "failed": 0,
            "unscored": 0,
            "pass_rate": 1,
            "mean_official_reward": 1,
        }],
        "rows": [row],
        "answer_key": "private",
    }
    if repaired:
        primary = dict(source, rows=[dict(row, status="unknown", official_reward=None)])
        repair = dict(source, scope="repair", rows=[row])
        report_path.with_name("primary.json").write_text(json.dumps(primary))
        report_path.with_name("repair.json").write_text(json.dumps(repair))
        source.update(
            original_directory="full",
            repair_directory="full.repair",
            billing={"known_usd": "0.01", "unsettled_calls": 1, "unsettled_reserved_usd": "0.02"},
            rows=[dict(
                row,
                attempt_count=2,
                score_source="technical_retry",
                original_attempt=primary["rows"][0],
                repair_attempt=row,
            )],
        )
    report_path.write_text(json.dumps(source))
    report = read_campaign(tmp_path, TESTER, "tau-official")
    assert report["rows"][0]["task_summary"] == row["task_summary"]
    assert report["rows"][0]["agent_cost_usd"] is None
    assert "private" not in json.dumps(report)
    if repaired:
        assert report["rows"][0]["attempt_count"] == 2
        assert report["rows"][0]["original_attempt"]["status"] == "unknown"
        assert report["rows"][0]["repair_attempt"]["official_reward"] == 1
        assert report["billing"]["unsettled_calls"] == 1
        assert read_campaign(tmp_path, TESTER, "tau-official-primary")["rows"][0]["official_reward"] is None
        assert read_campaign(tmp_path, TESTER, "tau-official-repair")["scope"] == "repair"
    with pytest.raises(BusinessError, match="仅测试员"):
        campaigns(tmp_path, SimpleNamespace(role="customer"))
    with pytest.raises(BusinessError, match="没有找到"):
        read_campaign(tmp_path, TESTER, "p8-old")


def test_missing_official_campaign_ignores_old_reports(tmp_path):
    old_path = tmp_path / ".runtime/p8-old/plan.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("{}")
    assert campaigns(tmp_path, TESTER) == {"items": []}


def test_official_campaign_http_is_tester_only_and_no_store(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aftercare import config
    from aftercare.auth import current_user
    from aftercare.evaluation.api import router

    monkeypatch.setattr(config, "ROOT", tmp_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    app.dependency_overrides[current_user] = lambda: SimpleNamespace(role="support")
    denied = client.get("/evaluation/campaigns")
    assert denied.status_code == 403
    assert denied.headers["Cache-Control"] == "no-store"

    app.dependency_overrides[current_user] = lambda: TESTER
    empty = client.get("/evaluation/campaigns")
    assert empty.status_code == 200
    assert empty.json() == {"items": []}
    assert empty.headers["Cache-Control"] == "no-store"


def test_project_case_endpoints_are_retired():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aftercare.auth import current_user
    from aftercare.evaluation.api import router

    app = FastAPI()
    app.state.evaluation = SimpleNamespace(catalog=None)
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: TESTER
    client = TestClient(app)

    for path in ("datasets", "cases?dataset=v0.2", "experiments", "imports"):
        response = client.get(f"/evaluation/{path}")
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "EVALUATION_RETIRED"
