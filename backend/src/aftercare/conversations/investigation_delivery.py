"""Start one automatic P4 run for the latest, published P2 turn."""

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.models import Case
from aftercare.business.rules import digest, utc
from aftercare.conversation_analysis.service import _scope
from aftercare.conversation_analysis.tables import ChatAnalysisTurn
from aftercare.db import User
from aftercare.decisions.contracts import DecisionRef, DecisionScope
from aftercare.decisions.service import _same_dependencies
from aftercare.decisions.tables import DecisionHead, DecisionSnapshot
from aftercare.intelligence.service import RunError
from aftercare.intelligence.tables import RunMutation

from .decisions import ConversationDecisions
from .tables import Conversation, InvestigationRequest

PROFILE = "chat-investigation-v1"
FINANCIAL_STATES = ("RESOLVED", "APPROVED", "EXECUTION_PENDING", "EXECUTION_FAILED")


def enabled(settings):
    return all(
        getattr(settings, flag, False)
        for flag in (
            "chat_investigation_enabled",
            "chat_decisions_enabled",
            "chat_analysis_enabled",
        )
    )


def stage_material_review(business, db, conversation, case):
    """Queue a new investigation revision in the current material-review transaction."""
    from .analysis import MATERIAL_REVIEW_CHANGED, scheduler
    from .memory import correction, pending_scope

    if (
        conversation.status != "OPEN"
        or conversation.require_new_case
        or not conversation.analysis_from_seq
        or conversation.analysis_from_seq > conversation.sequence
        or (
            conversation.id,
            conversation.analysis_issue_id,
            conversation.active_case_id,
            conversation.active_order_id,
            conversation.customer_id,
        )
        != (
            case.decision_conversation_id,
            case.decision_issue_id,
            case.id,
            case.order_id,
            case.customer_id,
        )
    ):
        return

    if correction(db, conversation) or pending_scope(db, conversation):
        return

    analysis = scheduler(business.engine, business.settings, clock=business.clock)
    latest = analysis._latest(db, conversation.id)
    stamp = business.clock()
    if (
        latest is not None
        and latest.state in {"PENDING", "RUNNING"}
        and latest.revision == conversation.revision
        and latest.scope_sha256 == _scope(conversation)
        and utc(latest.deadline) > utc(stamp)
        and (
            latest.state == "PENDING" or latest.lease_until and utc(latest.lease_until) > utc(stamp)
        )
    ):
        # Its normal completion will freeze the just-committed review. Preserve
        # pending customer input and any already-started visual attempt.
        return
    analysis.invalidate(db, conversation)
    conversation.revision += 1
    conversation.updated_at = stamp
    turn = analysis.stage(db, conversation, input_from_seq=conversation.analysis_from_seq)
    if turn is not None:
        turn.reason = MATERIAL_REVIEW_CHANGED


def _run_key(turn_id):
    return digest(["chat-investigation-turn", turn_id])


def _latest(turn):
    return (
        ~select(ChatAnalysisTurn.id)
        .where(
            ChatAnalysisTurn.conversation_id == turn.conversation_id,
            ChatAnalysisTurn.generation > turn.generation,
        )
        .correlate(turn)
        .exists()
    )


def pending(business, *, limit=50):
    """Recover both unregistered current P2 turns and committed start intents."""
    if not enabled(business.settings):
        return []
    turn = aliased(ChatAnalysisTurn)
    request = InvestigationRequest
    with Session(business.engine) as db:
        ready = list(
            db.scalars(
                select(request.turn_id)
                .where(request.state == "PENDING")
                .order_by(request.created_at, request.turn_id)
                .limit(limit)
            )
        )
        if len(ready) == limit:
            return ready
        new = db.scalars(
            select(turn.id)
            .join(Conversation, Conversation.id == turn.conversation_id)
            .join(Case, Case.id == Conversation.active_case_id)
            .outerjoin(request, request.turn_id == turn.id)
            .where(
                request.turn_id.is_(None),
                Case.status.notin_(FINANCIAL_STATES),
                turn.state == "PUBLISHED",
                turn.quiet_until <= business.clock(),
                Conversation.status == "OPEN",
                Conversation.revision == turn.revision,
                Conversation.require_new_case.is_(False),
                Conversation.analysis_issue_id == Case.decision_issue_id,
                Conversation.id == Case.decision_conversation_id,
                _latest(turn),
            )
            .order_by(turn.created_at, turn.id)
            .limit(limit - len(ready))
        )
        return [*ready, *new]


def _current(db, row, turn):
    latest = db.scalar(
        select(ChatAnalysisTurn.id)
        .where(ChatAnalysisTurn.conversation_id == row.id)
        .order_by(ChatAnalysisTurn.generation.desc())
        .limit(1)
    )
    return (
        row.status == "OPEN"
        and not row.require_new_case
        and row.active_case_id is not None
        and row.analysis_issue_id is not None
        and row.customer_id == turn.customer_id
        and row.revision == turn.revision
        and latest == turn.id
        and turn.state == "PUBLISHED"
        and _scope(row) == turn.scope_sha256
    )


def _complete_existing(db, request):
    mutation = db.get(RunMutation, (request.case_id, request.actor_id, _run_key(request.turn_id)))
    if mutation is None:
        return False
    request.run_id, request.state, request.reason = mutation.run_id, "STARTED", None
    return True


def _covered(snapshot, scope, stamp):
    if snapshot is None or utc(snapshot.expires_at) <= utc(stamp):
        return False
    if not _same_dependencies(snapshot.input, scope.model_dump(mode="json")):
        return False
    return any(
        check["status"] == "PASSED"
        and not snapshot.candidate.get("unresolved")
        or check["status"] == "NON_EXECUTING"
        and check.get("action") in {"REQUEST_EVIDENCE", "NOT_SUPPORTED"}
        for check in snapshot.checks.values()
    )


def _stage(business, turn_id):
    decisions = ConversationDecisions(business)
    with Session(business.engine) as db, db.begin():
        ident = db.scalar(
            select(ChatAnalysisTurn.conversation_id).where(ChatAnalysisTurn.id == turn_id)
        )
        if ident is None:
            return None
        row = db.get_one(Conversation, ident, with_for_update=True)
        turn = db.get_one(ChatAnalysisTurn, turn_id, with_for_update=True)
        request = db.get(InvestigationRequest, turn_id, with_for_update=True)
        if request is not None and (request.state != "PENDING" or _complete_existing(db, request)):
            return None
        case = db.get(Case, row.active_case_id) if row.active_case_id else None
        closed = case is None or case.status in FINANCIAL_STATES
        if closed or not _current(db, row, turn) or utc(turn.quiet_until) > utc(business.clock()):
            if request is not None:
                request.state, request.reason = "CANCELED", "INVESTIGATION_TURN_STALE"
            return None
        if request is None:
            request = InvestigationRequest(
                turn_id=turn.id,
                conversation_id=row.id,
                issue_id=row.analysis_issue_id,
                case_id=row.active_case_id,
                actor_id=turn.actor_id,
                state="PENDING",
                created_at=business.clock(),
            )
            db.add(request)
        elif (request.conversation_id, request.issue_id, request.case_id, request.actor_id) != (
            row.id,
            row.analysis_issue_id,
            row.active_case_id,
            turn.actor_id,
        ):
            request.state, request.reason = "CANCELED", "INVESTIGATION_SCOPE_CHANGED"
            return None
        owner = db.get(User, turn.actor_id, with_for_update={"read": True})
        if (
            owner is None
            or not owner.enabled
            or owner.role != "customer"
            or owner.customer_id != row.customer_id
        ):
            request.state, request.reason = "BLOCKED", "INVESTIGATION_OWNER_REVOKED"
            return None
        actor = UserContext(user_id=owner.id, role=owner.role, customer_id=owner.customer_id)
        try:
            current = decisions.resolve(db, actor, row.id, row.analysis_issue_id)
        except BusinessError as error:
            request.state, request.reason = "BLOCKED", error.code
            if error.code == "DECISION_INPUT_TOO_LARGE":
                from .analysis import _handoff
                from .service import ConversationService

                _handoff(
                    ConversationService(business),
                    db,
                    row,
                    {"generation": turn.generation},
                    code="HUMAN_HANDOFF",
                    message="当前材料较多，无法在自动核对范围内完整查看，已转客服。原消息和照片会保留。",
                )
            return None
        head = db.get(DecisionHead, (row.id, row.analysis_issue_id))
        previous = decisions._snapshot_ref(db, head)
        prior_data = previous.model_dump(mode="json") if previous else None
        snapshot = db.get(DecisionSnapshot, previous.id) if previous else None
        if _covered(snapshot, current, business.clock()):
            request.state, request.reason = "SUPERSEDED", "INVESTIGATION_ALREADY_COVERED"
            return None
        if request.scope is None:
            request.scope = current.model_dump(mode="json")
            request.previous = prior_data
        else:
            frozen = DecisionScope.model_validate(request.scope)
            if (
                current.dependency_sha256 != frozen.dependency_sha256
                or current.business_versions["case"] != frozen.business_versions["case"]
                or prior_data != request.previous
            ):
                request.state, request.reason = "CANCELED", "INVESTIGATION_DEPENDENCIES_CHANGED"
                return None
        return actor, request.scope, request.previous


def _settle(business, turn_id, *, run_id=None, reason=None):
    with Session(business.engine) as db, db.begin():
        request = db.get_one(InvestigationRequest, turn_id, with_for_update=True)
        if request.state != "PENDING":
            return
        if _complete_existing(db, request):
            return
        if run_id is not None:
            request.run_id, request.state = run_id, "STARTED"
        else:
            request.state, request.reason = "BLOCKED", reason


def process(business, agent, turn_id):
    if not enabled(business.settings):
        return
    staged = _stage(business, turn_id)
    if staged is None:
        return
    actor, raw_scope, previous = staged
    scope = DecisionScope.model_validate(raw_scope)
    try:
        run = agent.start(
            scope.case_id,
            actor,
            expected_version=scope.business_versions["case"],
            idempotency_key=_run_key(turn_id),
            mode="single",
            strategy="routed",
            message=scope.frozen_context["legacy_agent_input"]["message"],
            decision_scope=scope,
            decision_previous=DecisionRef.model_validate(previous) if previous else None,
            graph_profile=PROFILE,
            investigation_arm="auto",
            decision_turn_id=turn_id,
        )
    except (RunError, BusinessError) as error:
        _settle(business, turn_id, reason=error.code)
    else:
        _settle(business, turn_id, run_id=run["id"])
