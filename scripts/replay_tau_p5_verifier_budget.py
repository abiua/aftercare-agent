"""Rebuild the P5 O9961 verifier budget from archived scope and branch outputs."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from aftercare.decisions.contracts import DecisionScope
from aftercare.intelligence.models import ModelFailure, _estimate_input
from aftercare.investigation.graph import _recheck, _verify
from aftercare.investigation.input import build_input
from aftercare.investigation.orchestration import InvestigationContext
from aftercare.investigation.protocol import protocol_for_binding
from aftercare.investigation.routing import route_input
from aftercare.investigation.tools import InvestigationTools
from langgraph.runtime import Runtime


def source_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def archived_state(events):
    state = {"path": "MULTI", "recheck_count": 0}
    for role in ("fact", "policy"):
        claims = next(
            event["payload"]["claims"]
            for event in events
            if event["kind"] == "investigation_claims" and event["payload"]["phase_id"] == role
        )
        receipts = [
            event["payload"]["receipt"]
            for event in events
            if event["kind"] == "investigation_receipt" and event["payload"]["phase_id"] == role
        ]
        refs = {ref["source_id"] for receipt in receipts for ref in receipt["refs"]}
        refs.update(
            ref for claim in claims for ref in claim["supporting_refs"] + claim["conflicting_refs"]
        )
        state[f"{role}_result"] = {
            "claims": [{**claim, "id": claim["id"].split("/", 1)[1]} for claim in claims],
            "evidence_ids": sorted(refs),
            "receipts": receipts,
        }
    return state


class Audit:
    def __init__(self):
        self.events = []

    def event(self, kind, payload):
        self.events.append({"kind": kind, "payload": payload})


class OfflineVerifier:
    def __init__(self, tool_call, target):
        self.tool_call = tool_call
        self.target = target
        self.calls = []

    def call(self, _audit, logical_id, *, messages, tools, validate, **_kwargs):
        phase = logical_id.split(":", 2)[-1]
        self.calls.append(
            {
                "phase": phase,
                "estimate": _estimate_input(messages, tools, vision=False)[0],
                "tools": [tool["function"]["name"] for tool in tools],
            }
        )
        if phase == "verify-0:0":
            return validate([self.tool_call], "")
        if phase == "verify-0:1":
            scope = json.loads(messages[0].content.rsplit("\n", 1)[-1])
            assert scope["citable_evidence_ids"]
            upstream = json.loads(messages[1].content)["upstream_claims"]
            ids = [row[upstream["columns"].index("id")] for row in upstream["rows"]]
            result = {
                "assessments": [
                    {
                        "claim_id": claim_id,
                        "contradicted_field": "line_balance",
                        "verdict": "CONTRADICTED"
                        if claim_id == self.target["id"]
                        else "UNRESOLVED",
                        "evidence_refs": ["L01"] if claim_id == self.target["id"] else [],
                    }
                    for claim_id in ids
                ],
                "recheck": {
                    "target_role": "policy",
                    "claim_ids": [self.target["id"]],
                    "tool_name": "read_order_lines",
                    "arguments": {"line_ids": ["L01"]},
                    "reason": self.tool_call["args"]["reason"],
                },
            }
            return validate([], json.dumps(result))
        if phase == "recheck:0":
            result = {
                "claims": [
                    {
                        "id": self.target["id"].split("/", 1)[1],
                        "subject": self.target["subject"],
                        "predicate": self.target["predicate"],
                        "state": "SUPPORTED",
                        "value": "已核对L01实付4900分、已退1900分、可用余额3000分，余额不代表退款已批准。",
                        "supporting_refs": ["L01"],
                    }
                ]
            }
            return validate([], json.dumps(result))
        raise ModelFailure("P5_OFFLINE_STOP_BEFORE_FINAL_PROVIDER_CALL")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("earlier_tool_archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text())
    earlier = json.loads(args.earlier_tool_archive.read_text())
    events = archive["paid_usage"]["events"]
    binding = next(event["payload"] for event in events if event["kind"] == "decision_input")
    scope = DecisionScope.model_validate(binding["scope"])
    protocol = protocol_for_binding(binding, scope)
    frozen = build_input(scope, evidence_projection=protocol.evidence_projection)
    state = archived_state(events)
    target = next(
        {**row, "id": f"policy/{row['id']}"}
        for row in state["policy_result"]["claims"]
        if row["id"] == "line_applicability"
    )
    historical_call = next(
        call
        for row in earlier["paid_usage"]["attempts"]
        for call in (row["result"] or {}).get("calls", [])
        if call["name"] == "request_targeted_check"
    )
    tool_call = {
        **historical_call,
        "args": {**historical_call["args"], "claim_id": target["id"]},
    }
    model, audit = OfflineVerifier(tool_call, target), Audit()
    context = InvestigationContext(
        frozen,
        route_input(frozen),
        model,
        audit,
        InvestigationTools(frozen),
        None,
        "analysis",
        frozenset(scope.line_ids),
        protocol=protocol,
        output_reserve=binding["investigation_output_reserve"],
    )
    runtime = Runtime(context=context)
    verification = _verify(state, runtime)
    recheck = (
        _recheck({**state, **verification}, runtime) if verification["path"] == "RECHECK" else {}
    )
    if recheck.get("path") == "MULTI":
        _verify({**state, **recheck}, runtime)

    old = [
        event["payload"]
        for event in events
        if event["kind"] == "investigation_prompt_budget" and event["payload"]["role"] == "verifier"
    ]
    budgets = [
        event["payload"] for event in audit.events if event["kind"] == "investigation_prompt_budget"
    ]
    repository = Path(__file__).resolve().parent.parent
    sources = [
        "backend/src/aftercare/investigation/graph.py",
        "backend/src/aftercare/investigation/orchestration.py",
        "backend/src/aftercare/investigation/prompt_evidence.py",
        "backend/src/aftercare/investigation/tools.py",
        "backend/src/aftercare/intelligence/models.py",
        "scripts/replay_tau_p5_verifier_budget.py",
    ]
    expected = ["verify-0:0", "verify-0:1", "recheck:0", "verify-1:0"]
    report = {
        "status": "FIT" if [call["phase"] for call in model.calls] == expected else "INCOMPLETE",
        "archive": str(args.archive),
        "archive_sha256": source_hash(args.archive),
        "earlier_tool_archive": str(args.earlier_tool_archive),
        "earlier_tool_archive_sha256": source_hash(args.earlier_tool_archive),
        "source_snapshot_sha256": frozen.source_snapshot_sha256,
        "relevant_input_sha256": frozen.relevant_input_sha256,
        "source_sha256": {name: source_hash(repository / name) for name in sources},
        "api_calls": 0,
        "old_actual": old,
        "offline_preflight": budgets,
        "model_boundary_observations": model.calls,
        "request_replay": {
            "archived_claim_id": historical_call["args"]["claim_id"],
            "substituted_claim_id": target["id"],
            "only_changed_field": "arguments.claim_id",
            "tool_result_source": "real frozen read-only tools",
        },
        "receipt_hashes": [
            event["payload"]["receipt_sha256"]
            for event in audit.events
            if event["kind"] == "investigation_receipt"
        ],
        "scripted": ["verifier recheck decision", "policy claim revision"],
    }
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"status": report["status"], "model_boundary_observations": model.calls}))
    return 0 if report["status"] == "FIT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
