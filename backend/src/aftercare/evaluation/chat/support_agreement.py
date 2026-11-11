"""Descriptive agreement with a candidate-exposed support decision."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from aftercare.business.rules import digest
from aftercare.decisions.contracts import Candidate, DecisionRef, DecisionScope, Option, Review

from .scoring import PlannedRow

VERSION = "chat-support-agreement-v1"
Information = Literal["SAME_INFORMATION", "CHANGED_INFORMATION", "UNKNOWN"]


@dataclass(frozen=True)
class FrozenSnapshot:
    ref: DecisionRef
    conversation_id: str
    issue_id: str
    parent_id: str | None
    run_id: str | None
    origin: Literal["AGENT", "SUPPORT_MODIFICATION"]
    actor_id: str
    input: dict
    candidate: dict
    checks: dict
    expires_at: datetime

    def verify(self):
        scope = DecisionScope.model_validate(self.input)
        candidate = Candidate.model_validate(self.candidate)
        if (
            self.expires_at.utcoffset() is None
            or (self.conversation_id, self.issue_id) != (scope.conversation_id, scope.issue_id)
            or (self.run_id is not None) != (self.origin == "AGENT")
            or (self.run_id is not None and self.run_id != candidate.run_id)
            or digest(
                {
                    "id": self.ref.id,
                    "version": self.ref.version,
                    "parent_id": self.parent_id,
                    "run_id": self.run_id,
                    "origin": self.origin,
                    "actor_id": self.actor_id,
                    "input": self.input,
                    "candidate": self.candidate,
                    "checks": self.checks,
                    "expires_at": self.expires_at.astimezone(UTC).isoformat(),
                }
            )
            != self.ref.sha256
        ):
            raise ValueError("SNAPSHOT_BINDING_MISMATCH")
        return scope, candidate


@dataclass(frozen=True)
class FrozenSupportReview:
    id: str
    conversation_id: str
    issue_id: str
    snapshot_id: str
    resulting_snapshot_id: str
    actor_id: str
    request_sha256: str
    review: dict
    # The previous review ID is part of the original request digest. An export must
    # establish it from the recorded request/chain, including explicit null.
    expected_previous_review_id: str | None


@dataclass(frozen=True)
class AgreementInput:
    row: PlannedRow
    # The option is fixed by the evaluation plan before reading the support review.
    candidate_option_id: str | None
    expected_snapshot: DecisionRef | None
    expected_review_id: str | None
    expected_result: DecisionRef | None
    snapshot: FrozenSnapshot | None = None
    review: FrozenSupportReview | None = None
    result: FrozenSnapshot | None = None
    extraction_exclusion: str | None = None


@dataclass(frozen=True)
class Comparison:
    match: bool | None = None
    exclusion: str | None = None


@dataclass(frozen=True)
class AgreementRow:
    row: PlannedRow
    review_action: str | None
    information: Information
    action: Comparison
    refund_amount_by_line: Comparison


def _excluded(row, reason, review_action=None):
    comparison = Comparison(exclusion=reason)
    return AgreementRow(row, review_action, "UNKNOWN", comparison, comparison)


def _scope_key(scope):
    return (
        scope.conversation_id,
        scope.issue_id,
        scope.case_id,
        scope.order_id,
        scope.customer_id,
        frozenset(scope.line_ids),
    )


def _information(original, result):
    if original.frozen_context is None or result.frozen_context is None:
        return "UNKNOWN"
    before = {(ref.kind, ref.id): ref.sha256 for ref in original.input_refs}
    after = {(ref.kind, ref.id): ref.sha256 for ref in result.input_refs}
    if before != after:
        return "CHANGED_INFORMATION"
    if (
        original.input_sha256 == result.input_sha256
        and original.business_versions == result.business_versions
        and original.dependency_sha256 == result.dependency_sha256
    ):
        return "SAME_INFORMATION"
    return "UNKNOWN"


def _review_option(original, candidate, review, result, resulting_candidate):
    if review.action == "MODIFY":
        if (
            result.origin != "SUPPORT_MODIFICATION"
            or result.parent_id != original.ref.id
            or result.ref.version <= original.ref.version
            or resulting_candidate.run_id != candidate.run_id
            or resulting_candidate.options != (review.replacement,)
        ):
            raise ValueError("RESULT_BINDING_MISMATCH")
        return review.replacement
    if result.ref != original.ref:
        raise ValueError("RESULT_BINDING_MISMATCH")
    if review.action == "ACCEPT":
        selected = next(
            (option for option in candidate.options if option.id == review.option_id), None
        )
        if selected is None:
            raise ValueError("REVIEW_OPTION_MISSING")
        return selected
    return None


def native_review_request(payload: dict) -> Review:
    # These server-added fields remain in the private native record. Neither is
    # included in DecisionService's request digest.
    return Review.model_validate(
        {
            key: value
            for key, value in payload.items()
            if key not in {"program_check", "evidence_gaps"}
        }
    )


def score_agreement(item: AgreementInput) -> AgreementRow:
    if item.extraction_exclusion is not None:
        return _excluded(item.row, item.extraction_exclusion)
    snapshot, record, result = item.snapshot, item.review, item.result
    if snapshot is None:
        return _excluded(item.row, "NO_CANDIDATE")
    if record is None:
        return _excluded(item.row, "NO_SUPPORT_REVIEW")
    if result is None:
        return _excluded(item.row, "NO_RESULT_SNAPSHOT")
    try:
        original_scope, candidate = snapshot.verify()
        result_scope, resulting_candidate = result.verify()
        review = native_review_request(record.review)
        if (
            snapshot.origin != "AGENT"
            or snapshot.ref != item.expected_snapshot
            or result.ref != item.expected_result
            or record.id != item.expected_review_id
            or (record.snapshot_id, record.resulting_snapshot_id)
            != (snapshot.ref.id, result.ref.id)
            or (record.conversation_id, record.issue_id)
            != (snapshot.conversation_id, snapshot.issue_id)
            or _scope_key(original_scope) != _scope_key(result_scope)
            or digest(
                [
                    snapshot.ref.model_dump(mode="json"),
                    review.model_dump(mode="json"),
                    record.expected_previous_review_id,
                ]
            )
            != record.request_sha256
            or (review.action == "MODIFY" and record.actor_id != result.actor_id)
        ):
            return _excluded(item.row, "REVIEW_BINDING_MISMATCH")
        support = _review_option(snapshot, candidate, review, result, resulting_candidate)
    except (ValueError, TypeError):
        return _excluded(item.row, "INVALID_FROZEN_EVIDENCE")

    chosen = next(
        (option for option in candidate.options if option.id == item.candidate_option_id), None
    )
    if chosen is None:
        return _excluded(item.row, "NO_FROZEN_CANDIDATE_OPTION", review.action)
    support_action = (
        support.action
        if support is not None
        else ("REQUEST_EVIDENCE" if review.action == "REQUEST_EVIDENCE" else None)
    )
    if support_action is None:
        return _excluded(item.row, "NO_SUPPORT_BUSINESS_ACTION", review.action)
    information = (
        _information(original_scope, result_scope) if review.action == "MODIFY" else "UNKNOWN"
    )
    action = Comparison(match=chosen.action == support_action)
    if chosen.action != "REFUND" or not isinstance(support, Option) or support.action != "REFUND":
        amount = Comparison(exclusion="NOT_BOTH_REFUNDS")
    else:
        left = {line.line_id: line.amount_cents for line in chosen.lines}
        right = {line.line_id: line.amount_cents for line in support.lines}
        if left.keys() != right.keys():
            amount = Comparison(exclusion="DIFFERENT_REFUND_LINE_SCOPE")
        elif not left.keys() <= set(original_scope.line_ids):
            amount = Comparison(exclusion="REFUND_LINES_OUTSIDE_FROZEN_SCOPE")
        else:
            amount = Comparison(match=left == right)
    return AgreementRow(item.row, review.action, information, action, amount)


def _tally(comparisons):
    denominator = sum(row.match is not None for row in comparisons)
    return {
        "status": "MEASURED" if denominator else "NOT_MEASURABLE",
        "numerator": sum(row.match is True for row in comparisons),
        "denominator": denominator,
        "exclusions": dict(
            sorted(Counter(row.exclusion for row in comparisons if row.exclusion).items())
        ),
    }


def summarize_support_agreement(inputs, *, planned_rows):
    """Return every planned row and separate exposed review dispositions and information strata."""
    actual = {item.row: item for item in inputs}
    if (
        len(actual) != len(inputs)
        or len(set(planned_rows)) != len(planned_rows)
        or actual.keys() != set(planned_rows)
    ):
        raise ValueError("Agreement rows must exactly match the registered plan")
    rows = tuple(score_agreement(actual[row]) for row in planned_rows)
    groups = {}
    for row in rows:
        key = (row.row.arm, row.review_action or "UNAVAILABLE", row.information)
        groups.setdefault(key, []).append(row)
    return {
        "version": VERSION,
        "option_policy": "PREDECLARED_OPTION_ID",
        "plan_sha256": digest(
            [
                [
                    item.row.case_id,
                    item.row.repetition,
                    item.row.arm,
                    item.candidate_option_id,
                    item.expected_snapshot.model_dump(mode="json")
                    if item.expected_snapshot
                    else None,
                    item.expected_review_id,
                    item.expected_result.model_dump(mode="json") if item.expected_result else None,
                ]
                for item in (actual[row] for row in planned_rows)
            ]
        ),
        "rows": rows,
        "groups": [
            {
                "arm": arm,
                "review_action": review,
                "information": information,
                "planned_rows": len(group),
                "action_agreement": _tally([row.action for row in group]),
                "refund_amount_by_line_agreement": _tally(
                    [row.refund_amount_by_line for row in group]
                ),
            }
            for (arm, review, information), group in sorted(groups.items())
        ],
        "support_evidence_agreement": {
            "status": "NOT_MEASURABLE",
            "numerator": 0,
            "denominator": 0,
            "reason": "STRUCTURED_EVIDENCE_REQUIREMENTS_NOT_AVAILABLE",
        },
    }
