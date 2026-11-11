"""Auditable descriptive P5 metrics. Historical evaluation uses its own aggregation."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from aftercare.business.rules import digest

from .scoring import (
    SCORING_VERSION,
    Contribution,
    OptionPolicy,
    PlannedRow,
    RowScore,
    ScoreInput,
    score_row,
)

_UNSUPPORTED = {
    "independent_full_acceptability": "STRUCTURED_EVIDENCE_REQUIREMENTS_NOT_AVAILABLE",
    "support_action_agreement": "SUPPORT_DECISION_INPUT_NOT_FROZEN",
    "support_amount_agreement": "SUPPORT_DECISION_INPUT_NOT_FROZEN",
    "support_evidence_agreement": "SUPPORT_DECISION_INPUT_NOT_FROZEN",
    "wrong_option_passed": "INDEPENDENT_OPTION_AND_GATE_LABELS_NOT_AVAILABLE",
    "correct_option_blocked": "INDEPENDENT_OPTION_AND_GATE_LABELS_NOT_AVAILABLE",
    "material_information_strata": "VERSIONED_EVIDENCE_DELTA_NOT_AVAILABLE",
    "critical_fact_omission": "STRUCTURED_CLAIM_FACT_LABELS_NOT_AVAILABLE",
    "policy_applicability": "STRUCTURED_CLAUSE_APPLICABILITY_LABELS_NOT_AVAILABLE",
    "policy_citation_support": "INDEPENDENT_CITATION_LABELS_NOT_AVAILABLE",
    "recheck_corrected": "INDEPENDENT_PRE_POST_CLAIM_LABELS_NOT_AVAILABLE",
    "recheck_introduced_error": "INDEPENDENT_PRE_POST_CLAIM_LABELS_NOT_AVAILABLE",
    "human_takeover": "TAKEOVER_EVENTS_NOT_AVAILABLE",
}


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    planned_rows: int
    distinct_cases: int
    run_statuses: dict[str, int]
    action_scope_amount_match: dict
    exact_evidence_text_diagnostic: dict
    cost: dict


@dataclass(frozen=True)
class ScoreReport:
    protocol_version: str
    evidence_version: str
    scoring_version: str
    planned_rows_sha256: str
    option_policy: OptionPolicy
    arms: tuple[ArmMetrics, ...]
    rows: tuple[RowScore, ...]
    not_measurable: dict


def _tally(rows: list[Contribution]) -> dict:
    exclusions = Counter(row.exclusion for row in rows if row.exclusion)
    numerator = sum(row.match is True for row in rows)
    denominator = sum(row.match is not None for row in rows)
    return {
        "status": "MEASURED" if denominator else "NOT_MEASURABLE",
        "numerator": numerator,
        "denominator": denominator,
        "exclusions": dict(sorted(exclusions.items())),
    }


def _arm(arm: str, rows: list[RowScore]) -> ArmMetrics:
    known = [row.estimated_cost_usd for row in rows if row.estimated_cost_usd is not None]
    return ArmMetrics(
        arm=arm,
        planned_rows=len(rows),
        distinct_cases=len({row.row.case_id for row in rows}),
        run_statuses=dict(sorted(Counter(row.run_status for row in rows).items())),
        action_scope_amount_match=_tally([row.action_scope_amount_match for row in rows]),
        exact_evidence_text_diagnostic=_tally([row.exact_evidence_text_diagnostic for row in rows]),
        cost={
            "status": "MEASURED"
            if len(known) == len(rows)
            else "PARTIAL"
            if known
            else "NOT_MEASURABLE",
            "known_estimated_usd": str(sum(known, Decimal(0))) if known else None,
            "known_rows": len(known),
            "unknown_rows": len(rows) - len(known),
        },
    )


def summarize(
    inputs: tuple[ScoreInput, ...],
    *,
    protocol_version: str,
    evidence_version: str,
    planned_rows: tuple[PlannedRow, ...],
) -> ScoreReport:
    """Preserve all supplied plan rows and keep results separate by arm and evidence version."""
    if not protocol_version or not evidence_version or not inputs:
        raise ValueError("A nonempty versioned evaluation plan is required")
    actual = {item.row: item for item in inputs}
    expected = set(planned_rows)
    if len(actual) != len(inputs) or len(expected) != len(planned_rows):
        raise ValueError("Duplicate planned row")
    if actual.keys() != expected:
        raise ValueError("Scoring rows must exactly match the registered plan")
    option_policies = {item.option_policy for item in inputs}
    if len(option_policies) != 1:
        raise ValueError("All rows must use the same frozen option policy")

    rows = tuple(score_row(actual[row]) for row in planned_rows)
    groups: dict[str, list[RowScore]] = {}
    for row in rows:
        groups.setdefault(row.row.arm, []).append(row)
    return ScoreReport(
        protocol_version=protocol_version,
        evidence_version=evidence_version,
        scoring_version=SCORING_VERSION,
        planned_rows_sha256=digest(
            [[row.case_id, row.repetition, row.arm] for row in planned_rows]
        ),
        option_policy=next(iter(option_policies)),
        arms=tuple(_arm(arm, group) for arm, group in sorted(groups.items())),
        rows=rows,
        not_measurable={
            metric: {"status": "NOT_MEASURABLE", "numerator": 0, "denominator": 0, "reason": reason}
            for metric, reason in _UNSUPPORTED.items()
        },
    )
