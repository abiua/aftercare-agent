"""Build the tester's aggregate-only Retail dashboard from existing offline receipts.

Run from any directory with the archived batches available. Reads no model keys,
makes no network requests and never edits a source report or trajectory.
"""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from hashlib import sha256
import json
from math import ceil
from pathlib import Path

from tau_official_summary import call_receipts, write_json

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
EVIDENCE = ROOT / "evidence/architecture_refactor"
OUTPUT = ROOT / "evidence/tau_official/dashboard.json"
TOKENS = ("input_tokens", "output_tokens", "cached_input_tokens")


def read(path):
    return json.loads(path.read_bytes())


def load_run(directory, traces=None, hashes=None):
    manifest = read(directory / "manifest.json")
    reservations, settled = call_receipts(directory / "calls.jsonl")
    if settled.keys() - reservations.keys():
        raise ValueError(f"Unmatched settlement in {directory}")
    calls = defaultdict(list)
    for call_id, reservation in reservations.items():
        calls[reservation["row"]].append(settled.get(call_id))
    result = {}
    for item in manifest["schedule"]:
        key = f"{item['task_id']}-{item['architecture']}"
        if key in result:
            raise ValueError(f"Duplicate scheduled task: {key}")
        row_path = directory / "rows" / (key + ".json")
        row = read(row_path) if row_path.exists() else {}
        if row and any(row[k] != item[k] for k in ("task_id", "architecture", "seed")):
            raise ValueError(f"Row identity mismatch: {key}")
        path = (traces or directory / "official") / (key + ".json")
        messages = None
        partial = False
        if path.exists():
            raw = path.read_bytes()
            if hashes is not None and sha256(raw).hexdigest() != hashes.get(key):
                raise ValueError(f"Trace digest mismatch: {key}")
            trace = json.loads(raw)
            partial = (
                "task_id" not in trace
                and "seed" not in trace
                and row.get("status") in {"error", "unknown", "timeout", "budget_exhausted"}
                and trace.get("status") == row["status"]
            )
            if not partial and (
                trace.get("task_id") != item["task_id"] or trace.get("seed") != item["seed"]
            ):
                raise ValueError(f"Trace identity mismatch: {key}")
            messages = trace.get("messages") or None
        result[key] = (item, row, calls.get(key, []), messages, partial)
    if calls.keys() - result.keys():
        raise ValueError("Ledger references an unscheduled task")
    return manifest, result


def summarize(arm, tasks):
    turns, latencies = [], []
    counts = Counter()
    cost = Decimal(0)
    for attempts in tasks:
        observed_turns, durations = [], []
        reward = None
        for _, row, receipts, messages, partial in attempts:
            counts["attempts"] += 1
            if row.get("official_reward") is not None:
                reward = row["official_reward"]
            if row.get("duration_seconds") is not None and row.get("status") not in (
                "running",
                "not_run",
            ):
                durations.append(row["duration_seconds"])
            if messages is not None:
                counts["traces"] += 1
                counts["partial"] += partial
                if not partial:
                    observed_turns.append(sum(m["role"] == "assistant" for m in messages))
                for m in messages:
                    if (
                        m["role"] == "tool"
                        and m.get("requestor") == "assistant"
                        and m.get("execution_status") == "executed"
                    ):
                        if type(m.get("error")) is bool:
                            counts["tools"] += 1
                            counts["errors"] += m["error"]
                        else:
                            counts["unknown_tools"] += 1
            reported = [row.get(k + "_calls") for k in ("agent", "user", "review")]
            if None in reported or len(receipts) != sum(reported):
                counts["ledger_gaps"] += 1
            counts["calls"] += len(receipts)
            for receipt in receipts:
                if receipt is None:
                    continue
                counts["priced"] += 1
                cost += Decimal(str(receipt["actual_usd"]))
                if all(type(receipt.get(k)) is int and receipt[k] >= 0 for k in TOKENS):
                    counts["metered"] += 1
                    counts.update({k: receipt[k] for k in TOKENS})
        counts["successes"] += reward == 1
        if len(observed_turns) == len(attempts):
            turns.append(sum(observed_turns))
        if len(durations) == len(attempts):
            latencies.append(sum(durations))
    latencies.sort()
    reliable_cost = counts["priced"] == counts["calls"] and not counts["ledger_gaps"]
    return dict(
        architecture=arm,
        scheduled=len(tasks),
        passed=counts["successes"],
        attempts=counts["attempts"],
        traces=counts["traces"],
        partial=counts["partial"],
        tools=counts["tools"],
        errors=counts["errors"],
        unknown_tools=counts["unknown_tools"],
        steps_n=len(turns),
        avg_steps=sum(turns) / len(turns) if turns else None,
        latency_n=len(latencies),
        p95_seconds=latencies[ceil(0.95 * len(latencies)) - 1] if latencies else None,
        calls=counts["calls"],
        priced=counts["priced"],
        metered=counts["metered"],
        ledger_gaps=counts["ledger_gaps"],
        **{k: counts[k] for k in TOKENS},
        cost_per_success=float(cost / counts["successes"])
        if reliable_cost and counts["successes"]
        else None,
    )


def batch(key, label, directory, note, *, registration=None, repair=False, archive=None):
    traces = hashes = None
    if archive:
        if key == "v32-s1c":
            audit = read(archive / "audit.json")
            traces, hashes = Path(audit["official_trace_directory"]), audit["trajectory_sha256"]
        else:
            index = read(archive / "archive_manifest.json")
            traces = Path(index["local_source_directory"]) / "run-v1/official"
            hashes = {
                Path(row["file"]).stem: row["sha256"]
                for row in index["local_official_trajectories"]
            }
    manifest, first = load_run(directory, traces, hashes)
    retries = {}
    if repair:
        second, retries = load_run(directory.with_name(directory.name + ".repair"))
        if (
            second["repair_of"]["manifest_sha256"]
            != sha256((directory / "manifest.json").read_bytes()).hexdigest()
        ):
            raise ValueError("Repair manifest does not match original")
        for name, (item, *_rest) in retries.items():
            if (
                name not in first
                or item["seed"] != first[name][0]["seed"]
                or first[name][1].get("official_reward") is not None
            ):
                raise ValueError(f"Ineligible repair: {name}")
    groups = defaultdict(list)
    for name, attempt in first.items():
        attempts = [attempt, retries[name]] if name in retries else [attempt]
        groups[attempt[0]["architecture"]].append(attempts)
    commit = (
        read(EVIDENCE / registration)["git_head"] if registration else manifest.get("source_commit")
    )
    sources = manifest.get("source_snapshot_sha256")
    snapshot = (
        sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if sources
        else None
    )
    return dict(
        id=key,
        label=label,
        note=note,
        revision=manifest["revision"],
        split=manifest["split"],
        tasks=len(manifest["task_ids"]),
        trials=manifest["trials_per_task"],
        commit=commit,
        kernel=manifest.get("kernel_version"),
        patched=bool(manifest.get("language_prompt_patch") or manifest.get("working_tree_patch")),
        adapter=manifest.get("agent_adapter_sha256"),
        snapshot=snapshot,
        repairs=len(retries),
        groups=[summarize(arm, tasks) for arm, tasks in groups.items()],
    )


def build():
    nov17 = EVIDENCE / "kernel_v3_2_test_20261117"
    nov19 = EVIDENCE / "kernel_v3_2_s1c_test_20261119"
    return [
        batch(
            "v32",
            "v3.2 双组回归 · 11 月 17 日",
            nov17 / "run-v1",
            "这 40 道测试题在开发时已接触。",
            archive=nov17,
        ),
        batch(
            "v32-s1c",
            "v3.2 弱单 Agent 补测 · 11 月 19 日",
            nov19 / "run-v1",
            "与双组回归使用相同的 40 道题，运行日期不同。",
            archive=nov19,
        ),
        batch(
            "e4",
            "E4 消融 · 11 月 17 日",
            RUNTIME / "kernel-e4/run-v1",
            "本批新运行两组。两个对照组沿用 E3，可切换至 E3 查看。",
            registration="preregistration_e4.json",
            repair=True,
        ),
        batch(
            "e3",
            "E3 确认实验 · 11 月 15 日",
            RUNTIME / "kernel-e3/run-v1",
            "训练集排除 7 道开发题后剩余 67 题。",
            registration="preregistration_e3.json",
            repair=True,
        ),
        batch(
            "e2",
            "E2 五组对照 · 11 月 13 日",
            RUNTIME / "kernel-e2-20261113",
            "每组使用相同的 40 道测试题。",
            registration="preregistration_v2_2.json",
            repair=True,
        ),
        batch(
            "original",
            "首轮三组对照 · 11 月 12 日",
            RUNTIME / "tau-official-full",
            "只记录了被测适配器的 SHA-256，项目提交未记录。",
            repair=True,
        ),
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    write_json(parser.parse_args().output, build())
