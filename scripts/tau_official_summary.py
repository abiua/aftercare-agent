"""Aggregate the frozen denominator independently of how far a batch ran."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import os
import tempfile
from pathlib import Path

ARCHITECTURES = ("strong_single", "staged_single", "multi_agent")
ROLES = ("agent", "user", "review")


def write_json(path: Path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def row_key(item):
    return f"{item['task_id']}-{item['architecture']}"


def call_receipts(path):
    reservations = {}
    settlements = {}
    if not path.is_file():
        return reservations, settlements
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                # An interrupted append could leave a partial final line.
                break
            event = json.loads(line)
            call_id = event["call_id"]
            if "reserve_usd" in event:
                reservations[call_id] = event
            elif "actual_usd" in event:
                settlements[call_id] = event
    return reservations, settlements


def update_pending_costs(row, calls, settlements):
    for role in ROLES:
        role_ids = [call_id for call_id, call in calls if call["role"] == role]
        row[f"{role}_calls"] = len(role_ids)
        row[f"{role}_cost_usd"] = (
            float(sum(Decimal(settlements[call_id]["actual_usd"]) for call_id in role_ids))
            if all(call_id in settlements for call_id in role_ids) else None
        )
    return row


def summarize_trace(directory, row):
    if row["status"] in {"running", "not_run"}:
        return
    trace = directory / "official" / f"{row_key(row)}.json"
    messages = read_json(trace).get("messages", []) if trace.is_file() else []
    user_turns = [message.get("content") or "" for message in messages
                  if message.get("role") == "user"]
    signals = {
        "###OUT-OF-SCOPE###": "user_out_of_scope",
        "###STOP###": "user_stop",
        "###TRANSFER###": "user_transfer",
    }
    signal = next((label for marker, label in signals.items()
                   if user_turns and marker in user_turns[-1]), None)
    row["user_terminal_signal"] = signal
    if row["status"] == "completed" and not row.get("task_summary"):
        first_text = next((text for text in user_turns if text.strip() and text not in signals), "")
        row["task_summary"] = " ".join(first_text.split())[:500] or None
    reasons = []
    if row["status"] != "completed":
        reasons.append(row["status"])
    if row.get("termination_reason") in {"max_steps", "too_many_errors"}:
        reasons.append(row["termination_reason"])
    if row["official_reward"] is not None and row["official_reward"] < 1:
        if signal:
            reasons.append(signal)
        components = row.get("official_components", {})
        if components.get("DB", 1) < 1:
            reasons.append("db_mismatch")
        if components.get("NL_ASSERTION", 1) < 1:
            reasons.append("nl_assertion_failed")
        if not reasons:
            reasons.append("official_reward_below_one")
    row["failure_types"] = reasons


def summarize(directory, *, mark_interrupted=False):
    manifest = read_json(directory / "manifest.json")
    reservations, settlements = call_receipts(directory / "calls.jsonl")
    calls_by_row = {}
    for call_id, call in reservations.items():
        calls_by_row.setdefault(call["row"], []).append((call_id, call))
    unsettled = set(reservations) - set(settlements)
    actual_usd = sum(Decimal(event["actual_usd"]) for event in settlements.values())
    held_usd = sum(Decimal(reservations[call_id]["reserve_usd"]) for call_id in unsettled)
    rows = []
    unfinished = False
    for item in manifest["schedule"]:
        path = directory / "rows" / f"{row_key(item)}.json"
        row = read_json(path) if path.is_file() else {
            **item, "status": "not_run", "official_reward": None, "official_components": {},
            "duration_seconds": None, **{f"{role}_calls": 0 for role in ROLES},
            **{f"{role}_cost_usd": 0.0 for role in ROLES},
        }
        if row["status"] == "running":
            unfinished = True
            row = update_pending_costs(row, calls_by_row.get(row_key(item), []), settlements)
            if mark_interrupted:
                row.update(status="unknown", error_type="Interrupted")
        summarize_trace(directory, row)
        rows.append(row)
    return {
        **{key: value for key, value in manifest.items() if key != "schedule"},
        "complete": not unfinished and all(row["status"] != "not_run" for row in rows),
        "billing": {
            "attempted_calls": len(reservations), "settled_calls": len(settlements),
            "unsettled_calls": len(unsettled), "known_usd": float(actual_usd),
            "unsettled_reserved_usd": float(held_usd),
            "accounted_and_reserved_usd": float(actual_usd + held_usd),
        },
        "all_scored": all(row["official_reward"] is not None for row in rows),
        "architectures": aggregate_architectures(rows), "rows": rows,
    }


def aggregate_architectures(rows):
    groups = []
    for architecture in ARCHITECTURES:
        group = [row for row in rows if row["architecture"] == architecture]
        rewards = [row["official_reward"] for row in group if row["official_reward"] is not None]
        passed = sum(reward == 1 for reward in rewards)
        groups.append({
            "architecture": architecture, "scheduled": len(group),
            "completed": sum(row["status"] == "completed" for row in group),
            "passed": passed, "failed": len(rewards) - passed,
            "unscored": len(group) - len(rewards),
            "failure_types": dict(Counter(reason for row in group for reason in row.get("failure_types", []))),
            "pass_rate": passed / len(group) if group else None,
            "mean_official_reward": sum(rewards) / len(rewards) if rewards else None,
            "duration_seconds": round(sum(row["duration_seconds"] or 0 for row in group), 3),
            **{f"{role}_calls": sum(row[f"{role}_calls"] for row in group) for role in ROLES},
            **{f"{role}_cost_usd": (
                None if any(row[f"{role}_cost_usd"] is None for row in group)
                else round(sum(row[f"{role}_cost_usd"] for row in group), 8)
            ) for role in ROLES},
        })
    return groups


def write_repair_comparison(source, repair):
    original = summarize(source)
    retry = summarize(repair)
    retry_rows = {row_key(row): row for row in retry["rows"]}
    links = []
    groups = []
    for group in original["architectures"]:
        recovered = 0
        extra_passes = 0
        for row in original["rows"]:
            if row["architecture"] != group["architecture"]:
                continue
            replacement = retry_rows.get(row_key(row))
            if replacement is None:
                continue
            links.append({"task_id": row["task_id"], "architecture": row["architecture"],
                          "seed": row["seed"], "original_attempt": row, "repair_attempt": replacement})
            if row["official_reward"] is None and replacement["official_reward"] is not None:
                recovered += 1
                extra_passes += replacement["official_reward"] == 1
        groups.append({
            "architecture": group["architecture"], "scheduled": group["scheduled"],
            "original_passed": group["passed"], "original_unscored": group["unscored"],
            "recovered_scores": recovered, "passes_after_repair": group["passed"] + extra_passes,
            "unscored_after_repair": group["unscored"] - recovered,
            "pass_rate_after_repair": (group["passed"] + extra_passes) / group["scheduled"],
        })
    write_json(repair / "comparison.json", {
        "schema_version": 1, "original": source.name, "repair": repair.name,
        "original_billing": original["billing"], "repair_billing": retry["billing"],
        "architectures": groups, "linked_attempts": links,
    })


def combined_summary(source):
    from hashlib import sha256

    source = source.resolve()
    repair = source.with_name(f"{source.name}.repair")
    original = summarize(source)
    retry = summarize(repair)
    source_hash = sha256((source / "manifest.json").read_bytes()).hexdigest()
    if retry["repair_of"]["manifest_sha256"] != source_hash:
        raise ValueError("Repair references a different original manifest")
    retry_rows = {row_key(row): row for row in retry["rows"]}
    if not original["complete"] or not retry["complete"]:
        raise ValueError("Original and repair campaigns must both have terminated")

    rows = []
    for row in original["rows"]:
        key = row_key(row)
        attempt = retry_rows.pop(key, None)
        if attempt is None:
            rows.append({**row, "attempt_count": 1, "score_source": "original",
                         "initial_status": row["status"], "original_reward": row["official_reward"]})
            continue
        if row["status"] not in {"unknown", "error"} or row["official_reward"] is not None:
            raise ValueError(f"Repair includes an ineligible row: {key}")
        original_row_hash = sha256((source / "rows" / f"{key}.json").read_bytes()).hexdigest()
        if retry["original_attempts"][key]["row_sha256"] != original_row_hash:
            raise ValueError(f"Original attempt changed: {key}")
        score_recovered = attempt["official_reward"] is not None
        merged = {
            **(attempt if score_recovered else row),
            "attempt_count": 2,
            "initial_status": row["status"],
            "original_reward": row["official_reward"],
            "score_source": "technical_retry" if score_recovered else "unscored",
            "original_attempt": row,
            "repair_attempt": attempt,
            "duration_seconds": (row["duration_seconds"] or 0) + (attempt["duration_seconds"] or 0),
        }
        for role in ROLES:
            merged[f"{role}_calls"] = row[f"{role}_calls"] + attempt[f"{role}_calls"]
            costs = [row[f"{role}_cost_usd"], attempt[f"{role}_cost_usd"]]
            merged[f"{role}_cost_usd"] = None if None in costs else sum(costs)
        rows.append(merged)
    if retry_rows:
        raise ValueError("Repair includes rows outside the original campaign")
    billing = {key: original["billing"][key] + retry["billing"][key] for key in original["billing"]}
    budget = dict(original["budget"])
    for key in ("max_calls", "max_usd"):
        budget[key] += retry["budget"][key]
    return {
        **original,
        "budget": budget,
        "billing": billing,
        "original_billing": original["billing"],
        "repair_billing": retry["billing"],
        "original_architectures": original["architectures"],
        "original_directory": source.name,
        "repair_directory": repair.name,
        "architectures": aggregate_architectures(rows),
        "all_scored": all(row["official_reward"] is not None for row in rows),
        "rows": rows,
    }
