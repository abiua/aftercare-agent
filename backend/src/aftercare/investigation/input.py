"""Project an already-authorized P3 snapshot into bounded investigation data."""

import json
import re

from aftercare.business.rules import digest
from aftercare.conversations.decisions import _relevant
from aftercare.decisions.contracts import DecisionScope

from .catalogue import CATALOGUE_REF, valid_capsule
from .contracts import (
    ORDER_OWNERSHIP_REF,
    CustomerMessage,
    Evidence,
    EvidenceRef,
    InvestigationInput,
    account_order_ownership,
)
from .material_observations import (
    HUMAN_MATERIAL_PROJECTION_V1,
    ORDER_PAYMENT_REF,
    recorded_order_payment,
    validate_review_observations,
)
from .protocol import HUMAN_PAYMENT_V7

MAX_TEXT_BYTES = 24_000
MAX_EVIDENCE_BYTES = 196_608
_IDENTITY = re.compile(
    r"(?i)[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}|"
    r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)|"
    r"(?<!\d)\d{17}[\dXx](?!\w)"
)
_CONDITIONS = {
    "order_status": "status",
    "payment_status": "payment_status",
    "paid_at": "paid_at",
    "shipped_at": "shipped_at",
    "delivered_at": "delivered_at",
    "cancelled_at": "cancelled_at",
}


class InputUnavailable(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fields(row, names):
    return {key: row[key] for key in names if key in row}


def build_input(scope: DecisionScope, *, evidence_projection=None) -> InvestigationInput:
    """Validate the frozen document again because its nested values are mutable."""
    if scope.frozen_context is None:
        raise InputUnavailable("INVESTIGATION_SNAPSHOT_REQUIRED")
    try:
        scope = DecisionScope.model_validate(scope.model_dump(mode="json"))
        stored_projection = scope.frozen_context.get("evidence_projection")
        if stored_projection not in (
            None,
            HUMAN_MATERIAL_PROJECTION_V1,
            HUMAN_PAYMENT_V7.evidence_projection,
        ):
            raise InputUnavailable("INVESTIGATION_EVIDENCE_PROJECTION_UNSUPPORTED")
        if stored_projection != evidence_projection:
            raise InputUnavailable("INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH")
        return _project(scope, stored_projection)
    except InputUnavailable:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InputUnavailable("INVESTIGATION_SNAPSHOT_INVALID") from None


def _project(scope, evidence_projection):
    data = scope.frozen_context
    case, order = data["case"], data["order"]
    if (
        case["id"] != scope.case_id
        or case["customer_id"] != scope.customer_id
        or case["order_id"] != scope.order_id
        or order["id"] != scope.order_id
        or sorted(case["line_ids"]) != sorted(scope.line_ids)
    ):
        raise InputUnavailable("INVESTIGATION_SCOPE_MISMATCH")
    scoped = (scope.conversation_id, scope.issue_id, scope.case_id)
    bindings = {(ref.kind, ref.id): ref.sha256 for ref in scope.input_refs}

    def source(kind, ident, value, observed_at=None, *, bound_kind=None, committed=None):
        checksum = digest(committed if bound_kind else value)
        if bound_kind and bindings.get((bound_kind, ident)) != checksum:
            raise InputUnavailable("INVESTIGATION_REFERENCE_MISMATCH")
        return EvidenceRef(
            source_kind=kind,
            source_id=ident,
            scope=scoped,
            version_or_sha256=checksum,
            observed_at=observed_at,
        )

    messages = []
    all_messages = {}
    last_sequence = 0
    for item in data["messages"]:
        ident, sequence, raw = item["id"], item["sequence"], item["text"]
        if ident in all_messages or not last_sequence < sequence <= scope.input_through_seq:
            raise InputUnavailable("INVESTIGATION_SEQUENCE_INVALID")
        all_messages[ident] = sequence
        last_sequence = sequence
        original = [sequence, raw]
        ref = source("MESSAGE", ident, original, bound_kind="MESSAGE", committed=original)
        if _relevant(raw):
            messages.append(
                CustomerMessage(ref=ref, sequence=sequence, text=_IDENTITY.sub("[已隐藏]", raw))
            )
    if sum(len(row.text.encode()) for row in messages) > MAX_TEXT_BYTES:
        raise InputUnavailable("INVESTIGATION_TEXT_LIMIT")

    evidence = []

    def add(kind, ident, projection, *, trust="VERIFIED_RECORD", **options):
        ref = source(kind, ident, projection, **options)
        evidence.append(Evidence(ref=ref, data=projection, trust=trust))

    attachments = {}
    for item in data["attachments"]:
        ident, parent = item["id"], item["message_id"]
        if parent not in all_messages or ident in attachments:
            raise InputUnavailable("INVESTIGATION_ATTACHMENT_SCOPE")
        attachments[ident] = item["sha256"]
        add(
            "ATTACHMENT",
            ident,
            _fields(item, ("message_id", "sha256", "content_type", "size")),
            trust="CUSTOMER_ATTACHMENT",
            bound_kind="ATTACHMENT",
            committed=[parent, item["sha256"], item["size"], item["content_type"]],
        )
    for item in data["observations"]:
        parent = item["attachment_id"]
        if attachments.get(parent) != item["original_sha256"] or parent not in attachments:
            raise InputUnavailable("INVESTIGATION_OBSERVATION_SCOPE")
        add(
            "OBSERVATION",
            item["id"],
            _fields(
                item,
                (
                    "attachment_id",
                    "turn_id",
                    "attempt_id",
                    "original_sha256",
                    "sent_sha256",
                    "transform_version",
                    "candidate_kind",
                    "certainty",
                    "findings",
                    "summary",
                ),
            ),
            trust="UNVERIFIED_OBSERVATION",
            bound_kind="OBSERVATION",
            committed=[
                parent,
                item["original_sha256"],
                item["sent_sha256"],
                item["certainty"],
                item["findings"],
            ],
        )
    for item in data["materials"]:
        add(
            "MATERIAL",
            item["id"],
            item,
            observed_at=item.get("created_at"),
            bound_kind="FACT",
            committed=item,
        )
    for item in data["reviews"]:
        observations = None
        if "observations" in item:
            if evidence_projection not in (
                HUMAN_MATERIAL_PROJECTION_V1,
                HUMAN_PAYMENT_V7.evidence_projection,
            ):
                raise InputUnavailable("INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH")
            try:
                observations = validate_review_observations(
                    item,
                    {row["id"]: row for row in data["materials"]},
                    {row["id"]: row for row in data["attachments"]},
                    {
                        row["line_id"]: row["quantity"]
                        for row in order["items"]
                        if row["line_id"] in scope.line_ids
                    },
                    case["materials_version"],
                )
            except (KeyError, TypeError, ValueError):
                raise InputUnavailable(
                    "INVESTIGATION_HUMAN_MATERIAL_OBSERVATIONS_INVALID"
                ) from None
        if item["effective"]:
            projection = _fields(
                item, ("materials_version", "material_ids", "decision", "created_at")
            )
            if observations is not None:
                projection["observations"] = observations
            add(
                "REVIEW",
                item["id"],
                projection,
                observed_at=item.get("created_at"),
                bound_kind="FACT",
                committed=item,
            )
    balances = {row["line_id"]: row for row in data["eligibility"]["lines"]}
    explicit_tool_bindings = any(
        (kind == "FACT" and ident in balances)
        or (kind == "POLICY" and ident != "effective-policy-snapshot")
        for kind, ident in bindings
    )
    if (
        len(balances) != len(data["eligibility"]["lines"])
        or set(balances) != set(scope.line_ids)
        or sorted(row["line_id"] for row in order["items"]) != sorted(scope.line_ids)
    ):
        raise InputUnavailable("INVESTIGATION_LINE_SCOPE")
    for row in order["items"]:
        ident = row["line_id"]
        add(
            "ORDER_LINE",
            ident,
            {
                **_fields(
                    row,
                    (
                        "product_id",
                        "sku",
                        "title",
                        "quantity",
                        "paid_cents",
                        "refunded_cents",
                        "shipment_id",
                        "package_id",
                    ),
                ),
                **_fields(
                    balances[ident],
                    ("available_cents", "replace_available_quantity", "rule_status", "product_id"),
                ),
                "order_version": order["version"],
                "delivery_status": order.get("status"),
            },
            bound_kind="FACT" if explicit_tool_bindings else None,
            committed=[row, balances[ident]],
        )
    for name, field in _CONDITIONS.items():
        if field in order:
            add("CONDITION", name, {"value": order[field], "order_version": order["version"]})
    payment_ref = ("FACT", ORDER_PAYMENT_REF)
    if evidence_projection == HUMAN_PAYMENT_V7.evidence_projection:
        if ORDER_PAYMENT_REF not in scope.evidence_ids:
            raise InputUnavailable("INVESTIGATION_PAYMENT_BINDING_INVALID")
        payment = recorded_order_payment(order)
        add("CONDITION", ORDER_PAYMENT_REF, payment, bound_kind="FACT", committed=payment)
    elif payment_ref in bindings or ORDER_PAYMENT_REF in scope.evidence_ids:
        raise InputUnavailable("INVESTIGATION_PAYMENT_BINDING_INVALID")
    ownership_ref = ("FACT", ORDER_OWNERSHIP_REF)
    if (
        "authenticated_order_ownership" in data
        or ownership_ref in bindings
        or ORDER_OWNERSHIP_REF in scope.evidence_ids
    ):
        ownership = data.get("authenticated_order_ownership")
        if (
            ownership != account_order_ownership(scope.order_id)
            or ORDER_OWNERSHIP_REF not in scope.evidence_ids
        ):
            raise InputUnavailable("INVESTIGATION_OWNERSHIP_BINDING_INVALID")
        add(
            "CONDITION",
            ORDER_OWNERSHIP_REF,
            ownership,
            bound_kind="FACT",
            committed=ownership,
        )
    facts = data["facts"]
    if facts and bindings[("FACT", "case-facts")] != digest(facts):
        raise InputUnavailable("INVESTIGATION_REFERENCE_MISMATCH")
    for name, value in sorted(facts.items()):
        add("CONDITION", f"case:{name}", {"value": value})
    add("LEDGER", "refund-ledger", {"digest": scope.business_versions["ledger"]})
    for item in data["ledger"]:
        if item["action"] == "REFUND":
            add(
                "EXECUTION",
                item["id"],
                _fields(item, ("id", "action", "status", "amount_cents", "created_at", "receipt")),
                observed_at=item.get("created_at"),
            )
    policies = data["policies"]
    if (
        bindings[("POLICY", "effective-policy-snapshot")] != digest(policies)
        or policies["snapshot"] != scope.policy_catalogue_sha256
        or policies["snapshot"] != scope.business_versions["policies"]
    ):
        raise InputUnavailable("INVESTIGATION_REFERENCE_MISMATCH")
    catalogue = data.get("policy_catalogue")
    policy_context = {}
    if catalogue is not None:
        if (
            not valid_capsule(catalogue)
            or bindings.get(("POLICY", CATALOGUE_REF)) != digest(catalogue)
            or {product["id"] for product in catalogue["products"]}
            != {row["product_id"] for row in order["items"]}
        ):
            raise InputUnavailable("INVESTIGATION_POLICY_CATALOGUE_INVALID")
        policy_context = {
            "catalogue_sha256": catalogue["documents_sha256"],
            "products": catalogue["products"],
            "policy_defaults": {"status": "PUBLISHED", "time_basis": "decision_at", "priority": 0},
            "timestamps": {
                "decision_at": catalogue["decision_at"],
                "case_created_at": case.get("created_at"),
                "order_created_at": order.get("created_at"),
                "paid_at": order.get("paid_at"),
                "delivered_at": order.get("delivered_at"),
            },
        }
        for item in catalogue["documents"]:
            add(
                "POLICY",
                f"{item['id']}:{item['version']}",
                {"clause": item, "catalogue_sha256": catalogue["documents_sha256"]},
                trust="POLICY",
                bound_kind="POLICY",
                committed=item,
            )
    else:
        for item in policies["documents"]:
            key = f"{item['id']}:{item['version']}"
            selection = [
                product
                for product, docs in policies["by_product"].items()
                if any(
                    doc["id"] == item["id"] and doc["version"] == item["version"] for doc in docs
                )
            ]
            if not selection:
                raise InputUnavailable("INVESTIGATION_POLICY_SCOPE")
            add(
                "POLICY",
                key,
                {
                    "clause": item,
                    "selected_for": sorted(selection),
                    "snapshot": policies["snapshot"],
                },
                trust="POLICY",
                bound_kind="POLICY" if explicit_tool_bindings else None,
                committed=item,
            )
    payload = [row.model_dump(mode="json") for row in evidence]
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_EVIDENCE_BYTES:
        raise InputUnavailable("INVESTIGATION_EVIDENCE_LIMIT")
    if len({(row.ref.source_kind, row.ref.source_id) for row in evidence}) != len(evidence):
        raise InputUnavailable("INVESTIGATION_DUPLICATE_EVIDENCE")
    relevant = digest(
        [
            scope.dependency_sha256,
            [row.model_dump(mode="json") for row in messages],
            payload,
            *([evidence_projection] if evidence_projection else []),
            *(
                [
                    {
                        **policy_context,
                        "timestamps": {
                            key: value
                            for key, value in policy_context["timestamps"].items()
                            if key != "decision_at"
                        },
                    }
                ]
                if policy_context
                else []
            ),
        ]
    )
    return InvestigationInput(
        conversation_id=scope.conversation_id,
        issue_id=scope.issue_id,
        case_id=scope.case_id,
        order_id=scope.order_id,
        input_through_seq=scope.input_through_seq,
        source_snapshot_sha256=scope.input_sha256,
        relevant_input_sha256=relevant,
        messages=tuple(messages),
        evidence=tuple(evidence),
        eligibility=data["eligibility"],
        policy_blockers=tuple(policies["blockers"]),
        business_versions=scope.business_versions,
        intent=case["intent"],
        policy_catalogue_version=catalogue["version"] if catalogue else None,
        policy_context=policy_context,
        selected_policy_keys=tuple(
            f"{item['id']}:{item['version']}" for item in policies["documents"]
        ),
    )
