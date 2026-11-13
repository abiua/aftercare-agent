"""The checkpointed chat investigation graph."""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.decisions.contracts import CandidateOption, NonExecutingOption, Option
from aftercare.agent_kernel.contracts import VERSION as KERNEL_VERSION
from aftercare.intelligence.models import InvalidOutput, ModelFailure

from .catalogue import exact_policy_refs_match
from .kernel_adapter import KERNEL_ARM, REVISION_PHASE, review_outcome, suggestion
from .orchestration import (
    RECHECK_REQUEST,
    InvestigationContext,
    ProtectedAudit,
    run_phase,
    validate_recheck,
)
from .prompt_evidence import PRIVATE_RECEIPT_EVENT
from .tools import MAX_READ_IDS

CHAT_GRAPH_PROFILE = "chat-investigation-v1"
NO_RECHECK_ARM = "multi_no_recheck"
SEQUENTIAL_CLAIMS_ARM = "multi_sequential_claims"
SYNTHESIS_ONLY_ARM = "multi_synthesis_only"
ARMS = {
    "auto",
    "strong_single",
    "single_staged",
    "multi",
    NO_RECHECK_ARM,
    SEQUENTIAL_CLAIMS_ARM,
    SYNTHESIS_ONLY_ARM,
    KERNEL_ARM,
}
_OPTIONS = TypeAdapter(list[CandidateOption])
_MATERIAL_INTENTS = {"damaged", "wrong_item", "return", "warranty"}


class InvestigationState(TypedDict, total=False):
    cycle: int
    path: str
    status: str
    reason: str
    output: dict
    single_result: dict
    fact_result: dict
    policy_result: dict
    assessment: dict
    evidence_ids: list[str]
    active_claims: list[dict]
    stale_claims: list[str]
    retired_claims: list[dict]
    recheck: dict
    recheck_count: int
    recheck_receipt: dict
    sequential_claims: bool


def _output(context, *, reason, options=(), unresolved=(), status="NEEDS_REVIEW", extra=None):
    first = (
        next((option for option in options if isinstance(option, Option)), None)
        if reason == "ELIGIBLE"
        else None
    )
    return {
        "status": status,
        "reason": reason,
        "output": {
            **(extra or {}),
            "graph_profile": CHAT_GRAPH_PROFILE,
            "action": first.action if first else None,
            "lines": first.business_lines() if first else [],
            "policy_ids": list(first.policy_ids) if first else [],
            "investigation_options": [option.model_dump(mode="json") for option in options],
            "investigation_unresolved": list(unresolved),
            "reason": reason,
        },
    }


def _handoff(context, reason):
    return _output(context, reason=reason, unresolved=(reason,))


def _route(_state, runtime: Runtime[InvestigationContext]):
    context = runtime.context
    route = context.route
    selected = route.path
    # The kernel arm keeps the program route: ordinary cases get executor plus independent
    # review, complex cases go to the existing fact/policy/verify deep investigation.
    if selected != "PROGRAM" and context.arm not in {"auto", KERNEL_ARM}:
        selected = {
            "strong_single": "SINGLE",
            "single_staged": "STAGED",
            "multi": "MULTI",
            NO_RECHECK_ARM: "MULTI",
            SEQUENTIAL_CLAIMS_ARM: "MULTI",
            SYNTHESIS_ONLY_ARM: "MULTI",
        }[context.arm]
    context.audit.event(
        "investigation_route",
        {
            **route.model_dump(mode="json"),
            "selected_path": selected,
            "arm": context.arm,
            **({"counterexample_ablation": True} if context.counterexample_ablation else {}),
            **(
                {
                    "prompt_version": context.protocol.prompt_version,
                    "evidence_projection": context.protocol.evidence_projection,
                }
                if context.protocol.evidence_projection is not None
                else {}
            ),
        },
    )
    if selected == "PROGRAM":
        status = route.output.get("status", "NEEDS_REVIEW")
        return {
            "path": selected,
            **_output(
                context,
                reason=route.reason,
                status="NEEDS_REVIEW" if status == "WAITING_INPUT" else status,
                extra=route.output,
            ),
        }
    return {
        "path": selected,
        "recheck_count": 0,
        **({"sequential_claims": True} if context.arm == SEQUENTIAL_CLAIMS_ARM else {}),
    }


def _phase(
    context,
    role,
    phase_id,
    *,
    upstream=None,
    known=(),
    max_rounds=2,
    max_tools=3,
    prompt=None,
    tools_enabled=True,
    assessment_ref_ids=(),
    initial_material_ids=(),
):
    prompt = context.input.role_view(role) if prompt is None else prompt
    result, evidence, receipts, tool_calls = run_phase(
        context,
        role,
        phase_id,
        prompt=prompt,
        upstream=upstream,
        known_evidence=known,
        max_rounds=max_rounds,
        tool_limit=max_tools,
        tools_enabled=tools_enabled,
        assessment_ref_ids=assessment_ref_ids,
        initial_material_ids=initial_material_ids,
        audit=ProtectedAudit(context.audit, context.verifier_reserve)
        if role in {"fact", "policy"} and phase_id != "recheck"
        else None,
    )
    if role not in {"verifier", "synthesizer"}:
        context.audit.event(
            "investigation_claims",
            {
                "role": role,
                "phase_id": phase_id,
                "claims": _claims(role, result),
            },
        )
    return {**result, "evidence_ids": evidence, "receipts": receipts, "tool_calls": tool_calls}


def _strong(_state, runtime: Runtime[InvestigationContext]):
    context = runtime.context
    try:
        result = _phase(
            context,
            "strong_single",
            "single",
            max_rounds=8,
            max_tools=8,
            prompt=_fact_prompt(context, "strong_single"),
        )
    except ModelFailure as error:
        return {"path": "DONE", **_handoff(context, error.code)}
    if (
        result.get("escalate")
        and context.arm in {"auto", KERNEL_ARM}
        and _conflict(result["claims"])
        and result["evidence_ids"]
        and result["tool_calls"] <= 2
    ):
        context.audit.event(
            "investigation_escalation",
            {
                "from": "SINGLE",
                "to": "MULTI",
                "reason": result["escalate"],
            },
        )
        return {"path": "MULTI", "single_result": result}
    if context.arm == KERNEL_ARM:
        return {"path": "REVIEW", "single_result": result}
    return {"path": "DONE", **_single_outcome(context, result)}


def _single_outcome(context, result, *, emit=True):
    unread = _unread_material(context, result["evidence_ids"])
    if unread and _asks_for_material(result):
        return _material_handoff(context, unread, emit=emit)
    return _publish_options(context, result, emit=emit)


def _kernel_review(state, runtime: Runtime[InvestigationContext]):
    """Independent review at commit and at give-up; one revision at most, then program checks."""
    context = runtime.context
    result = state["single_result"]
    review = review_outcome(context, result, _single_outcome(context, result, emit=False))
    revised = False
    if review.verdict == "revise":
        try:
            result = _phase(
                context,
                "strong_single",
                REVISION_PHASE,
                max_rounds=8,
                max_tools=8,
                prompt={**_fact_prompt(context, "strong_single"), "review_suggestion": suggestion(review)},
            )
            revised = True
        except ModelFailure as error:
            # Keep the first, already checked outcome; record why the revision did not happen.
            context.audit.event("kernel_revision_failed", {"reason": error.code})
    final = _single_outcome(context, result)
    output = final["output"]
    output["kernel"] = {
        "version": KERNEL_VERSION,
        "review": review.model_dump(mode="json"),
        "revised": revised,
    }
    if review.verdict == "revise" and final["reason"] != "ELIGIBLE":
        # Support sees what the independent reviewer thought could still be done.
        output["investigation_unresolved"] = [
            *output["investigation_unresolved"],
            ("独立审查建议：" + review.next_step)[:500],
        ]
    return {"path": "DONE", **final}


def _role(role, *, staged=False, sequential_claims=False):
    def execute(state, runtime: Runtime[InvestigationContext]):
        context = runtime.context
        upstream = (
            _claims("fact", state.get("fact_result", {}))
            if (staged or sequential_claims) and role == "policy"
            else None
        )
        try:
            result = _phase(
                context,
                role,
                f"{'staged-' if staged else ''}{role}",
                upstream=upstream,
                known=state.get("fact_result", {}).get("evidence_ids", ())
                if staged and upstream
                else (),
                max_tools=2,
                initial_material_ids=(
                    sorted(
                        ident
                        for ident, ref in _material_catalogue(context).items()
                        if ref["source_kind"] == "MATERIAL"
                    )[:MAX_READ_IDS]
                    if role == "fact"
                    else ()
                ),
            )
        except ModelFailure as error:
            result = {"error": error.code}
        return {f"{role}_result": result}

    return execute


def _claims(role, result):
    return [
        {
            **claim,
            "role": role,
            "id": f"{role}/{claim['id']}",
            "depends_on_claims": [
                item if "/" in item else f"{role}/{item}" for item in claim["depends_on_claims"]
            ],
        }
        for claim in result.get("claims", [])
    ]


def _material_catalogue(context):
    if context.input.intent not in _MATERIAL_INTENTS:
        return {}
    return {
        row.ref.source_id: row.ref.model_dump(mode="json")
        for row in context.input.evidence
        if row.ref.source_kind in {"MATERIAL", "REVIEW", "ATTACHMENT", "OBSERVATION"}
    }


def _fact_prompt(context, role):
    view = context.input.role_view(role)
    if _material_catalogue(context):
        view["material_read_instruction"] = (
            "已提供材料目录。用 read_material_evidence 读取相关记录和有效人工审核。"
            "与 read_order_lines 独立，可在首轮并列发出。区分已上传、已读取和审核通过。"
            "未读材料不能认定缺失。"
        )
    return view


def _unread_material(context, evidence):
    return {
        ident: ref
        for ident, ref in _material_catalogue(context).items()
        if ident not in evidence and ref["source_kind"] in {"MATERIAL", "REVIEW"}
    }


def _material_handoff(context, unread, *, emit=True):
    reason = "INVESTIGATION_MATERIAL_NOT_READ"
    if emit:
        context.audit.event(
            "investigation_material_unread", {"evidence_ids": sorted(unread), "reason": reason}
        )
    return _output(context, reason=reason, unresolved=("已有冻结材料或审核未核对，交人工处理。",))


def _asks_for_material(result):
    return any(option["action"] == "REQUEST_EVIDENCE" for option in result["options"])


def _evidence(state):
    return sorted(
        {
            ref
            for key in ("fact_result", "policy_result")
            for ref in state.get(key, {}).get("evidence_ids", [])
        }
    )


def _all_claims(state):
    return _claims("fact", state["fact_result"]) + _claims("policy", state["policy_result"])


def _verify(state, runtime: Runtime[InvestigationContext]):
    context = runtime.context
    errors = [
        state[key]["error"] for key in ("fact_result", "policy_result") if "error" in state[key]
    ]
    if errors:
        return {"path": "DONE", **_handoff(context, errors[0])}
    claims = state.get("active_claims", _all_claims(state))
    evidence = state.get("evidence_ids", _evidence(state))
    if not claims or not evidence:
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_EVIDENCE_MISSING")}
    count = state.get("recheck_count", 0)
    raw_receipts = [
        receipt
        for key in ("fact_result", "policy_result")
        for receipt in state[key]["receipts"]
        if any(ref["source_id"] in evidence for ref in receipt["refs"])
    ]
    if count:
        raw_receipts.append(state["recheck_receipt"])
    unread = _unread_material(context, set(evidence) | set(_evidence(state)))
    prompt = {
        **context.input.role_view("verifier"),
        "original_read_receipts": raw_receipts,
        "original_frozen_evidence": [
            row.model_dump(mode="json")
            for row in context.input.evidence
            if row.ref.source_id in evidence
        ],
        "unresolved_invalidated_claim_ids": state.get("stale_claims", []),
    }
    if unread:
        prompt.update(
            unread_material_refs=list(unread.values()),
            material_verification_instruction=(
                "目录只证明已存在相应记录。检查上游是否把未读记录说成缺失。"
                "如有实际冲突，针对该主张用 read_material_evidence 补查。"
                "已有上传材料不能要求重复提交，未核实则保留缺口并交人工处理。"
            ),
        )
    statements = [
        row.model_dump(mode="json")
        for row in context.input.messages
        if row.ref.source_id in evidence
    ]
    if statements:
        prompt["customer_statements"] = statements
    synthesis = context.counterexample_ablation and context.arm == SYNTHESIS_ONLY_ARM
    proposal_tool = not count and (synthesis or not context.direct_recheck_request)
    try:
        result = _phase(
            context,
            "synthesizer" if synthesis else "verifier",
            "synthesize-0" if synthesis else f"verify-{count}",
            upstream=claims,
            known=evidence,
            max_rounds=2 if proposal_tool else 1,
            max_tools=1 if proposal_tool else 0,
            tools_enabled=proposal_tool,
            prompt=prompt,
            assessment_ref_ids=unread,
        )
    except ModelFailure as error:
        return {"path": "DONE", **_handoff(context, error.code)}
    if synthesis:
        context.audit.event(
            "investigation_synthesis",
            {"phase_id": "synthesize-0", "upstream_claim_ids": [claim["id"] for claim in claims]},
        )
        if any(receipt["name"] == "request_targeted_check" for receipt in result["receipts"]):
            reason = "INVESTIGATION_RECHECK_SUPPRESSED"
            context.audit.event(
                "investigation_recheck_suppressed",
                {"reason": reason, "source": "SYNTHESIS_TOOL_REQUEST"},
            )
            return {"path": "DONE", **_handoff(context, reason)}
        if unread and _asks_for_material(result):
            return {"path": "DONE", **_material_handoff(context, unread)}
        return {"path": "DONE", **_publish_options(context, result, claims=claims)}
    context.audit.event(
        "investigation_assessment",
        {
            "claims": claims,
            "assessments": result["assessments"],
            "recheck_count": count,
            "stale_claim_ids": state.get("stale_claims", []),
        },
    )
    request = result.get("recheck")
    if request is not None:
        disputed = {
            item["claim_id"] for item in result["assessments"] if item["verdict"] != "CONFIRMED"
        }
        if not set(request["claim_ids"]) <= disputed:
            return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
        if count or state["path"] == "STAGED":
            return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_UNRESOLVED")}
        if context.arm == NO_RECHECK_ARM:
            try:
                executor, _, _ = validate_recheck(
                    context, RECHECK_REQUEST.validate_python(request), claims, result["assessments"]
                )
            except (ValueError, BusinessError):
                return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_TOOL_DENIED")}
            reason = "INVESTIGATION_RECHECK_SUPPRESSED"
            context.audit.event(
                "investigation_recheck_suppressed",
                {
                    "claim_ids": request["claim_ids"],
                    "target_role": request["target_role"],
                    "executor_role": executor,
                    "tool_name": request["tool_name"],
                    "reason": request["reason"],
                },
            )
            return {
                "path": "DONE",
                **_output(
                    context,
                    reason=reason,
                    unresolved=[*result.get("unresolved", []), request["reason"], reason],
                ),
            }
        return {
            "path": "RECHECK",
            "recheck": request,
            "active_claims": claims,
            "assessment": result,
        }
    if unread and (not result["options"] or _asks_for_material(result)):
        return {"path": "DONE", **_material_handoff(context, unread)}
    if state.get("stale_claims"):
        result["unresolved"] = [
            *result.get("unresolved", []),
            "局部复查使部分依赖主张失效，尚未重新核对。",
        ]
        if not result.get("options"):
            return {
                "path": "DONE",
                **_handoff(context, "INVESTIGATION_DEPENDENT_CLAIMS_UNRESOLVED"),
            }
    return {
        "path": "DONE",
        **_publish_options(
            context,
            result,
            claims=claims,
            verified=True,
            evidence_scope=evidence if count else None,
        ),
    }


def affected_claims(claims, targets):
    affected = set(targets)
    while True:
        next_ids = {
            claim["id"]
            for claim in claims
            if set(claim["depends_on_claims"]).intersection(affected)
        } - affected
        if not next_ids:
            return affected
        affected.update(next_ids)


def _recheck(state, runtime: Runtime[InvestigationContext]):
    context = runtime.context
    try:
        request = RECHECK_REQUEST.validate_python(state["recheck"])
    except ValueError:
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_TOOL_DENIED")}
    targets = set(request.claim_ids)
    claims = state["active_claims"]
    target_claims = [row for row in claims if row["id"] in targets]
    assessments = state.get("assessment", {}).get("assessments", [])
    counterexamples = [
        item
        for item in assessments
        if item["claim_id"] in targets and item["verdict"] != "CONFIRMED"
    ]
    try:
        executor, arguments, related = validate_recheck(context, request, claims, assessments)
        receipt = context.tools.call(executor, request.tool_name, arguments)
    except InvalidOutput:
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
    except (ValueError, BusinessError):
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_TOOL_DENIED")}
    raw_receipt = receipt.model_dump(mode="json")
    receipt_sha256 = digest(raw_receipt)
    context.audit.event(
        PRIVATE_RECEIPT_EVENT,
        {
            "role": executor,
            "executor_role": executor,
            "target_role": request.target_role,
            "phase_id": "recheck",
            "receipt_sha256": receipt_sha256,
            "receipt": raw_receipt,
        },
    )
    if request.tool_name == "read_refund_execution" and not raw_receipt["data"].get("execution"):
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
    scope_refs = {
        row["source_id"]
        for row in raw_receipt["refs"]
        if request.tool_name != "search_applicable_policy" or row["source_kind"] == "POLICY"
    }
    refs = [row["source_id"] for row in raw_receipt["refs"]]
    alternatives = {}
    if request.tool_name == "read_policy_clause" and context.input.policy_catalogue_version:
        alternatives = {
            row.ref.source_id: row.data["clause"]["id"]
            for row in context.input.evidence
            if row.ref.source_kind == "POLICY"
        }
    new_policies = {alternatives[ref] for ref in scope_refs if ref in alternatives}
    linked_lines = set(
        context.input.refund_execution_line_ids.get(arguments.get("execution_id"), ())
    )
    for referenced in related:
        same_policy = new_policies.intersection(
            alternatives[ref] for ref in referenced if ref in alternatives
        )
        if (
            not referenced.intersection(scope_refs)
            and not same_policy
            and not referenced & linked_lines
        ):
            return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
    affected = affected_claims(claims, targets)
    retired_claims = [row for row in claims if row["id"] in affected]
    audit = {
        "target_role": request.target_role,
        "executor_role": executor,
        "claim_ids": sorted(targets),
        "stale_claim_ids": sorted(affected),
        "tool": request.tool_name,
        "evidence_ids": refs,
        "receipt_sha256": receipt_sha256,
        "counterexamples": counterexamples,
        "retired_claims": retired_claims,
        "reason": request.reason,
    }
    context.audit.event("investigation_recheck", audit)
    invalidated = {
        "recheck_count": 1,
        "active_claims": [row for row in claims if row["id"] not in affected],
        "stale_claims": sorted(affected),
        "retired_claims": retired_claims,
        "recheck_receipt": raw_receipt,
    }
    prior_refs = {
        ref for row in target_claims for ref in row["supporting_refs"] + row["conflicting_refs"]
    } & set(state.get("evidence_ids", _evidence(state)))
    prior_evidence = [
        row.model_dump(mode="json")
        for row in context.input.evidence
        if row.ref.source_kind == "POLICY" and row.ref.source_id in prior_refs
    ]
    try:
        revised = _phase(
            context,
            request.target_role,
            "recheck",
            known=[*refs, *(row["ref"]["source_id"] for row in prior_evidence)],
            prompt={
                **context.input.role_view(request.target_role),
                "specific_claims": target_claims,
                "prior_target_evidence": prior_evidence,
                "counterexamples": counterexamples,
                "check_reason": request.reason,
                "revision_instruction": (
                    "仅复查 specific_claims 中的主张。保留每条主张的原始 id（去掉角色前缀）、"
                    "subject 和 predicate，按补查回执更新 value、state 和证据引用。"
                ),
                "check_receipt": raw_receipt,
            },
            max_rounds=1,
            tools_enabled=False,
        )
    except ModelFailure as error:
        return {"path": "DONE", **invalidated, **_handoff(context, error.code)}
    replacements = _claims(request.target_role, revised)
    old_identity = {(row["id"], row["subject"], row["predicate"]) for row in target_claims}
    new_identity = {(row["id"], row["subject"], row["predicate"]) for row in replacements}
    if new_identity != old_identity:
        return {
            "path": "DONE",
            **invalidated,
            **_handoff(context, "INVESTIGATION_RECHECK_RESULT_INVALID"),
        }
    context.audit.event(
        "investigation_claim_revision",
        {
            "retired_claims": retired_claims,
            "replacement_claims": replacements,
            "target_claim_ids": sorted(targets),
            "unresolved_stale_claim_ids": sorted(affected - targets),
            "target_role": request.target_role,
            "executor_role": executor,
            "reason": request.reason,
        },
    )
    active = invalidated["active_claims"] + replacements
    surviving_refs = {
        ref for row in active for ref in row["supporting_refs"] + row["conflicting_refs"]
    }
    return {
        "path": "MULTI",
        **invalidated,
        "active_claims": active,
        "evidence_ids": sorted(surviving_refs | set(refs)),
        "stale_claims": sorted(affected - targets),
    }


def _conflict(claims):
    seen = defaultdict(set)
    for claim in claims:
        if claim["state"] != "SUPPORTED":
            return True
        seen[claim["subject"], claim["predicate"]].add(claim["value"])
    return any(len(values) > 1 for values in seen.values())


def _publish_options(
    context, result, *, claims=None, verified=False, evidence_scope=None, emit=True
):
    claims = result.get("claims", []) if claims is None else claims
    unresolved = result.get("unresolved", [])
    candidate = _OPTIONS.validate_python(result.get("options", []))
    if not candidate:
        return _handoff(context, "INVESTIGATION_NO_CANDIDATE")
    blockers = list(unresolved)
    if (
        not claims
        or _conflict(claims)
        or (verified and any(item["verdict"] != "CONFIRMED" for item in result["assessments"]))
    ):
        blockers.append("事实或条款主张仍有未核实或冲突项。")
    policy_refs = defaultdict(set)
    for row in context.input.evidence:
        if row.ref.source_kind == "POLICY":
            policy_refs[row.data["clause"]["id"]].add(row.ref.source_id)
    options, checks, seen = [], {}, set()
    allowed_evidence = None if evidence_scope is None else set(evidence_scope)
    for option in candidate:
        if option.id in seen:
            return _handoff(context, "INVESTIGATION_DUPLICATE_OPTION")
        seen.add(option.id)
        if allowed_evidence is not None and not set(option.evidence_ids) <= allowed_evidence:
            checks[option.id] = "INVESTIGATION_OPTION_SCOPE_INVALID"
            continue
        if isinstance(option, NonExecutingOption):
            if set(option.target_line_ids) <= context.line_ids:
                options.append(option)
            else:
                checks[option.id] = "INVESTIGATION_OPTION_SCOPE_INVALID"
            continue
        if (
            not set(option.evidence_ids) <= context.trusted_evidence_ids
            or not {line.line_id for line in option.lines} <= set(option.evidence_ids)
            or not {line.line_id for line in option.lines} <= context.line_ids
            or not all(
                policy_refs[policy_id].intersection(option.evidence_ids)
                for policy_id in option.policy_ids
            )
        ):
            checks[option.id] = "INVESTIGATION_OPTION_SCOPE_INVALID"
            continue
        options.append(option)
        if context.policy_authority is not None and not exact_policy_refs_match(
            context.policy_authority, option
        ):
            checks[option.id] = "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE"
            continue
        try:
            checked = context.validate_draft(
                action=option.action,
                lines=option.business_lines(),
                policy_ids=list(option.policy_ids),
            )
        except BusinessError as error:
            checks[option.id] = error.code
        else:
            if not set(option.policy_ids) <= set(checked["policy_ids"]):
                checks[option.id] = "INVESTIGATION_OPTION_POLICY_INVALID"
    (context.audit.event if emit else lambda *_: None)(
        "investigation_result",
        {
            "claims": claims,
            "assessments": result.get("assessments", []),
            "proposed_options": [option.model_dump(mode="json") for option in candidate],
            "selected_option_ids": [option.id for option in options],
            "program_checks": checks,
        },
    )
    if not options:
        return _handoff(context, "INVESTIGATION_RULE_OR_EVIDENCE_BLOCKED")
    if "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE" in checks.values():
        blockers.append("建议引用的政策版本不适用于目标商品。")
    if any(checks.get(option.id) for option in options):
        blockers.append("一项或多项原始方案未通过业务规则校验。")
    eligible = not blockers and any(isinstance(item, Option) for item in options)
    return _output(
        context,
        reason="ELIGIBLE" if eligible else "HUMAN_REVIEW",
        options=options,
        unresolved=tuple(dict.fromkeys(blockers)),
        status="COMPLETED" if eligible else "NEEDS_REVIEW",
    )


def build_investigation_graph(checkpointer):
    graph = StateGraph(InvestigationState, context_schema=InvestigationContext)
    graph.add_node("route", _route)
    graph.add_node("strong", _strong)
    graph.add_node("fact", _role("fact"))
    graph.add_node("policy", _role("policy"))
    graph.add_node("sequential_fact", _role("fact"))
    graph.add_node("sequential_policy", _role("policy", sequential_claims=True))
    graph.add_node("staged_fact", _role("fact", staged=True))
    graph.add_node("staged_policy", _role("policy", staged=True))
    graph.add_node("verify", _verify)
    graph.add_node("recheck", _recheck)
    graph.add_node("kernel_review", _kernel_review)
    graph.add_edge("kernel_review", END)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        lambda state: {
            "PROGRAM": END,
            "SINGLE": "strong",
            "STAGED": "staged_fact",
            "MULTI": "sequential_fact" if state.get("sequential_claims") else ["fact", "policy"],
        }[state["path"]],
    )
    graph.add_conditional_edges(
        "strong",
        lambda state: ["fact", "policy"]
        if state["path"] == "MULTI"
        else "kernel_review"
        if state["path"] == "REVIEW"
        else END,
    )
    graph.add_edge(["fact", "policy"], "verify")
    graph.add_edge("sequential_fact", "sequential_policy")
    graph.add_edge("sequential_policy", "verify")
    graph.add_edge("staged_fact", "staged_policy")
    graph.add_edge("staged_policy", "verify")
    graph.add_conditional_edges(
        "verify", lambda state: "recheck" if state["path"] == "RECHECK" else END
    )
    graph.add_conditional_edges(
        "recheck", lambda state: "verify" if state["path"] == "MULTI" else END
    )
    return graph.compile(checkpointer=checkpointer)
