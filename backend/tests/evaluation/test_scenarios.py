from datetime import timedelta
from types import SimpleNamespace

import pytest
from aftercare.auth import UserContext, current_user
from aftercare.business.errors import BusinessError
from aftercare.business.rules import now
from aftercare.db import Base
from aftercare.evaluation.api import router
from aftercare.evaluation.metrics import METRICS_VERSION
from aftercare.evaluation.scenarios import UNREGISTERED
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import EvaluationResult, EvaluationReview, Experiment
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

TESTER = UserContext(user_id="tester", role="tester")
CUSTOMER = UserContext(user_id="customer", role="customer", customer_id="C001")
BASE = "/evaluation/experiments/experiment/scenario-metrics"


@pytest.fixture
def evaluations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scenarios.sqlite'}")
    Base.metadata.create_all(engine)
    stamp = now()
    config = {
        "source": "fixture",
        "dataset_id": "v0.2",
        "metrics_version": METRICS_VERSION,
        "mode": "single",
        "strategy": "routed",
    }
    with Session(engine) as db, db.begin():
        db.add(
            Experiment(
                id="experiment",
                name="scenarios",
                status="COMPLETED",
                version=1,
                config=config,
                created_by=TESTER.user_id,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        for ident, case, repetition, family, intent, status, active in (
            ("a1", "A", 1, "D_OPEN_BOX", "damaged", "NEEDS_REVIEW", 1),
            ("a2", "A", 2, "D_OPEN_BOX", "damaged", "TIMEOUT", 9),
            ("b1", "B", 1, "W_NO_PHOTOS", "wrong_item", "NOT_RUN", None),
            ("c1", "C", 1, None, "refund_status", "PASSED", 70),
        ):
            actual = None
            checks = []
            trace = []
            reference = {
                "intent": intent,
                "required_policy_ids": ["POL02"],
                "required_facts": ["refund_ledger"],
                "tool_decisions": [
                    {"allowed_tools": ["orders"], "required_arguments": {"order_id": "O1"}}
                ],
            }
            if family is not None:
                reference["family"] = family
            if active is not None:
                actual = {
                    "observed_source": "application_offline",
                    "timings": {"active_ms": active},
                    "elapsed_ms": active * 2,
                    "model_audit_complete": ident != "a2",
                }
            if ident == "a1":
                actual["citation_audit"] = [
                    {"key": "p2:v1:0", "applicable": True},
                    {"key": "p3:v1:0", "applicable": False},
                ]
                actual["financial_safety"] = {"status": "PASS", "duplicates": 0}
                actual["draft_safety"] = ["FAIL"]
                checks = [{"path": "output.status", "status": "PASS"}]
                trace = [
                    {"kind": "retrieval", "payload": {"fused": ["POL02"]}},
                    {
                        "kind": "tool",
                        "payload": {
                            "name": "orders",
                            "status": "SUCCEEDED",
                            "schema_valid": True,
                            "arguments": {"order_id": "O1"},
                        },
                    },
                    {"kind": "tool", "payload": {"name": "facts", "status": "SUCCEEDED"}},
                    {"kind": "draft_rejected", "payload": {}},
                    {
                        "kind": "intent_probe",
                        "payload": {"intent": "damaged", "status": "OBSERVED"},
                    },
                ]
            if ident == "a2":
                actual["model_attempts"] = [
                    {
                        "logical_id": "x",
                        "ordinal": 1,
                        "cycle": 0,
                        "status": "UNKNOWN",
                        "estimated_cost_usd": None,
                    }
                ]
            db.add(
                EvaluationResult(
                    id=ident,
                    experiment_id="experiment",
                    case_id=case,
                    repetition=repetition,
                    intent=intent,
                    status=status,
                    version=1,
                    expected=reference,
                    actual=actual,
                    checks=checks,
                    trace=trace,
                    quota={},
                    started_at=stamp if active is not None else None,
                    finished_at=stamp + timedelta(milliseconds=active)
                    if active is not None
                    else None,
                )
            )
        for version, decision, label in ((1, "FAIL", "FAIL"), (2, "PASS", "PASS")):
            db.add(
                EvaluationReview(
                    id=f"review{version}",
                    result_id="a1",
                    version=version,
                    reviewer_id=TESTER.user_id,
                    decision=decision,
                    notes="saved review",
                    annotations={
                        "tools": {"0": label},
                        "citations": {"p2:v1:0": label, "p3:v1:0": "UNKNOWN"},
                    },
                    created_at=stamp,
                )
            )
    yield EvaluationService(engine, SimpleNamespace(profile="test"))
    engine.dispose()


def _summary(service, **scope):
    return service.scenario_metrics(TESTER, "experiment", **scope)


def _metrics(report):
    return {item["key"]: item for item in report["metrics"]}


def _contrib(service, summary, key, **page):
    return _summary(
        service,
        **summary["scope"],
        basis_revision=summary["basis_revision"],
        metric_key=key,
        **page,
    )


def test_scenario_scope_metric_parity_and_historical_unregistered(evaluations):
    summary = _summary(evaluations)
    by_key = _metrics(summary)
    for metric in evaluations.experiment(TESTER, "experiment")["metrics"]:
        scoped = by_key[metric["key"]]
        assert {key: scoped[key] for key in metric} == metric
        assert scoped["metric_version"] == METRICS_VERSION
        assert scoped["drilldown_supported"]
    assert {item["key"] for item in summary["scenario_options"]} == {
        "D_OPEN_BOX",
        "W_NO_PHOTOS",
        UNREGISTERED,
    }
    assert summary["scope_counts"] == {
        "planned": 4,
        "started": 3,
        "finished": 3,
        "not_run": 1,
        "failed": 0,
        "timed_out": 1,
        "cancelled": 0,
        "unique_cases": 3,
        "repeated_runs": 1,
    }
    selected = _summary(evaluations, scenario="D_OPEN_BOX")
    row = _metrics(selected)["task_success"]
    assert (row["value"], row["denominator"], row["measured_samples"], row["unique_cases"]) == (
        0.5,
        2,
        2,
        1,
    )
    assert _summary(evaluations, scenario=UNREGISTERED)["scope_counts"]["planned"] == 1
    empty = _summary(evaluations, scenario="D_OPEN_BOX", intent="wrong_item")
    assert empty["status"] == "AVAILABLE" and empty["scope_counts"]["planned"] == 0
    assert all(row["value"] is None for row in empty["metrics"])
    with pytest.raises(BusinessError, match="没有登记"):
        _summary(evaluations, scenario="not-known")


def test_contributor_subitems_percentile_safety_and_cost(evaluations):
    summary = _summary(evaluations, scenario="D_OPEN_BOX")
    task = _contrib(evaluations, summary, "task_success", bucket="included", limit=1, offset=1)
    assert task["total"] == 2
    assert task["bucket_counts"] == {"included": 2, "attention": 1, "unknown": 0, "excluded": 0}
    assert task["items"][0]["result_id"] == "a2"
    assert task["items"][0]["contributions"][0]["denominator"] == 1
    tool = _contrib(evaluations, summary, "tool_parameter_correctness")
    assert tool["metric"]["denominator"] == 1 and tool["metric"]["unique_cases"] == 1
    assert [item["reason"] for item in tool["items"][0]["contributions"]] == [
        "TOOL_REVIEW_PASS",
        "OBSERVATION_MISSING",
    ]
    cite = _contrib(evaluations, summary, "citation_support", bucket="unknown")
    assert cite["bucket_counts"]["included"] == cite["bucket_counts"]["unknown"] == 1
    assert cite["items"][0]["review_version"] == 2
    assert cite["items"][0]["contributions"][1]["reason"] == "HUMAN_REVIEW_UNKNOWN"
    assert cite["items"][0]["contributions"][1]["evidence"]["citation_key"] == "p3:v1:0"
    elapsed = _contrib(evaluations, summary, "active_ms_95")
    assert elapsed["metric"]["value"] == 9
    assert [item["contributions"][0]["evidence"]["rank"] for item in elapsed["items"]] == [1, 2]
    cost = _contrib(evaluations, summary, "cost")
    assert cost["metric"]["value"] is None
    assert cost["metric"]["evidence_unknown"] == 1 and cost["metric"]["unknown_unit"] == "run"
    assert cost["items"][0]["contributions"][0]["reason"] == "NO_MODEL_CALLS"
    assert {item["reason"] for item in cost["items"][1]["contributions"]} == {
        "AUDIT_MISSING",
        "COST_UNKNOWN",
    }
    zero = _metrics(_summary(evaluations, scenario=UNREGISTERED))["cost"]
    assert zero["value"] == 0 and zero["evidence_unknown"] == 0
    for key, n in (
        ("model_unsafe_suggestion_rate", 1),
        ("draft_rule_blocks", 1),
        ("final_violation_rate", 0),
    ):
        assert _contrib(evaluations, summary, key)["metric"]["numerator"] == n
    group = _contrib(evaluations, summary, "independent_case_consistency")
    assert group["metric"]["measured_samples"] == group["metric"]["unique_cases"] == 1
    assert all(
        item["contributions"][0]["evidence"]["repetitions"] == [1, 2] for item in group["items"]
    )


def test_snapshot_changes_even_when_result_versions_do_not(evaluations):
    original = _summary(evaluations, scenario="D_OPEN_BOX")
    with Session(evaluations.engine) as db, db.begin():
        row = db.get(EvaluationResult, "a2")
        row.actual = {**row.actual, "timings": {"active_ms": 11}}
    with pytest.raises(BusinessError) as error:
        _contrib(evaluations, original, "active_ms_95")
    assert error.value.code == "METRIC_BASIS_CHANGED" and error.value.status == 409
    next_summary = _summary(evaluations, scenario="D_OPEN_BOX")
    assert _metrics(next_summary)["active_ms_95"]["value"] == 11
    with Session(evaluations.engine) as db, db.begin():
        db.get(EvaluationReview, "review2").notes = "updated review observation"
    with pytest.raises(BusinessError) as error:
        _contrib(evaluations, next_summary, "citation_support")
    assert error.value.code == "METRIC_BASIS_CHANGED"


@pytest.mark.parametrize("definition", [None, "older-definition"])
def test_historical_definition_unavailable_and_never_inferred(evaluations, definition):
    with Session(evaluations.engine) as db, db.begin():
        row = db.get(Experiment, "experiment")
        config = dict(row.config)
        config.pop("metrics_version")
        if definition is not None:
            config["metrics_version"] = definition
        row.config = config
    summary = _summary(evaluations)
    assert summary["status"] == "HISTORICAL_DEFINITION_UNAVAILABLE"
    assert summary["frozen_metrics_version"] == definition
    assert summary["computed_metrics_version"] == METRICS_VERSION
    assert summary["metrics"] == [] and len(summary["basis_revision"]) == 64
    assert evaluations.experiment(TESTER, "experiment")["metrics"]
    with pytest.raises(BusinessError) as error:
        _contrib(evaluations, summary, "task_success")
    assert error.value.code == "METRIC_DEFINITION_UNAVAILABLE"


def test_route_authorization_no_store_and_conflict(evaluations):
    app = FastAPI()
    app.include_router(router)
    app.state.evaluation = evaluations
    app.dependency_overrides[current_user] = lambda: CUSTOMER
    with TestClient(app) as client:
        for route in (BASE, BASE + "/task_success/contributors?basis_revision=" + "0" * 64):
            denied = client.get(route)
            assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
        app.dependency_overrides[current_user] = lambda: TESTER
        response = client.get(BASE)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert client.get("/evaluation/capabilities").json()["eval_scenario_metrics"] is True
        invalid = client.get(BASE, params={"scenario": "absent"})
        assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
        contributors = BASE + "/task_success/contributors"
        matched = client.get(
            contributors, params={"basis_revision": response.json()["basis_revision"]}
        )
        assert matched.status_code == 200 and matched.headers["cache-control"] == "no-store"
        stale = client.get(contributors, params={"basis_revision": "0" * 64})
        assert stale.status_code == 409 and stale.headers["cache-control"] == "no-store"
        assert stale.json()["detail"]["code"] == "METRIC_BASIS_CHANGED"


def test_load_and_intent_contributions_are_not_per_case_rates(evaluations):
    with Session(evaluations.engine) as db, db.begin():
        start = now()
        for ident, intent, span, operations in (
            ("a1", "damaged", 1000, [{"ok": True, "elapsed_ms": 5}]),
            ("a2", "wrong_item", 2000, [{"ok": False, "elapsed_ms": 20}, {"ok": True}]),
        ):
            row = db.get(EvaluationResult, ident)
            row.started_at = start
            row.finished_at = start + timedelta(milliseconds=span)
            row.actual = {**row.actual, "load_operations": operations, "load_planned": 2}
            row.trace = [
                *row.trace,
                {"kind": "intent_probe", "payload": {"intent": intent, "status": "OBSERVED"}},
            ]
    summary = _summary(evaluations, scenario="D_OPEN_BOX")
    metrics = _metrics(summary)
    speed = _contrib(evaluations, summary, "load_operations_per_second")
    assert (
        speed["metric"]["value"],
        speed["metric"]["denominator_unit"],
        speed["metric"]["denominator"],
        speed["metric"]["measured_samples"],
    ) == (
        1.5,
        "millisecond",
        2000,
        3,
    )
    assert all(item["contributions"][-1]["reason"] == "LOAD_INTERVAL" for item in speed["items"])
    assert metrics["load_peak_concurrency"]["value"] == 2
    assert metrics["load_latency_p95"]["value"] == 20
    assert metrics["load_latency_p95"]["evidence_unknown"] == 2
    assert metrics["load_operation_success"]["value"] == pytest.approx(2 / 3)
    assert metrics["load_operation_success"]["unknown"] == 1
    matrix = _contrib(evaluations, summary, "intent_macro_f1")
    assert matrix["metric"]["value"] == pytest.approx(1 / 3)
    assert [row["contributions"][0]["evidence"]["observed"] for row in matrix["items"]] == [
        "damaged",
        "wrong_item",
    ]
    assert all("numerator" not in row["contributions"][0] for row in matrix["items"])


def test_compare_rejects_a_different_frozen_metric_definition(evaluations):
    with Session(evaluations.engine) as db, db.begin():
        original = db.get(Experiment, "experiment")
        db.add(
            Experiment(
                id="older",
                name="older",
                status=original.status,
                version=1,
                config={**original.config, "metrics_version": "ac-metrics-1"},
                created_by=TESTER.user_id,
                created_at=original.created_at,
                updated_at=original.updated_at,
            )
        )
        for source in db.query(EvaluationResult).filter_by(experiment_id=original.id):
            db.add(
                EvaluationResult(
                    id=f"old-{source.id}",
                    experiment_id="older",
                    case_id=source.case_id,
                    repetition=source.repetition,
                    intent=source.intent,
                    status=source.status,
                    version=1,
                    expected=source.expected,
                    actual=source.actual,
                    checks=source.checks,
                    trace=source.trace,
                    quota={},
                    started_at=source.started_at,
                    finished_at=source.finished_at,
                )
            )
    compared = evaluations.compare(TESTER, "experiment", "older")
    assert compared == {
        "compatible": False,
        "reasons": ["冻结条件不同 · metrics_version"],
        "rows": [],
        "metrics": [],
    }


def test_measured_unknown_checks_and_cost_still_enter_their_denominators(evaluations):
    with Session(evaluations.engine) as db, db.begin():
        row = db.get(EvaluationResult, "a2")
        row.checks = [{"status": "UNKNOWN", "path": "x"}, {"status": "UNKNOWN", "path": "y"}]
        row.expected = {**row.expected, "family": "ONLY_UNKNOWN"}
    summary = _summary(evaluations, scenario="ONLY_UNKNOWN")
    for key, count in (("check_unknown", 2), ("cost_unknown", 1)):
        report = _contrib(evaluations, summary, key, bucket="included")
        metric = report["metric"]
        assert metric["numerator"] == metric["denominator"] == metric["measured_samples"] == count
        assert metric["unknown"] == metric["evidence_unknown"] == count
        assert metric["unknown_includes_measured"] is True
        assert report["bucket_counts"]["included"] == report["bucket_counts"]["unknown"] == 1
        assert report["items"][0]["result_id"] == "a2"
    assert _metrics(summary)["final_violation_rate"]["measured_samples"] == 0
    assert _metrics(summary)["final_violation_rate"]["unique_cases"] == 0
    assert _metrics(summary)["final_violation_rate"]["unknown_includes_measured"] is False
