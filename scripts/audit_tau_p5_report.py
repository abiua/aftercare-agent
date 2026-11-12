"""Audit saved P5 traces without contacting the model or the database."""

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

EXECUTOR_ROLES = {
    "read_order_lines": "fact",
    "read_material_evidence": "fact",
    "read_refund_execution": "fact",
    "read_policy_clause": "policy",
    "search_applicable_policy": "policy",
}


def receipt_digest(receipt):
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def audit_recheck(events, output, *, frozen_scope=None):
    recheck = next((row for row in events if row["kind"] == "investigation_recheck"), None)
    if recheck is None:
        return {
            "trace": "NO_RECHECK_EVENT",
            "targeted_requests": [
                row["receipt"]["data"]
                for row in events
                if row["kind"] == "investigation_receipt"
                and row["receipt"]["name"] == "request_targeted_check"
            ],
            "completed_assessments": sum(
                row["kind"] == "investigation_assessment" for row in events
            ),
        }
    index = events.index(recheck)
    before, after = events[:index], events[index + 1 :]
    prior = next((row for row in reversed(before) if row["kind"] == "investigation_assessment"), {})
    receipt_event = next(
        (
            row
            for row in reversed(before)
            if row["kind"] == "investigation_receipt"
            and row.get("phase_id") == "recheck"
            and row.get("receipt_sha256") == recheck["receipt_sha256"]
        ),
        {},
    )
    prior_reads = [
        row["receipt"]
        for row in before
        if row["kind"] == "investigation_receipt"
        and row["phase_id"] != "recheck"
        and row["receipt"]["name"] != "request_targeted_check"
        and receipt_digest(row["receipt"]) == row["receipt_sha256"]
    ]
    prior_digests = {row["input_digest"] for row in prior_reads}
    receipt = receipt_event.get("receipt", {})
    executor_role = recheck.get("executor_role", recheck["target_role"])
    repeated_query = any(
        previous["name"] == receipt.get("name")
        and previous["arguments_sha256"] == receipt.get("arguments_sha256")
        for previous in prior_reads
    )
    refs = {row["source_id"] for row in receipt.get("refs", [])}
    receipt_data = receipt.get("data", {})
    if recheck["tool"] == "read_refund_execution":
        has_record = bool(receipt_data.get("execution"))
    elif recheck["tool"] == "search_applicable_policy":
        has_record = any(row["source_kind"] == "POLICY" for row in receipt.get("refs", []))
    else:
        has_record = bool(receipt_data.get("records"))
    prior_ref_versions = {
        (ref["source_id"], ref["version_or_sha256"]) for row in prior_reads for ref in row["refs"]
    }
    receipt_ref_versions = {
        (ref["source_id"], ref["version_or_sha256"]) for ref in receipt.get("refs", [])
    }
    revision = next((row for row in after if row["kind"] == "investigation_claim_revision"), {})
    final = next((row for row in reversed(after) if row["kind"] == "investigation_assessment"), {})
    initial_claims = {row["id"]: row for row in prior.get("claims", [])}
    targets = set(recheck["claim_ids"])
    affected = set(targets)
    while True:
        closure = affected | {
            row["id"]
            for row in initial_claims.values()
            if affected.intersection(row["depends_on_claims"])
        }
        if closure == affected:
            break
        affected = closure
    replacements = {row["id"]: row for row in revision.get("replacement_claims", [])}
    surviving = {row["id"]: row for row in final.get("claims", [])}
    candidates = output.get("investigation_options", [])
    scoped_line_ids = {
        row["source_id"] for row in receipt.get("refs", []) if row["source_kind"] == "ORDER_LINE"
    }
    relevant_candidates = [
        option
        for option in candidates
        if not scoped_line_ids
        or scoped_line_ids.intersection(option.get("target_line_ids", []))
        or scoped_line_ids.intersection(line["line_id"] for line in option.get("lines", []))
    ]
    checks = {
        "counterexample_in_initial_assessment": bool(targets)
        and all(
            any(
                item["claim_id"] == target
                and item["verdict"] != "CONFIRMED"
                and item.get("evidence_refs")
                and item in prior.get("assessments", [])
                for item in recheck["counterexamples"]
            )
            for target in targets
        ),
        "scoped_receipt_recorded": bool(refs)
        and has_record
        and refs == set(recheck["evidence_ids"])
        and receipt_event.get("role") == executor_role == EXECUTOR_ROLES.get(recheck["tool"])
        and receipt.get("name") == recheck["tool"]
        and receipt_digest(receipt) == recheck["receipt_sha256"]
        and receipt["name"] != "request_targeted_check",
        "dependent_claims_retired": targets <= initial_claims.keys()
        and all(initial_claims[key]["role"] == recheck["target_role"] for key in targets)
        and affected == set(recheck["stale_claim_ids"])
        and sorted(recheck["retired_claims"], key=lambda row: row["id"])
        == [initial_claims[key] for key in sorted(affected)]
        and set(revision.get("unresolved_stale_claim_ids", [])) == affected - targets,
        "replacements_bound_to_receipt": targets == replacements.keys()
        and all(
            (old := initial_claims.get(key))
            and row["role"] == old["role"] == recheck["target_role"]
            and (row["subject"], row["predicate"]) == (old["subject"], old["predicate"])
            and (row["value"], row["state"]) != (old["value"], old["state"])
            and refs.intersection(row["supporting_refs"] + row["conflicting_refs"])
            for key, row in replacements.items()
        ),
        "final_assessment_excludes_retired_dependencies": bool(final)
        and not (affected - targets).intersection(surviving)
        and all(surviving.get(key) == row for key, row in replacements.items()),
        "candidate_cites_receipt_or_output_explains_handoff": (
            all(refs.intersection(option.get("evidence_ids", [])) for option in relevant_candidates)
            if relevant_candidates
            else bool(output.get("reason") and output.get("investigation_unresolved"))
        ),
    }
    if frozen_scope is not None:
        checks["receipt_matches_frozen_input_and_scope"] = (
            prior_digests == {receipt.get("input_digest")}
            and bool(receipt.get("input_digest"))
            and all(tuple(row["scope"]) == frozen_scope for row in receipt.get("refs", []))
        )
    return {
        "trace": "CHAIN_PRESENT" if all(checks.values()) else "CHAIN_INCOMPLETE",
        "checks": checks,
        "target_claim_ids": sorted(targets),
        "retired_claim_ids": sorted(affected),
        "claim_revisions": [
            {"claim_id": key, "before": initial_claims[key]["value"], "after": row["value"]}
            for key, row in sorted(replacements.items())
            if key in initial_claims
        ],
        "receipt_tool": recheck["tool"],
        "receipt_arguments_sha256": receipt.get("arguments_sha256"),
        "exact_prior_tool_query_repeated": repeated_query if prior_reads else None,
        "target_role": recheck["target_role"],
        "executor_role": executor_role,
        "receipt_refs": receipt.get("refs", []),
        "receipt_refs_with_new_versions": (
            sorted(receipt_ref_versions - prior_ref_versions) if prior_reads else None
        ),
    }


def audit_report(report):
    usage = report.get("paid_usage")
    if usage is None:
        return {
            "evidence_mode": report.get("evidence_mode", "NO_LIVE_LEDGER"),
            "correction": audit_recheck(
                report.get("serial_events", []), report.get("final", {}).get("output", {})
            ),
        }

    events = [
        {**row["payload"], "kind": row["kind"]}
        for row in sorted(usage["events"], key=lambda row: row["seq"])
    ]
    run = usage.get("run") or {}
    output = run.get("output") or {}
    result = next((row for row in reversed(events) if row["kind"] == "investigation_result"), {})
    timeout_calls = [
        {
            "role": row["role"],
            "elapsed_seconds": round(
                (
                    datetime.fromisoformat(row["finished_at"])
                    - datetime.fromisoformat(row["started_at"])
                ).total_seconds(),
                6,
            ),
        }
        for row in report.get("transport_intervals", [])
        if row["outcome"] == "OpenAITimeoutError"
    ]
    tool_reads = [
        {
            "role": row["role"],
            "phase": row["phase_id"],
            "origin": row.get("origin", "UNSPECIFIED"),
            "tool": row["receipt"]["name"],
            "source_ids": [ref["source_id"] for ref in row["receipt"]["refs"]],
            "receipt_digest_matches": receipt_digest(row["receipt"]) == row["receipt_sha256"],
        }
        for row in events
        if row["kind"] == "investigation_receipt"
        and row["receipt"]["name"] != "request_targeted_check"
    ]
    ledger = usage.get("attempts", [])
    failed_or_unpriced = [
        {
            "run_id": row["run_id"],
            "logical_id": row["logical_id"],
            "ordinal": row["ordinal"],
            "status": row["status"],
            "error_code": row["error_code"],
            "reserved_usd": row["metadata_json"]["reserved_cost_usd"],
        }
        for row in ledger
        if row["estimated_cost_usd"] is None
    ]
    known_usd = sum(
        (
            Decimal(row["estimated_cost_usd"])
            for row in ledger
            if row["estimated_cost_usd"] is not None
        ),
        Decimal(0),
    )
    reserved_usd = sum((Decimal(row["reserved_usd"]) for row in failed_or_unpriced), Decimal(0))
    return {
        "evidence_mode": report.get("evidence_mode", "LIVE_TRANSPORT_PROVENANCE_REQUIRES_REVIEW"),
        "run_status": run.get("status"),
        "run_reason": run.get("reason"),
        "projection_state": (usage.get("projection") or {}).get("state"),
        "execution_count": usage.get("executions"),
        "candidates": output.get("investigation_options", []),
        "candidate_program_checks": result.get("program_checks"),
        "cost_readback": {
            "total_attempts": len(ledger),
            "current_run_attempts": sum(row["run_id"] == run.get("id") for row in ledger),
            "known_estimated_usd": str(known_usd),
            "unpriced_reserved_usd": str(reserved_usd),
            "conservatively_booked_usd": str(known_usd + reserved_usd),
            "matches_reported_totals": (
                known_usd == Decimal(usage["estimated_cost_usd"])
                and reserved_usd == Decimal(usage["unreconciled_reserved_usd"])
                and known_usd + reserved_usd == Decimal(usage["conservative_booked_usd"])
                if all(
                    usage.get(field) is not None
                    for field in (
                        "estimated_cost_usd",
                        "unreconciled_reserved_usd",
                        "conservative_booked_usd",
                    )
                )
                else None
            ),
            "unpriced_attempts": failed_or_unpriced,
        },
        "tool_reads": tool_reads,
        "configured_sdk_timeout_seconds": report.get("model_scope", {}).get("timeout_seconds"),
        "timeout_calls": timeout_calls,
        "correction": audit_recheck(
            events,
            output,
            frozen_scope=tuple(
                report.get(field) for field in ("conversation_id", "issue_id", "case_id")
            ),
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    results = {str(path): audit_report(json.loads(path.read_text())) for path in args.reports}
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
