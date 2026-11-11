"""Check registered citation prerequisites against an authorized business snapshot."""

from dataclasses import dataclass

from aftercare.business.refund_lookup import targeted_refund
from aftercare.intelligence.policies import document_key

CITATION_REGISTRY_VERSION = "conditional-citations-v2"


@dataclass(frozen=True)
class CitationCondition:
    use: str
    condition: str


# Keys identify the unmodified, reviewed policy documents in the v0.1 catalogue.
CONDITIONS = {
    (
        "POL08",
        "1.0",
        "a968be326bdac61225ed0cd761fecf8cff42bc9d77176f63df39974b8a5ec315",
    ): CitationCondition("REUSE_EXISTING_REFUND", "SAME_REFUND_REQUEST_RECORD"),
    (
        "POL12",
        "1.0",
        "48b64e9dedf089aa73c6a577cf824612db52f08c05def1aaf636bc229104c8a2",
    ): CitationCondition("RECONCILE_UNKNOWN_REFUND", "REFUND_EXECUTION_RESULT_UNRESOLVED"),
}


def _same_refund_request(ledger, *, matched=False):
    if matched:
        return "MET", "SAME_REFUND_REQUEST_VERIFIED", [ledger[0]["id"]]
    if not ledger:
        return "UNMET", "NO_REFUND_EXECUTION_RECORD", []
    # The order ledger does not carry a trusted binding to the particular request.
    return "UNKNOWN", "SAME_REFUND_REQUEST_NOT_VERIFIED", []


def _unresolved_refund_execution(ledger, *, matched=False):
    execution_ids = sorted(
        {
            row["id"]
            for row in ledger
            if row.get("status") == "UNKNOWN" and isinstance(row.get("id"), str) and row["id"]
        }
    )
    if execution_ids:
        return "MET", "UNKNOWN_REFUND_EXECUTION_FOUND", execution_ids
    if not ledger:
        return "UNMET", "NO_REFUND_EXECUTION_RECORD", []
    if any(row.get("status") not in ("SUCCEEDED", "FAILED", "SUBMITTING") for row in ledger):
        return "UNKNOWN", "REFUND_EXECUTION_EVIDENCE_INCOMPLETE", []
    if any(row["status"] == "SUBMITTING" for row in ledger):
        return "UNKNOWN", "REFUND_STILL_SUBMITTING_TIMEOUT_NOT_VERIFIED", []
    return "UNMET", "NO_CONFIRMED_UNKNOWN_REFUND_RESULT", []


CONDITION_HANDLERS = {
    "SAME_REFUND_REQUEST_RECORD": _same_refund_request,
    "REFUND_EXECUTION_RESULT_UNRESOLVED": _unresolved_refund_execution,
}


def assess_citations(citations: list[dict], evidence: dict, facts: dict) -> list[dict]:
    """Preserve the quote order and report preconditions, independent of semantic support."""
    return [_assess_citation(citation, evidence, facts) for citation in citations]


def _assess_citation(citation, evidence, facts):
    key = citation["id"], citation["version"]
    assessment = {
        **citation,
        "use": None,
        "condition_status": "NOT_ASSESSED",
        "reason_code": "CONDITION_NOT_REGISTERED_FOR_DOCUMENT",
        "registry_version": CITATION_REGISTRY_VERSION,
        "scope": {"kind": None, "execution_ids": []},
    }
    document = evidence.get(key)
    condition = CONDITIONS.get((*key, document_key(document))) if document else None
    if condition is None:
        return assessment
    assessment["use"] = condition.use
    handler = CONDITION_HANDLERS.get(condition.condition)
    if handler is None:
        assessment["reason_code"] = "CONDITION_HANDLER_UNAVAILABLE"
        return assessment

    scope = (
        "ORDER_REFUND_EXECUTION_LEDGER"
        if facts["case"]["intent"] == "refund_status"
        else "CURRENT_CASE_EXECUTION_LEDGER"
    )
    ledger = facts.get("ledger")
    lookup = facts.get("refund_lookup") if facts["case"]["intent"] == "refund_status" else None
    targeted = targeted_refund(lookup)
    if targeted:
        scope = "MATCHED_REFUND_EXECUTION"
    if not isinstance(ledger, list) or any(
        not isinstance(row, dict) or row.get("action") not in ("REFUND", "REPLACE")
        for row in ledger
    ):
        status, reason, execution_ids = "UNKNOWN", "REFUND_LEDGER_EVIDENCE_UNAVAILABLE", []
    elif not targeted:
        refunds = [row for row in ledger if row["action"] == "REFUND"]
        status, reason, execution_ids = handler(refunds)
    elif lookup["status"] == "NOT_FOUND":
        status, reason, execution_ids = "UNMET", "TARGET_REFUND_RECORD_NOT_FOUND", []
    elif lookup["status"] != "FOUND":
        status, reason, execution_ids = "UNKNOWN", "SAME_REFUND_REQUEST_NOT_VERIFIED", []
    else:
        execution_id = (lookup.get("execution") or {}).get("id")
        refunds = [
            row
            for row in ledger
            if row["action"] == "REFUND" and execution_id and row.get("id") == execution_id
        ]
        if len(refunds) != 1:
            status, reason, execution_ids = "UNKNOWN", "REFUND_LEDGER_EVIDENCE_UNAVAILABLE", []
        else:
            status, reason, execution_ids = handler(refunds, matched=True)
    assessment.update(
        condition_status=status,
        reason_code=reason,
        scope={"kind": scope, "execution_ids": execution_ids},
    )
    return assessment
