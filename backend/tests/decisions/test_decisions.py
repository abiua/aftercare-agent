"""Decision contract tests with isolated transactions and a controlled business validator."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.db import Base
from aftercare.decisions.contracts import (
    Candidate,
    CustomerResponse,
    DecisionRef,
    DecisionScope,
    NonExecutingOption,
    Option,
    Review,
)
from aftercare.decisions.service import DecisionService
from aftercare.decisions.tables import (
    CustomerDecision,
    DecisionHead,
    DecisionSnapshot,
    Offer,
    SupportDecision,
)

STAMP = datetime(2026, 11, 11, tzinfo=UTC)
SUPPORT = UserContext(user_id="staff", role="support")
OWNER = UserContext(user_id="client", role="customer", customer_id="owner")
OUTSIDER = UserContext(user_id="other", role="customer", customer_id="other")
SCOPE = DecisionScope(
    conversation_id="chat",
    issue_id="issue",
    case_id="case",
    order_id="order",
    customer_id="owner",
    line_ids=("line",),
    input_through_seq=3,
    input_sha256="a" * 64,
    input_refs=({"kind": "MESSAGE", "id": "msg", "sha256": "a" * 64},),
    evidence_graph_sha256="b" * 64,
    evidence_ids=("fact",),
    policy_catalogue_sha256="c" * 64,
    dependency_sha256="d" * 64,
    business_versions={"case": 1, "order": 1},
)


def _option(amount=1200, **updates):
    return Option.model_validate(
        {
            "id": "refund",
            "action": "REFUND",
            "lines": [{"line_id": "line", "amount_cents": amount}],
            "evidence_ids": ["fact"],
            "policy_ids": ["POL02"],
            "rationale": "Controlled rationale",
            **updates,
        }
    )


def _candidate(run="run", *, options=None, unresolved=()):
    return Candidate(
        run_id=run,
        graph_version="g1",
        route_version="r1",
        model_binding_sha256="a" * 64,
        options=options
        if options is not None
        else (
            _option(),
            _option(id="replace", action="REPLACE", lines=[{"line_id": "line", "quantity": 1}]),
        ),
        unresolved=unresolved,
    )


@pytest.fixture
def fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'decisions.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            cls.__table__
            for cls in (DecisionHead, DecisionSnapshot, SupportDecision, Offer, CustomerDecision)
        ],
    )
    state = SimpleNamespace(scope=SCOPE, now=STAMP, checked=[])

    def resolve(db, actor, conversation_id, issue_id):
        return state.scope

    def validate(db, scope, option):
        state.checked.append(option.id)
        if option.action == "REFUND" and sum(row.amount_cents for row in option.lines) > 1500:
            raise BusinessError("REFUND_CAP_EXCEEDED", "controlled")
        return {"policy_ids": ["POL02", "POL09"]}

    state.engine = engine
    state.service = DecisionService(resolve, validate, clock=lambda: state.now)
    yield state
    engine.dispose()


def _run(fixture, operation):
    with Session(fixture.engine) as db, db.begin():
        return operation(db)


def _error(fixture, operation, code):
    with pytest.raises(BusinessError) as error:
        _run(fixture, operation)
    assert error.value.code == code


def _record(fixture, candidate=None, **kwargs):
    return _run(
        fixture,
        lambda db: fixture.service.record_candidate(
            db, OWNER, fixture.scope, candidate or _candidate(), **kwargs
        ),
    )


def _review(fixture, ref, review=None, *, key="review", expected=None):
    return _run(
        fixture,
        lambda db: fixture.service.review(
            db,
            SUPPORT,
            "chat",
            "issue",
            ref,
            review or Review(action="ACCEPT", reason="checked", option_id="refund"),
            key=key,
            expected_review_id=expected,
        ),
    )


def _offer_ref(value):
    return DecisionRef.model_validate({key: value[key] for key in ("id", "version", "sha256")})


def test_candidate_review_and_customer_accept_are_independent(fixture):
    ref = _record(fixture)
    assert ref == _record(fixture)
    assert fixture.checked == ["refund", "replace"]
    result = _review(fixture, ref)
    assert result == _review(fixture, ref)
    offer = result["offer"]
    assert set(offer) == {"id", "version", "sha256", "expires_at", "action", "lines", "policy_ids"}
    binding = _offer_ref(offer)
    options = {
        "action": offer["action"],
        "lines": offer["lines"],
        "policy_ids": offer["policy_ids"],
    }
    gate = lambda db: fixture.service.require_accepted_offer(db, fixture.scope, binding, **options)
    _error(fixture, gate, "DECISION_CUSTOMER_CONSENT_REQUIRED")

    def respond(db):
        return fixture.service.respond(
            db,
            OWNER,
            "chat",
            "issue",
            binding,
            CustomerResponse(action="ACCEPT"),
            key="customer",
        )

    accepted = _run(fixture, respond)
    assert accepted == _run(fixture, respond)
    assert _run(fixture, gate)["customer_decision_id"] == accepted["id"]
    projection = _run(fixture, lambda db: fixture.service.current_offer(db, OWNER, "chat", "issue"))
    assert projection["customer_decision"]["action"] == "ACCEPT"
    _error(
        fixture,
        lambda db: fixture.service.current_offer(db, OUTSIDER, "chat", "issue"),
        "DECISION_NOT_FOUND",
    )
    _error(
        fixture,
        lambda db: fixture.service.snapshot(db, OWNER, "chat", "issue", ref.id),
        "DECISION_FORBIDDEN",
    )


def test_edit_preserves_original_and_invalidates_prior_customer_consent(fixture):
    ref = _record(fixture)
    first = _review(fixture, ref)
    old_offer = _offer_ref(first["offer"])
    _run(
        fixture,
        lambda db: fixture.service.respond(
            db,
            OWNER,
            "chat",
            "issue",
            old_offer,
            CustomerResponse(action="ACCEPT"),
            key="yes",
        ),
    )
    with pytest.raises(BusinessError, match="无法继续") as stale:
        _review(fixture, ref, Review(action="REJECT", reason="stale client"), key="stale")
    assert stale.value.code == "DECISION_REVIEW_STALE"
    second = _review(
        fixture,
        ref,
        Review(action="MODIFY", reason="review correction", replacement=_option(1300)),
        key="edit",
        expected=first["id"],
    )
    assert second["snapshot"]["version"] == 2
    original = _run(
        fixture, lambda db: fixture.service.snapshot(db, SUPPORT, "chat", "issue", ref.id)
    )
    assert original["candidate"]["options"][0]["lines"][0]["amount_cents"] == 1200
    assert original["origin"] == "AGENT" and original["actor_id"] == OWNER.user_id
    modified = _run(
        fixture,
        lambda db: fixture.service.snapshot(db, SUPPORT, "chat", "issue", second["snapshot"]["id"]),
    )
    assert modified["origin"] == "SUPPORT_MODIFICATION" and modified["parent_id"] == ref.id
    assert modified["original_run_id"] == original["original_run_id"]
    assert second["offer"]["lines"][0]["amount_cents"] == 1300
    _error(
        fixture,
        lambda db: fixture.service.require_accepted_offer(
            db,
            fixture.scope,
            old_offer,
            action="REFUND",
            lines=first["offer"]["lines"],
            policy_ids=first["offer"]["policy_ids"],
        ),
        "DECISION_OFFER_STALE",
    )
    assert _run(fixture, lambda db: len(db.scalars(select(CustomerDecision)).all())) == 1


def test_expiry_dependency_drift_and_program_rejection(fixture):
    ref = _record(fixture, _candidate(options=(_option(1700),)))
    original = _run(
        fixture, lambda db: fixture.service.snapshot(db, SUPPORT, "chat", "issue", ref.id)
    )
    assert original["checks"]["refund"]["code"] == "REFUND_CAP_EXCEEDED"
    fixture.scope = SCOPE.model_copy(
        update={
            "dependency_sha256": "f" * 64,
            "evidence_ids": ("fact", "new-evidence"),
        }
    )
    result = _review(
        fixture,
        ref,
        Review(
            action="MODIFY",
            reason="new evidence",
            replacement=_option(1300),
            new_evidence_ids=("new-evidence",),
        ),
    )
    assert result["offer"]
    fixture.now += timedelta(days=1)
    _error(
        fixture,
        lambda db: fixture.service.current_offer(db, OWNER, "chat", "issue"),
        "DECISION_STALE",
    )


@pytest.mark.parametrize("action", ["REJECT", "REQUEST_EVIDENCE", "TAKE_OVER"])
def test_non_offer_reviews_append_and_never_publish(fixture, action):
    ref = _record(fixture)
    result = _review(fixture, ref, Review(action=action, reason="manual investigation"))
    assert result["offer"] is None and result["action"] == action
    with pytest.raises(BusinessError) as conflict:
        _review(fixture, ref, Review(action=action, reason="changed request"))
    assert conflict.value.code == "DECISION_IDEMPOTENCY_CONFLICT"


def test_amount_must_be_integer_cents_and_no_candidate_cas_bypass(fixture):
    for invalid in (True, 12.5, "1200", 0):
        with pytest.raises(ValidationError):
            _option(invalid)
    ref = _record(fixture)
    with pytest.raises(BusinessError) as stale:
        _record(fixture, _candidate("later"))
    assert stale.value.code == "DECISION_STALE"
    result = _review(fixture, ref)
    assert result["offer"]
    with pytest.raises(BusinessError) as reviewed:
        _record(fixture, _candidate("later"), expected_previous=ref)
    assert reviewed.value.code == "DECISION_ALREADY_REVIEWED"


@pytest.mark.parametrize("action", ["REQUEST_EVIDENCE", "NOT_SUPPORTED", "HUMAN_HANDOFF"])
def test_structured_non_executing_candidate_retains_scope_and_has_no_offer(fixture, action):
    option = NonExecutingOption(
        id="further-check",
        action=action,
        target_line_ids=("line",),
        missing_evidence=("close-up of alleged damage",) if action == "REQUEST_EVIDENCE" else (),
        rationale="Evidence does not establish an executable remedy",
        alternatives=("Send a written description",),
    )
    ref = _record(fixture, _candidate(options=(option,), unresolved=("current picture unclear",)))
    review = _review(
        fixture, ref, Review(action="ACCEPT", reason="use scoped result", option_id=option.id)
    )
    assert review["offer"] is None and review["program_check"] == {
        "status": "NON_EXECUTING",
        "action": action,
    }
    assert fixture.checked == []
    stored = _run(
        fixture, lambda db: fixture.service.snapshot(db, SUPPORT, "chat", "issue", ref.id)
    )
    assert stored["candidate"]["options"][0]["target_line_ids"] == ["line"]


def test_human_modify_only_resolves_explicit_prior_items(fixture):
    ref = _record(fixture, _candidate(unresolved=("blur", "amount")))
    result = _review(
        fixture,
        ref,
        Review(
            action="MODIFY",
            reason="amount checked, blur remains",
            replacement=_option(1300),
            resolved_items=("amount",),
        ),
    )
    assert result["offer"] is None
    assert result["program_check"]["code"] == "DECISION_UNRESOLVED"
    stored = _run(
        fixture,
        lambda db: fixture.service.snapshot(db, SUPPORT, "chat", "issue", result["snapshot"]["id"]),
    )
    assert stored["candidate"]["unresolved"] == ["blur"]
