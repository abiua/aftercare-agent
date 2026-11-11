import pytest

from aftercare.evaluation.gates import GatePolicy, evaluate
from aftercare.evaluation.metrics import extend_metrics


def _metrics(rows):
    return {
        row["key"]: row for row in extend_metrics(rows, known_cost=None, cost_denominator=len(rows))
    }


def _event(kind, **payload):
    return {"kind": kind, "payload": payload}


def _row(case, target, observed, **changes):
    return {
        "case_id": case,
        "status": "PASSED",
        "started_at": "start",
        "finished_at": "finish",
        "expected": {"intent": target},
        "actual": {},
        "checks": [],
        "reviews": [],
        "trace": [_event("intent_probe", source="test-double", status="OBSERVED", intent=observed)],
        **changes,
    }


def test_metrics_hand_calculated_denominators_and_abstention():
    rows = [
        _row("one", "damaged", "damaged"),
        _row("two", "wrong_item", "damaged"),
        _row("three", "refund_status", None),
        _row("four", "refund_status", None, trace=[]),
    ]
    metrics = _metrics(rows)
    intent = metrics["intent_accuracy"]
    assert (intent["numerator"], intent["denominator"], intent["unknown"]) == (1, 3, 1)
    assert intent["detail"]["refund_status"] == {"UNKNOWN": 1}
    assert metrics["intent_macro_f1"]["value"] == pytest.approx(2 / 9)
    assert metrics["final_violation_rate"]["value"] is None
    assert metrics["final_violation_rate"]["unknown"] == 4
    assert metrics["independent_case_consistency"]["denominator"] == 4


def test_tools_clarification_citation_safety_and_route_have_separate_sources():
    row = _row(
        "one",
        "damaged",
        "damaged",
        expected={
            "intent": "damaged",
            "required_policy_ids": ["POL06", "POL14"],
            "required_facts": ["receipt"],
            "tool_decisions": [
                {"allowed_tools": ["get_policy"], "required_arguments": {"version": "2"}}
            ],
            "clarification_decisions": [
                {"event_id": "e1", "missing_materials": ["ITEM"]},
                {"event_id": "e2", "missing_materials": []},
            ],
        },
        actual={
            "events": {
                "e1": {"output": {"missing_materials": ["ITEM"]}},
                "e2": {"output": {"missing_materials": ["PARCEL"]}},
            },
            "citation_audit": [{"key": "POL06@1", "applicable": False}],
            "draft_safety": ["FAIL", "PASS", "UNKNOWN"],
            "financial_safety": {"status": "PASS", "duplicates": 0},
            "fault_observed": True,
            "timings": {"active_ms": 19, "queue_ms": 5},
            "model_attempts": [
                {
                    "cycle": 0,
                    "logical_id": "x",
                    "ordinal": 1,
                    "alias": "economy",
                    "status": "FAILED",
                },
                {
                    "cycle": 0,
                    "logical_id": "x",
                    "ordinal": 2,
                    "alias": "analysis",
                    "status": "SUCCEEDED",
                },
            ],
        },
        checks=[{"path": "status", "status": "PASS"}],
        reviews=[{"decision": "PASS", "annotations": {"citations": {"POL06@1": "FAIL"}}}],
        trace=[
            _event("tool", name="get_policy", arguments={"version": "1"}, schema_valid=True),
            _event("retrieval", fused=["POL06"]),
            _event(
                "route", preferred="economy", selected="analysis", excluded={"economy": "LIMIT"}
            ),
        ],
    )
    metrics = _metrics([row])
    values = {key: metric["value"] for key, metric in metrics.items()}
    assert values["tool_choice_accuracy"] == 1
    assert values["tool_schema_validity"] == 1
    assert values["tool_parameter_correctness"] == 0
    assert values["clarification_recall"] == values["unnecessary_clarification_rate"] == 1
    assert values["required_policy_recall"] == values["citation_version_applicability"] == 0
    assert values["citation_support"] == 0 and values["key_fact_evidence_coverage"] is None
    assert values["model_unsafe_suggestion_rate"] == 0.5
    assert values["final_violation_rate"] == 0
    assert values["fault_recovery_rate"] == values["fallback_success_rate"] == 1
    assert values["routing_escalation_rate"] == 1 and values["active_ms_95"] == 19


def test_technical_gate_requires_independent_gold_complete_evidence_and_no_critical_failures():
    policy = GatePolicy.model_validate(
        {
            "id": "unit",
            "version": "1",
            "name": "fixture gate",
            "source": "unit-test rubric",
            "thresholds": {
                "min_task_success": 1,
                "min_independent_cases": 1,
                "require_live": False,
            },
            "required_checks": ["status"],
        }
    ).model_dump(mode="json")
    row = _row(
        "one",
        "damaged",
        "damaged",
        checks=[{"path": "status", "status": "PASS"}],
        actual={"financial_safety": {"status": "PASS"}},
    )
    counts = {"planned": 1, "started": 1, "finished": 1}
    metrics = [
        {"key": "coverage", "value": 1},
        {"key": "task_success", "value": 1},
        {"key": "final_violation_rate", "value": 0, "unknown": 0},
    ]

    def gate():
        return evaluate(policy, [row], counts, metrics, source="fixture", status="COMPLETED")

    assert gate()["status"] == "INCOMPLETE"
    row["expected"].update(
        human_review="VERIFIED",
        label_source="INDEPENDENT_HUMAN",
        reviewer_id="unit-reviewer",
        reviewed_at="2026-11-10T00:00:00Z",
    )
    assert gate()["status"] == "PASS"
    assert gate()["release_status"] == "PENDING_INDEPENDENT_APPROVAL"
    metrics[-1]["unknown"] = 1
    assert gate()["status"] == "INCOMPLETE"
    row["checks"].append({"path": "cap", "status": "FAIL", "severity": "critical"})
    assert gate()["status"] == "FAILED"
