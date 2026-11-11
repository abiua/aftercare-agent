"""Pure scoring of a frozen P5 judgment against one planned investigation row."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .contracts import AcceptablePlan, CandidateOption, InitialJudgment

SCORING_VERSION = "chat-eval-scoring-v1"
OptionPolicy = Literal["ANY_OPTION", "ALL_OPTIONS"]
LabelSource = Literal["INDEPENDENT", "CONTROL", "MISSING"]
InputBinding = Literal["SAME_INPUT", "DIFFERENT_INPUT", "UNKNOWN"]
RunStatus = Literal[
    "COMPLETED", "NEEDS_REVIEW", "WAITING_INPUT", "FAILED", "CANCELED", "UNKNOWN", "NOT_RUN"
]
Exclusion = Literal[
    "FAILED",
    "CANCELED",
    "UNKNOWN",
    "NOT_RUN",
    "DIFFERENT_INPUT",
    "UNKNOWN_INPUT",
    "NO_CANDIDATE",
    "CONTROL_LABEL",
    "NO_INDEPENDENT_LABEL",
    "UNDETERMINED",
    "NO_COMPARABLE_EVIDENCE_REQUEST",
]


@dataclass(frozen=True)
class PlannedRow:
    case_id: str
    repetition: int
    arm: str


@dataclass(frozen=True)
class ScoreInput:
    row: PlannedRow
    run_status: RunStatus
    input_binding: InputBinding
    label_source: LabelSource
    option_policy: OptionPolicy
    judgment: InitialJudgment | None = None
    run_reason: str | None = None
    options: tuple[CandidateOption, ...] = ()
    # Full row estimate from a reconciled attempt ledger. Partial or unknown totals use None.
    estimated_cost_usd: Decimal | None = None

    def __post_init__(self):
        if self.option_policy not in ("ANY_OPTION", "ALL_OPTIONS"):
            raise ValueError("An explicit option policy is required")
        if self.estimated_cost_usd is not None and (
            not self.estimated_cost_usd.is_finite() or self.estimated_cost_usd < 0
        ):
            raise ValueError("Cost must be a finite nonnegative estimate")


@dataclass(frozen=True)
class Contribution:
    match: bool | None
    exclusion: Exclusion | None = None


@dataclass(frozen=True)
class RowScore:
    row: PlannedRow
    run_status: RunStatus
    run_reason: str | None
    input_binding: InputBinding
    label_source: LabelSource
    option_policy: OptionPolicy
    action_scope_amount_match: Contribution
    exact_evidence_text_diagnostic: Contribution
    option_structure_matches: tuple[bool, ...]
    estimated_cost_usd: Decimal | None


def _plan_match(option: CandidateOption, plan: AcceptablePlan) -> bool:
    if option.action != plan.action:
        return False
    proposed = {line.line_id: line for line in option.lines}
    expected = {line.line_id: line for line in plan.lines}
    if len(proposed) != len(option.lines) or proposed.keys() != expected.keys():
        return False
    for line_id, line in proposed.items():
        target = expected[line_id]
        if option.action == "REFUND":
            if (
                line.amount_cents is None
                or line.quantity is not None
                or not target.amount_min_cents <= line.amount_cents <= target.amount_max_cents
            ):
                return False
        elif option.action == "REPLACE":
            if line.amount_cents is not None or line.quantity != target.quantity:
                return False
        elif line.amount_cents is not None or line.quantity is not None:
            return False
    return True


def _exclude(item: ScoreInput) -> Exclusion | None:
    """Return the first exclusion by run, input, candidate, and label precedence."""
    if item.run_status in ("FAILED", "CANCELED", "UNKNOWN", "NOT_RUN"):
        return item.run_status
    if item.input_binding == "DIFFERENT_INPUT":
        return "DIFFERENT_INPUT"
    if item.input_binding != "SAME_INPUT":
        return "UNKNOWN_INPUT"
    if not item.options:
        return "NO_CANDIDATE"
    if item.label_source == "CONTROL":
        return "CONTROL_LABEL"
    if item.label_source != "INDEPENDENT" or item.judgment is None:
        return "NO_INDEPENDENT_LABEL"
    if item.judgment.judgment == "UNDETERMINED":
        return "UNDETERMINED"
    return None


def score_row(item: ScoreInput) -> RowScore:
    cost = item.estimated_cost_usd if item.run_status not in ("UNKNOWN", "NOT_RUN") else None
    reason = _exclude(item)
    if reason is not None:
        excluded = Contribution(None, reason)
        return RowScore(
            item.row,
            item.run_status,
            item.run_reason,
            item.input_binding,
            item.label_source,
            item.option_policy,
            excluded,
            excluded,
            (),
            cost,
        )

    assert item.judgment is not None
    reduce_options = any if item.option_policy == "ANY_OPTION" else all
    matching_plans = [
        [plan for plan in item.judgment.acceptable_plans if _plan_match(option, plan)]
        for option in item.options
    ]
    matches = tuple(bool(plans) for plans in matching_plans)
    evidence_matches = [
        any(set(option.required_evidence) == set(plan.required_evidence) for plan in plans)
        for option, plans in zip(item.options, matching_plans, strict=True)
        if option.action == "REQUEST_EVIDENCE" and plans
    ]
    # Free text can be semantically equivalent without matching. No quality score uses this.
    diagnostic = (
        Contribution(reduce_options(evidence_matches))
        if evidence_matches
        else Contribution(None, "NO_COMPARABLE_EVIDENCE_REQUEST")
    )
    return RowScore(
        item.row,
        item.run_status,
        item.run_reason,
        item.input_binding,
        item.label_source,
        item.option_policy,
        Contribution(reduce_options(matches)),
        diagnostic,
        matches,
        cost,
    )
