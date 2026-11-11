"""Project a persisted legacy graph result into an immutable P3 candidate."""

from pydantic import TypeAdapter
from sqlalchemy import select

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.conversation_analysis.tables import ChatAnalysisTurn
from aftercare.conversations.tables import Conversation
from aftercare.db import User
from aftercare.decisions.contracts import (
    Candidate,
    CandidateOption,
    DecisionRef,
    DecisionScope,
    NonExecutingOption,
    Option,
)
from aftercare.investigation.graph import CHAT_GRAPH_PROFILE

from .tables import AgentRun, ModelAttempt, RunEvent

DECISION_INPUT_EVENT = "decision_input"
DECISION_READY_EVENT = "decision_ready"
_BINDING_VERSION = "ac-p3-agent-input-v1"
_READY_STATES = {"WAITING_INPUT", "NEEDS_REVIEW", "COMPLETED"}
_MATERIAL = {
    "ITEM": "商品照片",
    "PARCEL": "包裹外观照片",
    "WRONG_ITEM": "收到商品的照片",
    "LABEL": "包裹外侧标签照片",
}
_REVIEW_REASONS = {
    "MATERIAL_REVIEW_REQUIRED": "材料需由客服核对。本次观察不足以确认材料已审核。",
    "HUMAN_REVIEW": "现有业务事实需要客服核对，暂不能确定处理动作或金额。",
    "ROLE_CONFLICT": "图运行对事实或条款存在冲突，需由客服核对。",
    "DRAFT_RULE_CONFLICT": "建议未通过现行规则核对，需客服重新判断。",
    "UNRESOLVED_POLICY": "当前条款或版本无法确定，需客服核对。",
    "POLICY_BLOCKED": "本案政策范围或有效版本仍待确认。",
    "REFUND_LOOKUP": "退款执行编号需要澄清，当前不能定位原记录。",
    "INVESTIGATION_ISSUE_CALL_LIMIT": "本事项模型调用已达上限，需要客服继续核对。",
    "INVESTIGATION_ISSUE_COST_LIMIT": "本事项模型费用已达上限，需要客服继续核对。",
    "INVESTIGATION_ISSUE_COST_UNKNOWN": "本事项存在尚未确认的模型费用。",
    "INVESTIGATION_ISSUE_BUDGET_VIOLATION": "本事项有模型请求超出预留费用，需要人工核对。",
    "FACTS_CHANGED_DURING_RUN": "处理期间业务事实发生变化，需重新核对。",
}


def _fail(code, status=409):
    raise BusinessError(code, "当前运行无法形成会话候选。", status)


def make_binding(scope, previous, facts, actor, message, context_sha256):
    if not isinstance(scope, DecisionScope) or (
        previous is not None and not isinstance(previous, DecisionRef)
    ):
        _fail("DECISION_RUN_BINDING_INVALID")
    case = facts["case"]
    if (
        scope.case_id != case["id"]
        or scope.order_id != case["order_id"]
        or scope.customer_id != case["customer_id"]
        or set(scope.line_ids) != set(case["line_ids"])
        or (actor.role == "customer" and actor.customer_id != scope.customer_id)
        or any(
            scope.business_versions.get(key) != value for key, value in facts["versions"].items()
        )
    ):
        _fail("DECISION_RUN_SCOPE_MISMATCH")
    return {
        "version": _BINDING_VERSION,
        "cycle": 0,
        "actor_id": actor.user_id,
        "message_sha256": digest(message),
        "context_sha256": context_sha256,
        "scope": scope.model_dump(mode="json"),
        "expected_previous": previous.model_dump(mode="json") if previous is not None else None,
    }


def load_binding(db, run):
    rows = list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.kind == DECISION_INPUT_EVENT)
            .limit(2)
        )
    )
    if not rows:
        return None
    row = rows[0]
    binding = row.payload
    if (
        len(rows) != 1
        or binding.get("version") != _BINDING_VERSION
        or binding.get("cycle") != run.cycle
        or binding.get("actor_id") != run.owner_id
        or binding.get("message_sha256") != digest(run.message)
    ):
        _fail("DECISION_RUN_BINDING_INVALID")
    return binding


def auto_turn_current(db, scope, actor_id, turn_id):
    latest = (
        select(ChatAnalysisTurn.id)
        .where(ChatAnalysisTurn.conversation_id == scope.conversation_id)
        .order_by(ChatAnalysisTurn.generation.desc())
        .limit(1)
        .correlate(None)
        .scalar_subquery()
    )
    lines = db.scalar(
        select(Conversation.line_ids)
        .join(ChatAnalysisTurn, ChatAnalysisTurn.conversation_id == Conversation.id)
        .where(
            Conversation.id == scope.conversation_id,
            ChatAnalysisTurn.id == turn_id,
            ChatAnalysisTurn.id == latest,
            ChatAnalysisTurn.actor_id == actor_id,
            ChatAnalysisTurn.state == "PUBLISHED",
            ChatAnalysisTurn.revision == Conversation.revision,
            ChatAnalysisTurn.customer_id == scope.customer_id,
            Conversation.status == "OPEN",
            Conversation.require_new_case.is_(False),
            Conversation.customer_id == scope.customer_id,
            Conversation.active_case_id == scope.case_id,
            Conversation.active_order_id == scope.order_id,
            Conversation.analysis_issue_id == scope.issue_id,
        )
    )
    return lines is not None and set(lines) == set(scope.line_ids)


def _options(run, scope):
    out = run.output or {}
    trusted_evidence = tuple(
        item.id
        for item in scope.input_refs
        if item.kind in {"FACT", "POLICY"} and item.id in scope.evidence_ids
    )
    if out.get("graph_profile") == CHAT_GRAPH_PROFILE and out.get("investigation_options"):
        options = TypeAdapter(tuple[CandidateOption, ...]).validate_python(
            out["investigation_options"]
        )
        return options, tuple(out.get("investigation_unresolved", ()))
    if (
        run.status == "COMPLETED"
        and run.reason == "ELIGIBLE"
        and out.get("action") in {"REFUND", "REPLACE"}
        and out.get("lines")
        and out.get("policy_ids")
        and trusted_evidence
    ):
        return (
            Option(
                id="graph-option",
                action=out["action"],
                lines=out["lines"],
                evidence_ids=trusted_evidence,
                policy_ids=out["policy_ids"],
                rationale="订单行和金额或数量来自已通过图内业务规则校验的建议，仍须客服和客户确认。",
            ),
        ), ()
    kinds = out.get("missing_materials") or []
    missing = tuple(dict.fromkeys(_MATERIAL[kind] for kind in kinds if kind in _MATERIAL))
    if missing and (
        (run.status == "WAITING_INPUT" and run.reason == "WAITING_INPUT")
        or (out.get("graph_profile") == CHAT_GRAPH_PROFILE and out.get("status") == "WAITING_INPUT")
    ):
        action, reason = "REQUEST_EVIDENCE", "当前缺少对应订单行的必要材料。"
    elif run.status == "COMPLETED" and run.reason in {"INELIGIBLE", "RULE_INELIGIBLE"}:
        action, reason = "NOT_SUPPORTED", "现行规则未支持对这些订单行提供退款或换发。"
    elif (
        out.get("graph_profile") == CHAT_GRAPH_PROFILE
        and run.reason == "REFUND_LOOKUP"
        and run.status == "COMPLETED"
    ):
        action, reason = "NOT_SUPPORTED", "这是原退款记录查询，无需另行提出退款或换发方案。"
    else:
        action = "HUMAN_HANDOFF"
        reason = _REVIEW_REASONS.get(
            run.reason, "当前证据不足以确定动作、金额或数量，需由客服核对。"
        )
    return (
        NonExecutingOption(
            id="graph-disposition",
            action=action,
            target_line_ids=scope.line_ids,
            evidence_ids=trusted_evidence,
            rationale=reason,
            missing_evidence=missing if action == "REQUEST_EVIDENCE" else (),
            alternatives=("如暂时无法提供，可说明已有信息，由客服核对。",)
            if action == "REQUEST_EVIDENCE"
            else (),
        ),
    ), tuple(
        dict.fromkeys([reason, *out.get("investigation_unresolved", [])])
    ) if action == "HUMAN_HANDOFF" else ()


def project_candidate(db, decisions, actor, run_id):
    """Internal projector. Only a bound, current, completed cycle may become a candidate."""
    run = db.get(AgentRun, run_id)
    if run is None or actor.user_id != run.owner_id or actor.role not in {"support", "customer"}:
        _fail("DECISION_RUN_NOT_FOUND", 404)
    current_user = db.get(User, actor.user_id)
    if (
        current_user is None
        or not current_user.enabled
        or current_user.role != actor.role
        or current_user.customer_id != actor.customer_id
    ):
        _fail("DECISION_RUN_NOT_FOUND", 404)
    binding = load_binding(db, run)
    if binding is None or run.status not in _READY_STATES:
        _fail("DECISION_RUN_NOT_READY")
    scope = DecisionScope.model_validate(binding["scope"])
    if scope.case_id != run.case_id or (
        actor.role == "customer" and scope.customer_id != actor.customer_id
    ):
        _fail("DECISION_RUN_NOT_FOUND", 404)
    # The resolver takes conversation and business locks before taking the run lock.
    # A canceled waiting run cannot race a projection past this locked read.
    decisions.resolve(db, actor, scope.conversation_id, scope.issue_id)
    run = db.get(AgentRun, run_id, populate_existing=True, with_for_update={"read": True})
    if run is None or run.status not in _READY_STATES or load_binding(db, run) != binding:
        _fail("DECISION_RUN_NOT_READY")
    turn_id = binding.get("decision_turn_id")
    if turn_id and not auto_turn_current(db, scope, actor.user_id, turn_id):
        _fail("INVESTIGATION_INPUT_STALE")
    observed = list(
        db.scalars(
            select(RunEvent)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.kind == "context_binding",
                RunEvent.payload["cycle"].as_integer() == run.cycle,
            )
            .limit(2)
        )
    )
    if (
        len(observed) != 1
        or observed[0].payload.get("digest") != binding["context_sha256"]
        or run.reason
        in {"FACTS_CHANGED_DURING_RUN", "RECOVERY_CONTEXT_CHANGED", "RUN_ACTOR_REVOKED"}
    ):
        _fail("DECISION_RUN_CONTEXT_CHANGED")
    ready = list(
        db.scalars(
            select(RunEvent)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.kind == DECISION_READY_EVENT,
            )
            .limit(2)
        )
    )
    if len(ready) != 1 or ready[0].payload != {
        "cycle": run.cycle,
        "context_sha256": binding["context_sha256"],
        "output_sha256": digest([run.status, run.reason, run.output]),
    }:
        _fail("DECISION_RUN_NOT_READY")
    attempts = list(
        db.scalars(
            select(ModelAttempt)
            .where(ModelAttempt.run_id == run.id, ModelAttempt.cycle == run.cycle)
            .order_by(ModelAttempt.logical_id, ModelAttempt.ordinal)
        )
    )
    options, unresolved = _options(run, scope)
    candidate = Candidate(
        run_id=run.id,
        graph_version=binding.get("graph_profile") or f"existing-{run.mode}-v1",
        route_version=(
            binding["investigation_arm"]
            if attempts and binding.get("graph_profile")
            else f"existing-{run.strategy}"
            if attempts
            else "rules-only"
        ),
        model_binding_sha256=digest(
            [
                {
                    "logical_id": row.logical_id,
                    "ordinal": row.ordinal,
                    "status": row.status,
                    "registry_sha256": row.metadata_json.get("base_registry_digest"),
                    "transport": row.metadata_json.get("transport_evidence"),
                    "model": row.metadata_json.get("model"),
                }
                for row in attempts
            ]
        ),
        options=options,
        unresolved=unresolved,
    )
    previous = binding["expected_previous"]
    return decisions.record_candidate(
        db,
        actor,
        scope,
        candidate,
        expected_previous=DecisionRef.model_validate(previous) if previous is not None else None,
    )
