"""Program-first routing over verified facts and scoped evidence relationships."""

import re

from aftercare.business.refund_lookup import locate_refund, resolve_refund_text

from .contracts import InvestigationInput, RouteDecision
from .input import InputUnavailable

_NOT_DELIVERED = re.compile(r"(?:我|本人)?(?:还|仍)?(?:没有|没|未)收到(?:货|包裹|商品)?")
_INVESTIGABLE_POLICY = {
    "UNKNOWN_POLICY_TIME",
    "UNKNOWN_POLICY_PRODUCT_SCOPE",
    "CONFLICTING_POLICY_PRIORITY",
    "INCOMPATIBLE_SCOPED_TERMS",
}


def route_unavailable(error: InputUnavailable) -> RouteDecision:
    return RouteDecision(
        path="PROGRAM",
        reason=error.code,
        financial_risk={},
        output={"status": "NEEDS_REVIEW", "missing_materials": []},
    )


def route_input(value: InvestigationInput) -> RouteDecision:
    groups = {}
    for row in value.evidence:
        groups.setdefault(row.ref.source_kind, []).append(row)
    eligibility = value.eligibility
    unknown = any(
        row.data["status"] in {"SUBMITTING", "UNKNOWN"} for row in groups.get("EXECUTION", [])
    )
    financial_risk = {
        "maximum_refundable_cents": eligibility.get("available_cents"),
        "has_unsettled_refund": unknown,
        "requires_existing_business_controls": True,
    }

    def program(reason, status, *, refs=(), **output):
        return RouteDecision(
            path="PROGRAM",
            reason=reason,
            financial_risk=financial_risk,
            evidence_refs=refs,
            output={"status": status, "missing_materials": [], **output},
        )

    if value.intent == "refund_status":
        ledger = [row.data for row in groups.get("EXECUTION", [])]
        lookup = resolve_refund_text(ledger, *(item.text for item in value.messages))
        if lookup["status"] == "REFERENCE_REQUIRED":
            lookup = {
                "status": "ORDER_LEDGER" if ledger else "NO_RECORD",
                "executions": [locate_refund(ledger, row["id"])["execution"] for row in ledger],
            }
        status = (
            "WAITING_INPUT"
            if lookup["status"] in {"AMBIGUOUS_REFERENCE", "INVALID_REFERENCE"}
            else "COMPLETED"
        )
        return program(
            "REFUND_LOOKUP",
            status,
            refund_lookup=lookup,
            refs=tuple(row.ref for kind in ("LEDGER", "EXECUTION") for row in groups.get(kind, [])),
        )
    soft_policy = bool(value.policy_blockers) and all(
        item.split(":", 1)[0] in _INVESTIGABLE_POLICY for item in value.policy_blockers
    )
    if (
        value.policy_blockers
        and not soft_policy
        or eligibility.get("reason_code") == "POLICY_REQUIRED"
    ):
        return program(
            "POLICY_BLOCKED",
            "NEEDS_REVIEW",
            blockers=list(value.policy_blockers) or [eligibility["reason_code"]],
        )
    if unknown:
        return program(
            "REFUND_EXECUTION_UNSETTLED",
            "NEEDS_REVIEW",
            refs=tuple(
                row.ref
                for row in groups["EXECUTION"]
                if row.data["status"] in {"SUBMITTING", "UNKNOWN"}
            ),
        )
    observations = groups.get("OBSERVATION", [])
    useful_observations = any(
        row.data["certainty"] == "OBSERVED"
        or set(row.data.get("findings", ())) - {"BLUR", "OCCLUDED", "UNCERTAIN_CONTENT"}
        for row in observations
    )
    evidence_pending = (
        eligibility.get("reason_code") == "MATERIAL_REVIEW_REQUIRED"
        or eligibility.get("status") == "WAITING_INPUT"
        and useful_observations
    )
    if eligibility.get("status") == "WAITING_INPUT" and not evidence_pending:
        return program(
            "EVIDENCE_REQUIRED", "WAITING_INPUT", missing_materials=eligibility["missing_materials"]
        )
    if eligibility.get("status") == "INELIGIBLE":
        return program("INELIGIBLE", "COMPLETED", business_reason=eligibility.get("reason"))
    if eligibility.get("status") != "ELIGIBLE" and not (soft_policy or evidence_pending):
        return program(eligibility.get("reason_code") or "HUMAN_REVIEW", "NEEDS_REVIEW")

    features = []
    refs = []
    if soft_policy:
        features.append("POLICY_APPLICABILITY_UNRESOLVED")
        refs.extend(row.ref for row in groups.get("POLICY", []))
    lines = groups.get("ORDER_LINE", [])
    if len(lines) > 1:
        features.append("MULTIPLE_CLAIMED_LINES")
        refs.extend(row.ref for row in lines)
    delivered = next(
        (
            row
            for row in groups.get("CONDITION", [])
            if row.ref.source_id == "order_status" and row.data["value"] == "DELIVERED"
        ),
        None,
    )
    disputed = [
        message.ref
        for message in value.messages
        if any(
            _NOT_DELIVERED.fullmatch(sentence.strip())
            for sentence in re.split(r"[。！？!?；;\n]", message.text)
        )
    ]
    if delivered and disputed:
        features.append("CUSTOMER_DISPUTES_RECORDED_DELIVERY")
        refs.extend([delivered.ref, *disputed])
    version_groups = {}
    for row in groups.get("POLICY", []):
        version_groups.setdefault(row.data["clause"]["id"], []).append(row)
    variants = [row for rows in version_groups.values() if len(rows) > 1 for row in rows]
    if variants:
        features.append("MULTIPLE_APPLICABLE_POLICY_VERSIONS")
        refs.extend(row.ref for row in variants)
    return RouteDecision(
        path="MULTI" if features else "SINGLE",
        reason="COMPLEX_EVIDENCE" if features else "SCOPED_STANDARD",
        complexity_features=tuple(features),
        financial_risk=financial_risk,
        evidence_refs=tuple({(ref.source_kind, ref.source_id): ref for ref in refs}.values()),
        output={
            "business_status": eligibility.get("status"),
            "material_review_required": evidence_pending,
            "policy_blockers": list(value.policy_blockers),
        },
    )
