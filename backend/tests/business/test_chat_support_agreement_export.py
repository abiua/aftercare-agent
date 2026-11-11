from dataclasses import replace
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import SUPPORT
from test_chat_decisions_integration import _link, _ref, client, configured, system  # noqa: F401
from test_conversations import headers

from aftercare.conversations.decision_delivery import project
from aftercare.decisions.contracts import DecisionRef
from aftercare.decisions.tables import SupportDecision
from aftercare.evaluation.chat.scoring import PlannedRow
from aftercare.evaluation.chat.support_agreement_export import (
    ExportTarget,
    export_support_agreement,
)


def test_export_reads_native_conversation_evidence_request_and_respects_cutoff(configured):  # noqa: F811
    env = configured
    url, _ = _link(env)
    run = env.client.post(url + "/decisions/run", headers=headers(SUPPORT), json={}).json()
    env.agent.process(run["run_id"])
    project(env.service, run["run_id"])
    snapshot = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"]
    option = snapshot["candidate"]["options"][0]
    assert option["action"] == "REQUEST_EVIDENCE"
    reply = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(snapshot),
            "expected_review_id": None,
            "review": {"action": "REQUEST_EVIDENCE", "reason": "private test text"},
            "evidence_gaps": ["ISSUE_DESCRIPTION"],
        },
    )
    assert reply.status_code == 200
    review_id = reply.json()["id"]
    with Session(env.db) as db:
        native = db.get_one(SupportDecision, review_id)
        assert native.review["evidence_gaps"]
        cutoff = native.created_at.replace(tzinfo=UTC)

    target = ExportTarget(
        PlannedRow("case", 1, "multi"),
        DecisionRef.model_validate(_ref(snapshot)),
        option["id"],
        cutoff,
    )
    early = replace(
        target, row=PlannedRow("case", 2, "multi"), cutoff=cutoff - timedelta(microseconds=1)
    )
    report = export_support_agreement(env.db, (target, early))
    first, missing = report["rows"]
    assert first.review_action == "REQUEST_EVIDENCE"
    assert first.action.match is True
    assert first.information == "UNKNOWN"
    assert missing.action.match is None
    assert report["extraction"][0]["review_id"] == review_id
    assert report["extraction"][1]["exclusion"] in {
        "NO_SUPPORT_REVIEW_BY_CUTOFF",
        "NO_CANDIDATE_BY_CUTOFF",
    }
    assert report["support_evidence_agreement"]["status"] == "NOT_MEASURABLE"
    assert "private test text" not in repr(report)
    later = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(snapshot),
            "expected_review_id": review_id,
            "review": {"action": "REQUEST_EVIDENCE", "reason": "later review"},
            "evidence_gaps": ["ISSUE_DESCRIPTION"],
        },
    )
    assert later.status_code == 200
    later_id = later.json()["id"]
    with Session(env.db) as db:
        assert set(db.scalars(select(SupportDecision.id))) == {review_id, later_id}
        later_cutoff = db.get_one(SupportDecision, later_id).created_at.replace(tzinfo=UTC)
    expanded = replace(target, cutoff=later_cutoff)
    report = export_support_agreement(env.db, (expanded,))
    assert report["extraction"][0]["review_id"] == review_id

    with Session(env.db) as db, db.begin():
        db.get_one(SupportDecision, review_id).request_sha256 = "a" * 64
    invalid = export_support_agreement(env.db, (expanded,))
    assert invalid["rows"][0].action.match is None
    assert invalid["extraction"][0]["exclusion"] == "SUPPORT_RECORD_BINDING_UNVERIFIED"
    assert invalid["rows"][0].action.exclusion == "SUPPORT_RECORD_BINDING_UNVERIFIED"
    assert invalid["groups"][0]["action_agreement"]["exclusions"] == {
        "SUPPORT_RECORD_BINDING_UNVERIFIED": 1,
    }

    with Session(env.db) as db, db.begin():
        db.get_one(SupportDecision, later_id).created_at = cutoff
    ambiguous = export_support_agreement(env.db, (expanded,))
    assert ambiguous["rows"][0].action.match is None
    assert ambiguous["extraction"][0]["exclusion"] == "FIRST_SUPPORT_REVIEW_AMBIGUOUS"
    assert ambiguous["rows"][0].action.exclusion == "FIRST_SUPPORT_REVIEW_AMBIGUOUS"
    assert ambiguous["groups"][0]["action_agreement"]["exclusions"] == {
        "FIRST_SUPPORT_REVIEW_AMBIGUOUS": 1,
    }
