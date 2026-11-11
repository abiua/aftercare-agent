"""Describe native proposal-gate outcomes against independently verified option labels."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from re import fullmatch
from typing import Literal

from aftercare.business.rules import digest

VERSION = "chat-option-gate-accuracy-v1"
LABEL_SCHEMA = "chat-blind-labels-v2"
GATE_SCHEMA = "chat-blind-option-gates-v1"
_EXECUTING = {"REFUND", "REPLACE"}


@dataclass(frozen=True)
class VerifiedLabelSource:
    """Issued by the caller's independent source verification, outside the tester payload."""

    task_id: str
    labels_sha256: str
    verification_record_sha256: str
    kind: Literal["INDEPENDENT_HUMAN", "CONTROL"]


@dataclass(frozen=True)
class SealedReview:
    task_id: str
    input: dict
    images: dict
    candidates: list[dict]
    initial: dict
    exposure: dict
    labels: dict
    registration: dict
    source: VerifiedLabelSource | None
    registration_sha256: str


def _sha(value):
    return isinstance(value, str) and fullmatch("[a-f0-9]{64}", value) is not None


def _sealed(review, plan_sha256, controls_sha256, scope_sha256):
    initial, exposure, labels, registration = (
        review.initial,
        review.exposure,
        review.labels,
        review.registration,
    )
    input_sha = digest(review.input)
    bundle = digest(review.candidates)
    private = {
        key: registration[key] for key in ("plan_sha256", "controls_sha256", "scope_sha256", "arms")
    }
    if "option_gate_schema_version" in registration:
        private["option_gate_schema_version"] = registration["option_gate_schema_version"]
        private["option_gates_sha256"] = registration["option_gates_sha256"]
        if private["option_gates_sha256"] != digest(private["arms"]):
            return False
    return (
        digest(registration) == review.registration_sha256
        and registration["task_id"] == review.task_id
        and registration["plan_sha256"] == plan_sha256
        and registration["controls_sha256"] == controls_sha256
        and registration["scope_sha256"] == scope_sha256
        and initial["sha256"] == digest([input_sha, initial["payload"]])
        and exposure["initial_sha256"] == initial["sha256"]
        and exposure["candidates_sha256"] == bundle
        and exposure["sha256"] == digest([review.task_id, initial["sha256"], bundle])
        and labels["exposure_sha256"] == exposure["sha256"]
        and labels["sha256"] == digest([exposure["sha256"], labels["payload"]])
        and registration["source_sha256"]
        == digest(
            ["chat-blind-pg-registration-v1", private, scope_sha256, input_sha, review.images]
        )
    )


def _ordered_native_timestamps(review):
    try:
        timestamps = [
            datetime.fromisoformat(stage["created_at"])
            for stage in (review.initial, review.exposure, review.labels)
        ]
        return (
            all(timestamp.utcoffset() is not None for timestamp in timestamps)
            and timestamps[0] <= timestamps[1] <= timestamps[2]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _indexed(items, key):
    index = {key(item): item for item in items}
    if len(index) != len(items):
        raise ValueError("duplicate sealed review reference")
    return index


def _options(review, row, plan_sha, controls_sha):
    registration = review.registration
    if not _sealed(review, plan_sha, controls_sha, row["input_sha256"]):
        return "INCOMPATIBLE_SEAL_OR_INPUT", []
    arm = _indexed(registration["arms"], lambda item: item["row_id"]).get(row["id"])
    if arm is None or arm["arm"] != row["arm"]:
        return "INCOMPATIBLE_ARM", []
    if arm["status"] == "UNKNOWN":
        return "UNKNOWN_RUN", []
    if arm["status"] not in {"COMPLETED", "NEEDS_REVIEW", "WAITING_INPUT"}:
        return "RUN_NOT_REVIEWABLE", []
    if arm["candidate_status"] != "AVAILABLE":
        return "CANDIDATE_UNAVAILABLE", []
    candidate = _indexed(review.candidates, lambda item: item["id"]).get(arm["blind_candidate_id"])
    if candidate is None or candidate["status"] != "AVAILABLE":
        return "INCOMPATIBLE_CANDIDATE", []
    labels = review.labels["payload"]
    if labels.get("label_schema_version") != LABEL_SCHEMA:
        return "MISSING_V2_LABELS", []
    if not _ordered_native_timestamps(review):
        return "UNVERIFIED_REVIEW_CHRONOLOGY", []
    if review.initial["payload"].get("judgment") != "DECIDABLE":
        return "UNDETERMINED_INITIAL", []
    if review.source is None:
        return "UNVERIFIED_LABEL_SOURCE", []
    if review.source.kind == "CONTROL":
        return "CONTROL", []
    if (
        review.source.kind != "INDEPENDENT_HUMAN"
        or review.source.task_id != review.task_id
        or review.source.labels_sha256 != review.labels["sha256"]
        or not _sha(review.source.verification_record_sha256)
    ):
        return "UNVERIFIED_LABEL_SOURCE", []
    if registration.get("option_gate_schema_version") != GATE_SCHEMA or not arm.get("option_gate"):
        return "MISSING_NATIVE_GATE", []
    native = arm["option_gate"]
    if (
        not _sha(arm["snapshot_sha256"])
        or native["snapshot_sha256"] != arm["snapshot_sha256"]
        or not _sha(native["checks_sha256"])
    ):
        return "INCOMPATIBLE_NATIVE_GATE", []
    gate = _indexed(native["options"], lambda item: item["option_id"])
    if (
        not isinstance(arm.get("snapshot_id"), str)
        or not arm["snapshot_id"]
        or len({item["native_option_id"] for item in native["options"]}) != len(gate)
        or any(
            item["option_id"] != digest([arm["snapshot_id"], item["native_option_id"]])[:32]
            for item in native["options"]
        )
    ):
        return "INCOMPATIBLE_NATIVE_GATE", []
    judgments = _indexed(
        labels["option_labels"], lambda item: (item["candidate_id"], item["option_id"])
    )
    options = _indexed(candidate["options"], lambda item: item["id"])
    if not gate.keys() <= options.keys():
        return "INCOMPATIBLE_NATIVE_GATE", []
    output = []
    for option_id, option in options.items():
        status = gate.get(option_id, {}).get("status")
        decision = judgments.get((candidate["id"], option_id), {}).get("decision")
        if option["action"] not in _EXECUTING or status == "NON_EXECUTING":
            exclusion = "NON_EXECUTING"
        elif status not in {"PASSED", "BLOCKED"}:
            exclusion = "MISSING_NATIVE_GATE"
        elif decision == "UNDETERMINED":
            exclusion = "UNKNOWN_OPTION_LABEL"
        elif decision not in {"ACCEPTABLE", "UNACCEPTABLE"}:
            exclusion = "MISSING_OPTION_LABEL"
        else:
            exclusion = None
        output.append(
            {
                "option_id": option_id,
                "native_gate": status,
                "judgment": decision,
                "exclusion": exclusion,
            }
        )
    return (None if output else "NO_OPTIONS"), output


def _rate(options, judgment, gate):
    eligible = [row for row in options if not row["exclusion"] and row["judgment"] == judgment]
    return {
        "status": "MEASURED" if eligible else "NOT_MEASURABLE",
        "numerator": sum(row["native_gate"] == gate for row in eligible),
        "denominator": len(eligible),
    }


def summarize_gate_accuracy(plan, plan_sha256, reviews_by_row):
    """One pinned plan only. Missing reviews remain explicit plan rows."""
    rows = plan["rows"]
    if not rows or digest(plan) != plan_sha256:
        raise ValueError("gate accuracy requires one intact nonempty plan")
    indexed = _indexed(rows, lambda item: item["id"])
    if not reviews_by_row.keys() <= indexed.keys():
        raise ValueError("review outside gate accuracy plan")
    _indexed(rows, lambda item: (item["case_id"], item["repetition"], item["arm"]))
    controls_sha = digest(plan["controls"])
    out = []
    for row in rows:
        review = reviews_by_row.get(row["id"])
        if str(plan["controls"].get("evidence_grade", "")).startswith("CONTROL"):
            reason, options = "CONTROL", []
        elif review is None:
            reason, options = "MISSING_REVIEW", []
        else:
            try:
                reason, options = _options(review, row, plan_sha256, controls_sha)
            except (KeyError, TypeError, ValueError):
                reason, options = "INCOMPATIBLE_REVIEW", []
        out.append({**row, "exclusion": reason, "options": options})

    def group_metrics(group):
        options = [option for row in group for option in row["options"]]
        return {
            "planned_rows": len(group),
            "native_program_gate_wrong_option_passed": _rate(options, "UNACCEPTABLE", "PASSED"),
            "native_program_gate_correct_option_blocked": _rate(options, "ACCEPTABLE", "BLOCKED"),
            "row_exclusions": dict(Counter(row["exclusion"] for row in group if row["exclusion"])),
            "option_exclusions": dict(
                Counter(row["exclusion"] for row in options if row["exclusion"])
            ),
        }

    arms = {
        arm: group_metrics([row for row in out if row["arm"] == arm])
        for arm in sorted({row["arm"] for row in out})
    }
    cases = {}
    for case_id, arm in sorted({(row["case_id"], row["arm"]) for row in out}):
        cases.setdefault(case_id, {})[arm] = group_metrics(
            [row for row in out if (row["case_id"], row["arm"]) == (case_id, arm)]
        )
    return {
        "version": VERSION,
        "scope": "NATIVE_PROGRAM_PROPOSAL_GATE_NOT_MODEL_SELF_REVIEW_OR_HUMAN_APPROVAL",
        "plan_protocol": plan["protocol"],
        "plan_sha256": plan_sha256,
        "controls_sha256": controls_sha,
        "label_schema": LABEL_SCHEMA,
        "native_gate_schema": GATE_SCHEMA,
        "arms": arms,
        "arms_scope": "AGGREGATE_ACROSS_CASES",
        "by_case_and_arm": cases,
        "rows": out,
    }
