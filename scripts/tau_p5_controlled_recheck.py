"""Controlled O9951 recheck for the P5 runtime's audited model context.

Import this module from the budgeted runner. It has no standalone paid entry
point, runtime discovery, database writes, or decision projection.
"""

from copy import deepcopy
from dataclasses import replace

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.investigation.graph import (
    InvestigationState,
    _recheck,
    _role,
    _verify,
)
from aftercare.investigation.orchestration import InvestigationContext
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

EVIDENCE_MODE = "CONTROLLED_UPSTREAM_ERROR_REAL_VERIFIER"
TARGET_ID = "l01_amounts"
CORRECT_AMOUNT = "可用金额3000分"
CONTROLLED_AMOUNT = "可用金额4900分"


def controlled_seed(context, original_report):
    """Bind archived real fact output to the exact frozen O9951 input."""
    value = context.input
    if (
        value.order_id != "O9951"
        or original_report["order"]["id"] != value.order_id
        or (
            original_report["conversation_id"],
            original_report["issue_id"],
            original_report["case_id"],
        )
        != (value.conversation_id, value.issue_id, value.case_id)
        or context.route.path != "MULTI"
    ):
        raise ValueError("P5_CONTROLLED_SCOPE_INVALID")
    events = original_report["paid_usage"]["events"]
    claim_rows = [
        row["payload"]["claims"]
        for row in events
        if row["kind"] == "investigation_claims"
        and row["payload"]["role"] == row["payload"]["phase_id"] == "fact"
    ]
    if len(claim_rows) != 1:
        raise ValueError("P5_CONTROLLED_FACT_PROVENANCE_INVALID")
    receipt_events = [
        row["payload"]
        for row in events
        if row["kind"] == "investigation_receipt"
        and row["payload"]["role"] == row["payload"]["phase_id"] == "fact"
    ]
    if not receipt_events or any(
        row["receipt"]["input_digest"] != value.relevant_input_sha256
        or digest(row["receipt"]) != row["receipt_sha256"]
        or any(
            tuple(ref["scope"]) != (value.conversation_id, value.issue_id, value.case_id)
            for ref in row["receipt"]["refs"]
        )
        for row in receipt_events
    ):
        raise ValueError("P5_CONTROLLED_RECEIPT_PROVENANCE_INVALID")
    claims = []
    for original in claim_rows[0]:
        claim = deepcopy(original)
        prefix, _, claim["id"] = claim["id"].partition("/")
        if prefix != "fact" or claim.pop("role") != "fact":
            raise ValueError("P5_CONTROLLED_FACT_ROLE_INVALID")
        claims.append(claim)
    targets = [row for row in claims if row["id"] == TARGET_ID]
    if (
        len(targets) != 1
        or targets[0]["subject"] != "L01"
        or "L01" not in targets[0]["supporting_refs"]
        or targets[0]["value"].count(CORRECT_AMOUNT) != 1
    ):
        raise ValueError("P5_CONTROLLED_TARGET_INVALID")
    target = targets[0]
    original_value = target["value"]
    target["value"] = original_value.replace(CORRECT_AMOUNT, CONTROLLED_AMOUNT)
    receipts = [row["receipt"] for row in receipt_events]
    evidence_ids = {ref["source_id"] for receipt in receipts for ref in receipt["refs"]}
    if not all(
        set(row["supporting_refs"] + row["conflicting_refs"]) <= evidence_ids for row in claims
    ):
        raise ValueError("P5_CONTROLLED_FACT_REFERENCES_INVALID")
    seed = {"claims": claims, "evidence_ids": sorted(evidence_ids), "receipts": receipts}
    injection = {
        "evidence_mode": EVIDENCE_MODE,
        "claim_id": f"fact/{TARGET_ID}",
        "field": "value",
        "before": original_value,
        "after": target["value"],
        "frozen_input_digest": value.relevant_input_sha256,
        "original_run_id": original_report["paid_usage"]["run"]["id"],
        "archived_fact_receipt_sha256": [row["receipt_sha256"] for row in receipt_events],
    }
    return seed, injection


def _deny_execution_validation(*args, **kwargs):
    raise BusinessError("P5_CONTROLLED_EXECUTION_DISABLED", "受控核验不提交业务执行。")


def invoke_controlled_recheck(context, original_report):
    """Use an already admitted isolated runtime context with a real model.

    The caller owns locking, attempt journaling, spending caps and readback.
    This graph reuses the production policy, verify and recheck nodes.
    """
    fact_result, injection = controlled_seed(context, original_report)
    if context.model is None:
        raise ValueError("P5_CONTROLLED_MODEL_REQUIRED")
    isolated = replace(context, validate_draft=_deny_execution_validation)
    isolated.audit.event("p5_controlled_upstream_error", injection)
    graph = StateGraph(InvestigationState, context_schema=InvestigationContext)
    graph.add_node("policy", _role("policy"))
    graph.add_node("verify", _verify)
    graph.add_node("recheck", _recheck)
    graph.add_edge(START, "policy")
    graph.add_edge("policy", "verify")
    graph.add_conditional_edges(
        "verify", lambda state: "recheck" if state["path"] == "RECHECK" else END
    )
    graph.add_conditional_edges(
        "recheck", lambda state: "verify" if state["path"] == "MULTI" else END
    )
    with tracing_context(enabled=False, parent=False):
        return graph.compile().invoke(
            {"cycle": 0, "path": "MULTI", "recheck_count": 0, "fact_result": fact_result},
            context=isolated,
        )
