"""Replay saved frozen P5 input through the production prompt preflight offline."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from aftercare.business.rules import digest
from aftercare.decisions.contracts import DecisionScope
from aftercare.intelligence.models import ModelFailure
from aftercare.investigation.graph import _all_claims, _evidence, _verify
from aftercare.investigation.input import build_input
from aftercare.investigation.orchestration import InvestigationContext
from aftercare.investigation.prompt_evidence import PromptEvidence, verifier_claims
from aftercare.investigation.protocol import HUMAN_PAYMENT_V7
from aftercare.investigation.tools import InvestigationTools


class PreflightCapture:
    """Stop before provider transport, with one synthetic tool request to size round 1."""

    def __init__(self, claim_id, ref, tool):
        self.request = {
            "name": "request_targeted_check",
            "id": "offline-preflight",
            "args": {
                "claim_id": claim_id,
                "conflicting_refs": [ref],
                "target_tool": tool,
                "reason": "核对该判断与同一范围的冻结原始记录。" * 10,
            },
        }
        self.events = []

    def event(self, kind, data):
        self.events.append((kind, data))

    def call(self, _audit, logical_id, *, tools, validate, **_kwargs):
        if logical_id.endswith("verify-0:0") and tools:
            return validate([self.request], "")
        raise ModelFailure("OFFLINE_CAPTURE_STOP")


def archived_state(events, value):
    state = {"cycle": 0, "path": "MULTI"}
    for role in ("fact", "policy"):
        claims = [
            claim
            for row in events
            if row["kind"] == "investigation_claims" and row["payload"]["role"] == role
            for claim in row["payload"]["claims"]
        ]
        receipts = [
            row["payload"]["receipt"]
            for row in events
            if row["kind"] == "investigation_receipt" and row["payload"]["role"] == role
        ]
        refs = {ref["source_id"] for receipt in receipts for ref in receipt["refs"]}
        refs.update(
            ref for claim in claims for ref in claim["supporting_refs"] + claim["conflicting_refs"]
        )
        if role == "fact":
            refs.update(row.ref.source_id for row in value.messages)
            refs.update(
                row.ref.source_id
                for row in value.evidence
                if row.ref.source_kind in {"ATTACHMENT", "OBSERVATION"}
            )
        state[f"{role}_result"] = {
            "claims": [
                {
                    **{key: item for key, item in claim.items() if key != "role"},
                    "id": claim["id"].split("/", 1)[1],
                }
                for claim in claims
            ],
            "evidence_ids": sorted(refs),
            "receipts": receipts,
        }
    return state


def compare_business_evidence(value, state):
    refs = set(_evidence(state))
    frozen = [row.model_dump(mode="json") for row in value.evidence if row.ref.source_id in refs]
    receipts = [
        receipt for role in ("fact", "policy") for receipt in state[f"{role}_result"]["receipts"]
    ]
    messages = [row.model_dump(mode="json") for row in value.messages if row.ref.source_id in refs]
    prompt = {
        **value.role_view("verifier"),
        "original_frozen_evidence": frozen,
        "original_read_receipts": receipts,
        "customer_statements": messages,
    }
    codec = PromptEvidence(value, protocol=HUMAN_PAYMENT_V7)
    compact = codec.prompt(prompt)
    table = compact["original_frozen_evidence"]
    restored = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
    for position in compact.get("policy_corpus_rows", []):
        row = restored[position]
        row["data"] = {**row["data"], "catalogue_sha256": value.policy_context["catalogue_sha256"]}
    original = frozen + [row for receipt in receipts for row in receipt["data"].get("records", [])]
    assert {tuple(codec.ref(row["ref"])): codec.record(row) for row in original} == {
        tuple(row["ref"]): row for row in restored
    }
    assert compact["scope"] == [value.conversation_id, value.issue_id, value.case_id]
    assert compact["customer_statements"] == [codec.record(row) for row in messages]

    def source_id(ref):
        if isinstance(ref, int):
            return restored[ref]["ref"][1]
        if isinstance(ref, list):
            kind, position = ref
            assert kind == "message"
            return compact["customer_statements"][position]["ref"][1]
        return ref

    claims = _all_claims(state)
    encoded_claims = verifier_claims(claims, codec.verifier_ref_positions)
    decoded_claims = []
    for cells in encoded_claims["rows"]:
        claim = dict(zip(encoded_claims["columns"], cells, strict=True))
        claim.setdefault("role", claim["id"].split("/", 1)[0])
        claim["subject"] = source_id(claim["subject"])
        for field in ("supporting_refs", "conflicting_refs"):
            claim[field] = [source_id(ref) for ref in claim[field]]
        decoded_claims.append(claim)
    assert decoded_claims == claims
    for raw, projected in zip(receipts, compact["original_read_receipts"], strict=True):
        assert projected["name"] == raw["name"]
        assert projected["arguments_sha256"] == raw["arguments_sha256"]
        assert [
            restored[index]["ref"] if isinstance(index, int) else index
            for index in projected["refs"]
        ] == [codec.ref(ref) for ref in raw["refs"]]
        if "retrieval" in raw["data"]:
            assert projected["data"]["retrieval"]["proof_sha256"] == digest(
                raw["data"]["retrieval"]
            )
    return {
        "claims": len(claims),
        "records": len(restored),
        "customer_statements": len(messages),
        "original_receipts": len(receipts),
        "decoded_business_evidence_matches": True,
    }


def audit(path):
    content = path.read_bytes()
    report = json.loads(content)
    events = report["paid_usage"]["events"]
    scope = next(row["payload"]["scope"] for row in events if row["kind"] == "decision_input")
    value = build_input(
        DecisionScope.model_validate(scope),
        evidence_projection=HUMAN_PAYMENT_V7.evidence_projection,
    )
    state = archived_state(events, value)
    equivalence = compare_business_evidence(value, state)

    amount_case = value.order_id == "O9961"
    role, ref, tool = (
        ("fact", "L01", "read_order_lines")
        if amount_case
        else ("policy", "POL06:1.0", "read_policy_clause")
    )
    target = next(
        row["id"]
        for row in _all_claims(state)
        if row["role"] == role and ref in row["supporting_refs"]
    )
    capture = PreflightCapture(target, ref, tool)
    tools = InvestigationTools(value)
    context = InvestigationContext(
        value,
        SimpleNamespace(path="MULTI"),
        capture,
        capture,
        tools,
        None,
        "routed",
        protocol=HUMAN_PAYMENT_V7,
        output_reserve=4096,
    )
    runtime = SimpleNamespace(context=context)
    _verify(state, runtime)
    arguments = (
        {"line_ids": [ref]}
        if amount_case
        else {
            "policy_id": "POL06",
            "version": "1.0",
            "product_id": value.policy_context["products"][0]["id"],
        }
    )
    receipt = tools.call(role, tool, arguments).model_dump(mode="json")
    _verify({**state, "recheck_count": 1, "recheck_receipt": receipt}, runtime)
    budgets = [
        {**row, "remaining_input_estimate": row["limit"] - row["estimated_with_tools"]}
        for kind, row in capture.events
        if kind == "investigation_prompt_budget"
    ]
    assert [(row["phase_id"], row["round"]) for row in budgets] == [
        ("verify-0", 0),
        ("verify-0", 1),
        ("verify-1", 0),
    ]
    assert all(row["preflight"] == "FIT" for row in budgets)
    return {
        "order_id": value.order_id,
        "source_report": path.name,
        "source_report_sha256": sha256(content).hexdigest(),
        "frozen_input_digest": value.relevant_input_sha256,
        "business_evidence_roundtrip": equivalence,
        "prompt_budgets": budgets,
        "minimum_remaining_input_estimate": min(row["remaining_input_estimate"] for row in budgets),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    result = {
        "evidence_mode": "OFFLINE_ARCHIVED_FROZEN_SCOPE_PREFLIGHT",
        "scope": "Archived real upstream claims and receipts; synthetic verifier request; actual local frozen tool read.",
        "model_api_calls": 0,
        "database_connections": 0,
        "audits": [audit(path) for path in args.reports],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
