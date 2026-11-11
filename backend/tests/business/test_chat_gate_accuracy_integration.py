"""Verify the scorer reads the persisted review chain and excludes controlled labels."""

from dataclasses import replace
from datetime import UTC

from sqlalchemy.orm import Session
from test_business import TESTER
from test_chat_blind_registration import _register
from test_chat_blind_registration import paired as _paired
from test_chat_blind_review import BASE, _initial
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers
from test_conversations import system as _system

from aftercare.business.rules import digest
from aftercare.evaluation.chat.gate_accuracy import (
    SealedReview,
    VerifiedLabelSource,
    _options,
    summarize_gate_accuracy,
)
from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindLabels, BlindTask

client, system, configured, paired = _client, _system, _configured, _paired


def _native_row(row, *fields):
    created_at = row.created_at
    if created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=UTC)
    return {
        "created_at": created_at.isoformat(),
        **{field: getattr(row, field) for field in fields},
    }


def test_native_v2_labels_and_registered_option_gates_remain_control_evidence(paired):
    env, runtime, manifest, case, _, _ = paired
    task_id = _register(runtime, manifest, case)["id"]
    endpoint = f"{BASE}/{task_id}"
    initial_view = env.client.get(endpoint, headers=headers(TESTER)).json()
    sealed = env.client.post(
        f"{endpoint}/initial", json=_initial(initial_view["input"]), headers=headers(TESTER)
    )
    assert sealed.status_code == 200
    revealed = env.client.post(
        f"{endpoint}/reveal", json={"expected_version": 2}, headers=headers(TESTER)
    )
    assert revealed.status_code == 200
    candidates = revealed.json()["exposure"]["candidates"]
    labeled = env.client.post(
        f"{endpoint}/labels",
        json={
            "expected_version": 3,
            "label_schema_version": "chat-blind-labels-v2",
            "items": [
                {"candidate_id": candidate["id"], "decision": "UNDETERMINED"}
                for candidate in candidates
            ],
            "option_labels": [
                {
                    "candidate_id": candidate["id"],
                    "option_id": option["id"],
                    "decision": "UNDETERMINED",
                }
                for candidate in candidates
                for option in candidate["options"]
            ],
        },
        headers=headers(TESTER),
    )
    assert labeled.status_code == 200, labeled.text
    plan = manifest.read("plan.json")
    plan_sha = digest(plan)
    registration = manifest.read(f"blind-{task_id}.json")
    with Session(env.db) as db:
        task = db.get_one(BlindTask, task_id)
        assert task.source_sha256 == registration["source_sha256"]
        initial = _native_row(db.get_one(BlindInitial, task_id), "sha256", "payload")
        exposure = _native_row(
            db.get_one(BlindExposure, task_id), "sha256", "initial_sha256", "candidates_sha256"
        )
        labels = _native_row(
            db.get_one(BlindLabels, task_id), "sha256", "exposure_sha256", "payload"
        )
        control = VerifiedLabelSource(
            task_id, labels["sha256"], digest(["sqlite-controlled-integration", task_id]), "CONTROL"
        )
        review = SealedReview(
            task_id=task_id,
            input=task.input,
            images=task.image_objects,
            candidates=task.candidates,
            initial=initial,
            exposure=exposure,
            labels=labels,
            registration=registration,
            source=control,
            registration_sha256=digest(registration),
        )
    controls_sha = digest(plan["controls"])
    for row in plan["rows"]:
        # Exercise the native chain before the public summary's plan-wide control shortcut.
        assert _options(review, row, plan_sha, controls_sha) == ("CONTROL", [])
        broken = replace(review, exposure={**exposure, "initial_sha256": "0" * 64})
        assert _options(broken, row, plan_sha, controls_sha) == ("INCOMPATIBLE_SEAL_OR_INPUT", [])
    summary = summarize_gate_accuracy(plan, plan_sha, {row["id"]: review for row in plan["rows"]})
    assert [row["exclusion"] for row in summary["rows"]] == ["CONTROL"] * len(plan["rows"])
    for arm in summary["arms"].values():
        assert arm["row_exclusions"] == {"CONTROL": 1}
        assert arm["native_program_gate_wrong_option_passed"]["denominator"] == 0
        assert arm["native_program_gate_correct_option_blocked"]["denominator"] == 0
