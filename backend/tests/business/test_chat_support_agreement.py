from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.rules import digest
from aftercare.db import Base
from aftercare.decisions.contracts import Candidate, DecisionRef, DecisionScope, Review
from aftercare.decisions.service import DecisionService
from aftercare.decisions.tables import DecisionHead, DecisionSnapshot, Offer, SupportDecision
from aftercare.evaluation.chat.scoring import PlannedRow
from aftercare.evaluation.chat.support_agreement import (
    AgreementInput,
    FrozenSnapshot,
    FrozenSupportReview,
    score_agreement,
    summarize_support_agreement,
)

STAMP = datetime(2026, 11, 11, tzinfo=UTC)
SHA = "a" * 64


def _option(ident, lines):
    return {
        "id": ident,
        "action": "REFUND",
        "lines": [{"line_id": line, "amount_cents": cents} for line, cents in lines],
        "evidence_ids": ["fact"],
        "policy_ids": ["policy"],
        "rationale": "Fixture",
    }


def _snapshot(ident, options, *, parent=None, evidence_sha=SHA, context_changed=False):
    refs = [{"kind": "FACT", "id": "fact", "sha256": evidence_sha}]
    context = {"fixture": not context_changed}
    scope = DecisionScope(
        conversation_id="conv",
        issue_id="issue",
        case_id="case",
        order_id="order",
        customer_id="customer",
        line_ids=("L01", "L02"),
        input_through_seq=1,
        input_sha256=digest([refs, context]),
        input_refs=refs,
        evidence_graph_sha256=SHA,
        evidence_ids=("fact",),
        policy_catalogue_sha256=SHA,
        dependency_sha256=SHA,
        business_versions={"materials": 1},
        frozen_context=context,
    )
    candidate = Candidate(
        run_id="run",
        graph_version="graph",
        route_version="route",
        model_binding_sha256=SHA,
        options=options,
    )
    signed = {
        "id": ident,
        "version": 2 if parent else 1,
        "parent_id": parent,
        "run_id": None if parent else "run",
        "origin": "SUPPORT_MODIFICATION" if parent else "AGENT",
        "actor_id": "support",
        "input": scope.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "checks": {o.id: {"status": "PASSED"} for o in candidate.options},
        "expires_at": STAMP.isoformat(),
    }
    ref = DecisionRef(id=ident, version=signed["version"], sha256=digest(signed))
    return FrozenSnapshot(
        ref=ref,
        conversation_id="conv",
        issue_id="issue",
        parent_id=parent,
        run_id=signed["run_id"],
        origin=signed["origin"],
        actor_id="support",
        input=signed["input"],
        candidate=signed["candidate"],
        checks=signed["checks"],
        expires_at=STAMP,
    )


def _item(
    disposition, *, replacement=None, option_id=None, evidence_sha=SHA, context_changed=False
):
    initial = _snapshot(
        "original",
        [
            _option("fixed", [("L01", 100), ("L02", 200)]),
            _option("alternative", [("L01", 200), ("L02", 100)]),
        ],
    )
    result = (
        _snapshot(
            "modified",
            [replacement],
            parent="original",
            evidence_sha=evidence_sha,
            context_changed=context_changed,
        )
        if replacement
        else initial
    )
    review = Review(
        action=disposition, reason="Fixture", replacement=replacement, option_id=option_id
    )
    payload = review.model_dump(mode="json")
    record = FrozenSupportReview(
        id="review",
        conversation_id="conv",
        issue_id="issue",
        snapshot_id="original",
        resulting_snapshot_id=result.ref.id,
        actor_id="support",
        request_sha256=digest([initial.ref.model_dump(mode="json"), payload, None]),
        review={**payload, "program_check": None},
        expected_previous_review_id=None,
    )
    return AgreementInput(
        PlannedRow("fixture", 1, "multi"),
        "fixed",
        initial.ref,
        record.id,
        result.ref,
        initial,
        record,
        result,
    )


def test_accept_cannot_select_the_comparison_option_and_information_is_unknown():
    result = score_agreement(_item("ACCEPT", option_id="alternative"))
    assert result.review_action == "ACCEPT"
    assert result.information == "UNKNOWN"
    assert result.action.match is True
    # Same aggregate cents, different per-line cents.
    assert result.refund_amount_by_line.match is False


def test_modification_checks_information_and_exact_per_line_scope():
    same = score_agreement(
        _item("MODIFY", replacement=_option("new", [("L02", 200), ("L01", 100)]))
    )
    assert same.information == "SAME_INFORMATION"
    assert same.refund_amount_by_line.match is True
    changed = score_agreement(
        _item("MODIFY", replacement=_option("new", [("L02", 200)]), evidence_sha="b" * 64)
    )
    assert changed.information == "CHANGED_INFORMATION"
    assert changed.action.match is True
    assert changed.refund_amount_by_line.exclusion == "DIFFERENT_REFUND_LINE_SCOPE"


def test_context_only_change_cannot_be_called_same_information():
    item = _item("MODIFY", replacement=_option("new", [("L01", 100)]), context_changed=True)
    assert score_agreement(item).information == "UNKNOWN"


def test_direct_evidence_action_measurable_but_evidence_contents_unavailable():
    item = _item("REQUEST_EVIDENCE")
    absent = replace(item, row=PlannedRow("absent", 1, "multi"), review=None)
    report = summarize_support_agreement((absent, item), planned_rows=(item.row, absent.row))
    first, second = report["rows"]
    assert first.action.match is False
    assert first.refund_amount_by_line.exclusion == "NOT_BOTH_REFUNDS"
    assert second.action.exclusion == "NO_SUPPORT_REVIEW"
    assert report["support_evidence_agreement"]["status"] == "NOT_MEASURABLE"
    assert len(report["groups"]) == 2


def test_tampered_review_or_snapshot_never_contributes():
    item = _item("ACCEPT", option_id="fixed")
    assert (
        score_agreement(
            replace(item, review=replace(item.review, snapshot_id="foreign"))
        ).action.exclusion
        == "REVIEW_BINDING_MISMATCH"
    )
    assert (
        score_agreement(replace(item, review=replace(item.review, request_sha256=SHA))).action.match
        is None
    )
    tampered = replace(item.snapshot, checks={})
    assert (
        score_agreement(replace(item, snapshot=tampered)).action.exclusion
        == "INVALID_FROZEN_EVIDENCE"
    )


def test_native_decision_service_modification_binds_snapshot_hash_and_previous_review():
    scope = DecisionScope.model_validate(
        _snapshot("fixture", [_option("fixed", [("L01", 100)])]).input
    )
    actor = UserContext(user_id="support", role="support")
    service = DecisionService(
        lambda db, who, conversation, issue: scope,
        lambda db, current, option: {"policy_ids": ["policy"]},
        clock=lambda: STAMP,
    )
    candidate = Candidate(
        run_id="native-run",
        graph_version="graph",
        route_version="route",
        model_binding_sha256=SHA,
        options=[_option("fixed", [("L01", 100)])],
    )
    modification = Review(
        action="MODIFY", reason="Fixture", replacement=_option("revised", [("L01", 200)])
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            table.__table__ for table in (DecisionHead, DecisionSnapshot, SupportDecision, Offer)
        ],
    )

    def export_snapshot(db, ref):
        row = db.get_one(DecisionSnapshot, ref.id)
        return FrozenSnapshot(
            ref=DecisionRef(id=row.id, version=row.version, sha256=row.sha256),
            conversation_id=row.conversation_id,
            issue_id=row.issue_id,
            parent_id=row.parent_id,
            run_id=row.run_id,
            origin=row.origin,
            actor_id=row.actor_id,
            input=row.input,
            candidate=row.candidate,
            checks=row.checks,
            expires_at=row.expires_at.replace(tzinfo=UTC),
        )

    try:
        with Session(engine) as db, db.begin():
            original = service.record_candidate(db, actor, scope, candidate)
            receipt = service.review(
                db,
                actor,
                scope.conversation_id,
                scope.issue_id,
                original,
                modification,
                key="native-review",
                expected_review_id=None,
            )
        with Session(engine) as db:
            row = db.get_one(SupportDecision, receipt["id"])
            support = FrozenSupportReview(
                id=row.id,
                conversation_id=row.conversation_id,
                issue_id=row.issue_id,
                snapshot_id=row.snapshot_id,
                resulting_snapshot_id=row.resulting_snapshot_id,
                actor_id=row.actor_id,
                request_sha256=row.request_sha256,
                review=row.review,
                expected_previous_review_id=None,
            )
            result = DecisionRef.model_validate(receipt["snapshot"])
            item = AgreementInput(
                PlannedRow("fixture", 1, "multi"),
                "fixed",
                original,
                row.id,
                result,
                export_snapshot(db, original),
                support,
                export_snapshot(db, result),
            )
    finally:
        engine.dispose()

    score = score_agreement(item)
    assert score.information == "SAME_INFORMATION"
    assert score.action.match is True
    assert score.refund_amount_by_line.match is False
    wrong_previous = replace(support, expected_previous_review_id="wrong")
    assert (
        score_agreement(replace(item, review=wrong_previous)).action.exclusion
        == "REVIEW_BINDING_MISMATCH"
    )
    report = summarize_support_agreement((item,), planned_rows=(item.row,))
    changed_binding = replace(item, expected_result=original)
    changed_report = summarize_support_agreement((changed_binding,), planned_rows=(item.row,))
    assert report["plan_sha256"] != changed_report["plan_sha256"]
