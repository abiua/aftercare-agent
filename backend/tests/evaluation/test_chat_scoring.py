"""Exercise descriptive P5 scoring without a database or provider."""

from dataclasses import replace
from decimal import Decimal

import pytest

from aftercare.evaluation.chat.contracts import CandidateOption, InitialJudgment
from aftercare.evaluation.chat.metrics import summarize
from aftercare.evaluation.chat.scoring import PlannedRow, ScoreInput, score_row


def _option(action, lines, *, gaps=()):
    return CandidateOption(
        id="candidate-option",
        action=action,
        lines=lines,
        required_evidence=list(gaps),
        evidence_ids=[],
        policy_ids=[],
        rationale="fixture",
        alternatives=[],
        evidence_references=[],
        policy_references=[],
    )


def _input(plans, *options, **overrides):
    return ScoreInput(
        **{
            "row": PlannedRow("case", 1, "multi"),
            "run_status": "COMPLETED",
            "input_binding": "SAME_INPUT",
            "label_source": "INDEPENDENT",
            "option_policy": "ALL_OPTIONS",
            "judgment": InitialJudgment(
                expected_version=1, judgment="DECIDABLE", acceptable_plans=plans
            ),
            "options": options,
            **overrides,
        }
    )


def _refund(line_id, minimum, maximum):
    return {
        "action": "REFUND",
        "lines": [{"line_id": line_id, "amount_min_cents": minimum, "amount_max_cents": maximum}],
    }


def test_strict_matching_uses_the_same_accepted_plan_for_action_scope_and_amount():
    plans = [_refund("A", 100, 200), _refund("B", 300, 400)]
    wrong = _option("REFUND", [{"line_id": "A", "amount_cents": 300}])
    extra = _option(
        "REFUND", [{"line_id": "A", "amount_cents": 200}, {"line_id": "B", "amount_cents": 300}]
    )
    right = _option("REFUND", [{"line_id": "A", "amount_cents": 200}])
    row = _input(plans, wrong, extra, right)
    all_options = score_row(row)
    assert all_options.option_structure_matches == (False, False, True)
    assert all_options.action_scope_amount_match.match is False
    assert (
        score_row(replace(row, option_policy="ANY_OPTION")).action_scope_amount_match.match is True
    )


def test_replacement_quantity_and_evidence_text_diagnostic():
    plans = [
        {"action": "REPLACE", "lines": [{"line_id": "A", "quantity": 2}]},
        {
            "action": "REQUEST_EVIDENCE",
            "lines": [{"line_id": "A"}],
            "required_evidence": ["商品图", "包裹图"],
        },
    ]
    wrong_quantity = _option("REPLACE", [{"line_id": "A", "quantity": 1}])
    exact = _option("REQUEST_EVIDENCE", [{"line_id": "A"}], gaps=("包裹图", "商品图"))
    paraphrase = _option("REQUEST_EVIDENCE", [{"line_id": "A"}], gaps=("补拍包裹", "商品图"))
    result = score_row(_input(plans, wrong_quantity, exact))
    assert result.option_structure_matches == (False, True)
    assert result.exact_evidence_text_diagnostic.match is True
    result = score_row(_input(plans, paraphrase))
    assert result.action_scope_amount_match.match is True
    assert result.exact_evidence_text_diagnostic.match is False


def test_aggregate_keeps_excluded_rows_and_unknown_cost_separate():
    accepted = _input(
        [_refund("A", 100, 200)],
        _option("REFUND", [{"line_id": "A", "amount_cents": 100}]),
        estimated_cost_usd=Decimal("0.010"),
    )
    undecided = InitialJudgment(expected_version=1, judgment="UNDETERMINED")
    variants = [
        {"label_source": "CONTROL"},
        {"input_binding": "DIFFERENT_INPUT"},
        {"options": ()},
        {"judgment": undecided},
        {"run_status": "FAILED"},
        {"run_status": "UNKNOWN", "estimated_cost_usd": Decimal(0)},
        {"run_status": "NOT_RUN"},
        {"input_binding": "UNKNOWN"},
    ]
    inputs = (
        accepted,
        *(
            replace(
                replace(accepted, row=PlannedRow("case", index, "multi"), estimated_cost_usd=None),
                **variant,
            )
            for index, variant in enumerate(variants, 2)
        ),
    )
    report = summarize(
        inputs,
        protocol_version="chat-eval-v1",
        evidence_version="controlled-test-v1",
        planned_rows=tuple(item.row for item in inputs),
    )
    assert report.protocol_version == "chat-eval-v1"
    assert report.evidence_version == "controlled-test-v1"
    arm = report.arms[0]
    assert arm.planned_rows == len(report.rows) == 9 and arm.distinct_cases == 1
    assert arm.action_scope_amount_match == {
        "status": "MEASURED",
        "numerator": 1,
        "denominator": 1,
        "exclusions": {
            key: 1
            for key in (
                "CONTROL_LABEL",
                "DIFFERENT_INPUT",
                "FAILED",
                "NO_CANDIDATE",
                "NOT_RUN",
                "UNDETERMINED",
                "UNKNOWN",
                "UNKNOWN_INPUT",
            )
        },
    }
    assert arm.cost == {
        "status": "PARTIAL",
        "known_estimated_usd": "0.010",
        "known_rows": 1,
        "unknown_rows": 8,
    }
    assert report.not_measurable["wrong_option_passed"]["status"] == "NOT_MEASURABLE"


def test_no_valid_judgment_is_not_measurable_and_duplicate_plan_rows_rejected():
    row = _input([_refund("A", 100, 200)], run_status="NOT_RUN")
    metadata = {
        "protocol_version": "chat-eval-v1",
        "evidence_version": "controlled-test-v1",
        "planned_rows": (row.row,),
    }
    report = summarize((row,), **metadata)
    assert report.arms[0].action_scope_amount_match["status"] == "NOT_MEASURABLE"
    assert report.arms[0].cost["known_estimated_usd"] is None
    assert len(report.planned_rows_sha256) == 64
    with pytest.raises(ValueError, match="exactly match"):
        summarize(
            (row,), **{**metadata, "planned_rows": (row.row, PlannedRow("other", 1, "multi"))}
        )
    with pytest.raises(ValueError, match="Duplicate"):
        summarize((row, row), **metadata)
