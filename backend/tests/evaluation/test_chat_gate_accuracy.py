from dataclasses import replace

import pytest

from aftercare.business.rules import digest
from aftercare.evaluation.chat.gate_accuracy import (
    GATE_SCHEMA,
    LABEL_SCHEMA,
    SealedReview,
    VerifiedLabelSource,
    summarize_gate_accuracy,
)


def _fixture(
    *,
    status="NEEDS_REVIEW",
    schema=LABEL_SCHEMA,
    grade="REAL_PROVIDER_TEXT_ONLY",
    incorrect_opaque_id=False,
):
    scope = "a" * 64
    row = {"id": "row", "case_id": "case", "repetition": 1, "arm": "multi", "input_sha256": scope}
    plan = {
        "protocol": "chat-eval-v1",
        "controls": {"evidence_grade": grade},
        "rows": [row, {**row, "id": "unreviewed", "case_id": "other"}],
    }

    def option_id(index):
        if incorrect_opaque_id and index == 0:
            return "incorrect"
        return digest(["snapshot", f"native{index}"])[:32]

    candidates = [
        {
            "id": "candidate",
            "status": "AVAILABLE",
            "options": [
                {"id": option_id(n), "action": action}
                for n, action in enumerate(
                    ("REFUND", "REPLACE", "REFUND", "REFUND", "HUMAN_HANDOFF", "REFUND", "REFUND")
                )
            ],
        }
    ]
    initial = {"payload": {"judgment": "DECIDABLE"}, "created_at": "2026-11-11T12:00:00+08:00"}
    initial["sha256"] = digest([digest({}), initial["payload"]])
    exposure = {
        "initial_sha256": initial["sha256"],
        "candidates_sha256": digest(candidates),
        "created_at": "2026-11-11T04:01:00Z",
    }
    exposure["sha256"] = digest(["task", initial["sha256"], digest(candidates)])
    labels = {
        "created_at": "2026-11-11T04:02:00+00:00",
        "exposure_sha256": exposure["sha256"],
        "payload": {
            "label_schema_version": schema,
            "items": [{"candidate_id": "candidate", "decision": "ACCEPTABLE"}],
            "option_labels": [
                {"candidate_id": "candidate", "option_id": option_id(n), "decision": decision}
                for n, decision in enumerate(
                    (
                        "UNACCEPTABLE",
                        "ACCEPTABLE",
                        "UNACCEPTABLE",
                        "ACCEPTABLE",
                        "ACCEPTABLE",
                        "UNDETERMINED",
                    )
                )
            ],
        },
    }
    labels["sha256"] = digest([exposure["sha256"], labels["payload"]])
    options = [
        {"option_id": option_id(n), "native_option_id": f"native{n}", "status": gate}
        for n, gate in enumerate(
            ("PASSED", "BLOCKED", "BLOCKED", "PASSED", "NON_EXECUTING", "PASSED")
        )
    ]
    private = {
        "plan_sha256": digest(plan),
        "controls_sha256": digest(plan["controls"]),
        "scope_sha256": scope,
        "option_gate_schema_version": GATE_SCHEMA,
        "arms": [
            {
                "row_id": "row",
                "arm": "multi",
                "status": status,
                "candidate_status": "AVAILABLE",
                "blind_candidate_id": "candidate",
                "snapshot_id": "snapshot",
                "snapshot_sha256": "b" * 64,
                "option_gate": {
                    "snapshot_sha256": "b" * 64,
                    "checks_sha256": "c" * 64,
                    "options": options,
                },
            }
        ],
    }
    private["option_gates_sha256"] = digest(private["arms"])
    registration = {
        **private,
        "task_id": "task",
        "source_sha256": digest(["chat-blind-pg-registration-v1", private, scope, digest({}), {}]),
    }
    proof = VerifiedLabelSource("task", labels["sha256"], "e" * 64, "INDEPENDENT_HUMAN")
    return plan, SealedReview(
        "task",
        {},
        {},
        candidates,
        initial,
        exposure,
        labels,
        registration,
        proof,
        digest(registration),
    )


def _summarize(plan, review):
    return summarize_gate_accuracy(plan, digest(plan), {"row": review})


def test_native_gate_uses_option_units_and_retains_exclusions():
    plan, review = _fixture()
    report = _summarize(plan, review)
    arm = report["arms"]["multi"]
    for key in (
        "native_program_gate_wrong_option_passed",
        "native_program_gate_correct_option_blocked",
    ):
        assert arm[key] == {"status": "MEASURED", "numerator": 1, "denominator": 2}
    assert arm["row_exclusions"] == {"MISSING_REVIEW": 1}
    assert arm["option_exclusions"] == {
        "NON_EXECUTING": 1,
        "UNKNOWN_OPTION_LABEL": 1,
        "MISSING_NATIVE_GATE": 1,
    }
    assert len(report["rows"]) == 2


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"grade": "CONTROLLED_WORKER_CORE"}, "CONTROL"),
        ({"status": "UNKNOWN"}, "UNKNOWN_RUN"),
        ({"status": "FAILED"}, "RUN_NOT_REVIEWABLE"),
        ({"status": "CANCELED"}, "RUN_NOT_REVIEWABLE"),
        ({"status": "NOT_RUN"}, "RUN_NOT_REVIEWABLE"),
        ({"incorrect_opaque_id": True}, "INCOMPATIBLE_NATIVE_GATE"),
        ({"schema": "chat-blind-labels-v1"}, "MISSING_V2_LABELS"),
    ],
)
def test_missing_qualification_never_contributes_to_rate(kwargs, reason):
    plan, review = _fixture(**kwargs)
    report = _summarize(plan, review)
    assert report["rows"][0]["exclusion"] == reason
    assert report["arms"]["multi"]["native_program_gate_wrong_option_passed"]["denominator"] == 0


def test_external_proof_and_frozen_hashes_are_required():
    plan, review = _fixture()
    assert _summarize(plan, replace(review, source=None))["rows"][0]["exclusion"] == (
        "UNVERIFIED_LABEL_SOURCE"
    )
    assert (
        _summarize(plan, replace(review, registration_sha256="f" * 64))["rows"][0]["exclusion"]
        == "INCOMPATIBLE_SEAL_OR_INPUT"
    )
    assert (
        _summarize(plan, replace(review, exposure={**review.exposure, "sha256": "f" * 64}))["rows"][
            0
        ]["exclusion"]
        == "INCOMPATIBLE_SEAL_OR_INPUT"
    )
    with pytest.raises(ValueError, match="outside"):
        summarize_gate_accuracy(plan, digest(plan), {"unknown-row": review})
    with pytest.raises(ValueError, match="intact"):
        summarize_gate_accuracy(plan, "f" * 64, {})


def test_legacy_native_mapping_stays_readable_but_unmeasurable():
    plan, review = _fixture()
    registration = dict(review.registration)
    registration.pop("option_gate_schema_version")
    registration.pop("option_gates_sha256")
    registration["arms"] = [
        {key: value for key, value in row.items() if key != "option_gate"}
        for row in registration["arms"]
    ]
    private = {
        key: registration[key] for key in ("plan_sha256", "controls_sha256", "scope_sha256", "arms")
    }
    registration["source_sha256"] = digest(
        [
            "chat-blind-pg-registration-v1",
            private,
            private["scope_sha256"],
            digest(review.input),
            review.images,
        ]
    )
    report = _summarize(
        plan,
        replace(review, registration=registration, registration_sha256=digest(registration)),
    )
    assert report["rows"][0]["exclusion"] == "MISSING_NATIVE_GATE"
    assert report["arms"]["multi"]["native_program_gate_correct_option_blocked"] == {
        "status": "NOT_MEASURABLE",
        "numerator": 0,
        "denominator": 0,
    }


@pytest.mark.parametrize("timestamp", [None, "2026-11-11T04:01:00", "2026-11-11T03:59:00Z"])
def test_missing_naive_or_out_of_order_native_timestamps_are_excluded(timestamp):
    plan, review = _fixture()
    exposure = {**review.exposure, "created_at": timestamp}
    assert _summarize(plan, replace(review, exposure=exposure))["rows"][0]["exclusion"] == (
        "UNVERIFIED_REVIEW_CHRONOLOGY"
    )


def test_case_arm_strata_do_not_pool_measured_and_excluded_cases():
    plan, review = _fixture()
    report = _summarize(plan, review)
    rate = "native_program_gate_wrong_option_passed"
    assert report["arms_scope"] == "AGGREGATE_ACROSS_CASES"
    assert report["arms"]["multi"]["planned_rows"] == 2
    assert report["by_case_and_arm"]["case"]["multi"][rate] == {
        "status": "MEASURED",
        "numerator": 1,
        "denominator": 2,
    }
    other = report["by_case_and_arm"]["other"]["multi"]
    assert other[rate] == {"status": "NOT_MEASURABLE", "numerator": 0, "denominator": 0}
    assert other["row_exclusions"] == {"MISSING_REVIEW": 1}
