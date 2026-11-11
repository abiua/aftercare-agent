"""The checkpointed P4 chat investigation graph."""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.decisions.contracts import CandidateOption, NonExecutingOption, Option
from aftercare.intelligence.models import ModelFailure

from .catalogue import exact_policy_refs_match
from .orchestration import InvestigationContext, ProtectedAudit, RecheckRequest, run_phase
from .prompt_evidence import PRIVATE_RECEIPT_EVENT

CHAT_GRAPH_PROFILE = "chat-investigation-v1"
ARMS = {"auto", "strong_single", "single_staged", "multi"}
_OPTIONS = TypeAdapter(list[CandidateOption])


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
    recheck: dict
    recheck_count: int
    recheck_receipt: dict


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
    if selected != "PROGRAM" and context.arm != "auto":
        selected = {"strong_single": "SINGLE", "single_staged": "STAGED", "multi": "MULTI"}[
            context.arm
        ]
    context.audit.event(
        "investigation_route",
        {
            **route.model_dump(mode="json"),
            "selected_path": selected,
            "arm": context.arm,
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
    return {"path": selected, "recheck_count": 0}


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
        audit=ProtectedAudit(context.audit, context.verifier_reserve)
        if role in {"fact", "policy"} and phase_id != "recheck"
        else None,
    )
    if role != "verifier":
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
        result = _phase(context, "strong_single", "single", max_rounds=8, max_tools=8)
    except ModelFailure as error:
        return {"path": "DONE", **_handoff(context, error.code)}
    if (
        result.get("escalate")
        and context.arm == "auto"
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
    return {"path": "DONE", **_publish_options(context, result)}


def _role(role, *, staged=False):
    def execute(state, runtime: Runtime[InvestigationContext]):
        context = runtime.context
        upstream = (
            _claims("fact", state.get("fact_result", {})) if staged and role == "policy" else None
        )
        try:
            result = _phase(
                context,
                role,
                f"{'staged-' if staged else ''}{role}",
                upstream=upstream,
                known=state.get("fact_result", {}).get("evidence_ids", ()) if upstream else (),
                max_tools=2,
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
    try:
        result = _phase(
            context,
            "verifier",
            f"verify-{count}",
            upstream=claims,
            known=evidence,
            max_rounds=1 if count else 2,
            max_tools=1,
            tools_enabled=not count,
            prompt=prompt,
        )
    except ModelFailure as error:
        return {"path": "DONE", **_handoff(context, error.code)}
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
        return {
            "path": "RECHECK",
            "recheck": request,
            "active_claims": claims,
            "assessment": result,
        }
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
    return {"path": "DONE", **_publish_options(context, result, claims=claims, verified=True)}


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
    request = RecheckRequest.model_validate(state["recheck"])
    targets = set(request.claim_ids)
    claims = state["active_claims"]
    own = {row["id"] for row in claims if row["role"] == request.target_role}
    if not targets <= own:
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
    try:
        context.tools.validate_call(request.target_role, request.tool_name, request.arguments)
        receipt = context.tools.call(request.target_role, request.tool_name, request.arguments)
    except (ValueError, BusinessError):
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_TOOL_DENIED")}
    raw_receipt = receipt.model_dump(mode="json")
    receipt_sha256 = digest(raw_receipt)
    context.audit.event(
        PRIVATE_RECEIPT_EVENT,
        {
            "role": request.target_role,
            "phase_id": "recheck",
            "receipt_sha256": receipt_sha256,
            "receipt": raw_receipt,
        },
    )
    refs = [row["source_id"] for row in raw_receipt["refs"]]
    alternatives = {}
    if request.tool_name == "read_policy_clause" and context.input.policy_catalogue_version:
        alternatives = {
            row.ref.source_id: row.data["clause"]["id"]
            for row in context.input.evidence
            if row.ref.source_kind == "POLICY"
        }
    new_policies = {alternatives[ref] for ref in refs if ref in alternatives}
    for claim in claims:
        if claim["id"] not in targets:
            continue
        referenced = set(claim["supporting_refs"] + claim["conflicting_refs"])
        same_policy = new_policies.intersection(
            alternatives[ref] for ref in referenced if ref in alternatives
        )
        if not referenced.intersection(refs) and not same_policy:
            return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_SCOPE_INVALID")}
    affected = affected_claims(claims, targets)
    audit = {
        "target_role": request.target_role,
        "claim_ids": sorted(targets),
        "stale_claim_ids": sorted(affected),
        "tool": request.tool_name,
        "evidence_ids": refs,
        "receipt_sha256": receipt_sha256,
    }
    context.audit.event("investigation_recheck", audit)
    try:
        revised = _phase(
            context,
            request.target_role,
            "recheck",
            known=refs,
            prompt={
                **context.input.role_view(request.target_role),
                "specific_claims": [row for row in claims if row["id"] in targets],
                "check_receipt": raw_receipt,
            },
            max_rounds=1,
            tools_enabled=False,
        )
    except ModelFailure as error:
        return {"path": "DONE", **_handoff(context, error.code)}
    replacements = _claims(request.target_role, revised)
    if {row["id"] for row in replacements} != targets:
        return {"path": "DONE", **_handoff(context, "INVESTIGATION_RECHECK_RESULT_INVALID")}
    active = [row for row in claims if row["id"] not in affected] + replacements
    surviving_refs = {
        ref for row in active for ref in row["supporting_refs"] + row["conflicting_refs"]
    }
    return {
        "path": "MULTI",
        "recheck_count": 1,
        "active_claims": active,
        "evidence_ids": sorted(surviving_refs | set(refs)),
        "stale_claims": sorted(affected - targets),
        "recheck_receipt": raw_receipt,
    }


def _conflict(claims):
    seen = defaultdict(set)
    for claim in claims:
        if claim["state"] != "SUPPORTED":
            return True
        seen[claim["subject"], claim["predicate"]].add(claim["value"])
    return any(len(values) > 1 for values in seen.values())


def _publish_options(context, result, *, claims=None, verified=False):
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
    for option in candidate:
        if option.id in seen:
            return _handoff(context, "INVESTIGATION_DUPLICATE_OPTION")
        seen.add(option.id)
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
    context.audit.event(
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
    graph.add_node("staged_fact", _role("fact", staged=True))
    graph.add_node("staged_policy", _role("policy", staged=True))
    graph.add_node("verify", _verify)
    graph.add_node("recheck", _recheck)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        lambda state: {
            "PROGRAM": END,
            "SINGLE": "strong",
            "STAGED": "staged_fact",
            "MULTI": ["fact", "policy"],
        }[state["path"]],
    )
    graph.add_conditional_edges(
        "strong", lambda state: ["fact", "policy"] if state["path"] == "MULTI" else END
    )
    graph.add_edge(["fact", "policy"], "verify")
    graph.add_edge("staged_fact", "staged_policy")
    graph.add_edge("staged_policy", "verify")
    graph.add_conditional_edges(
        "verify", lambda state: "recheck" if state["path"] == "RECHECK" else END
    )
    graph.add_conditional_edges(
        "recheck", lambda state: "verify" if state["path"] == "MULTI" else END
    )
    return graph.compile(checkpointer=checkpointer)
