"""Execute a tester's frozen Retail batch. Without --live only validate the plan."""

import argparse
import os
import time
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path

import run_tau_official_campaign as official
from build_campaign_dashboard import batch
from tau_official_budget import AGENT, PRICES, REVIEW, USER
from tau_official_summary import call_receipts, read_json, write_json


def prepare(directory, host):
    plan = read_json(directory / "plan.json")
    for name, digest in plan["hashes"].items():
        if sha256((directory / "source" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Snapshot changed")
    official.SOURCE = host / ".runtime/tau-official-source"
    hashes, population = official.load_official_source()
    if plan["revision"] != official.REVISION or not set(plan["task_ids"]) <= set(population):
        raise ValueError("Dataset changed")
    # The pinned LiteLLM wheel bundles the o200k vocabulary used for budget reservations.
    tokenizers = distribution("litellm").locate_file("litellm/litellm_core_utils/tokenizers")
    if tokenizers.is_dir():
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(tokenizers))
    official.initialize_official()
    from aftercare.agent_kernel.contracts import VERSION, architectures
    from tau2.runner import get_tasks

    arms = plan["architectures"]
    selected = set(plan["task_ids"])
    tasks = {
        t.id: t for t in get_tasks("retail", task_split_name="test", task_ids=plan["task_ids"])
    }
    if set(tasks) != selected:
        raise ValueError("Missing scheduled tasks")
    schedule = []
    for index, task_id in enumerate(population):
        if task_id in selected:
            for offset in range(len(arms)):
                schedule.append(
                    {
                        "task_id": task_id,
                        "architecture": arms[(index + offset) % len(arms)],
                        "seed": 42 + index,
                    }
                )
    return {
        "schema_version": 1,
        "scope": "workbench",
        "benchmark": "tau2-bench",
        "domain": "retail",
        "split": "test",
        "revision": official.REVISION,
        "dataset_sha256": hashes,
        "task_ids": plan["task_ids"],
        "trials_per_task": 1,
        "schedule": schedule,
        "architectures": arms,
        "concurrency": 1,
        "source_commit": plan["commit"],
        "working_tree_patch": plan["patched"],
        "kernel_version": VERSION,
        "source_snapshot_sha256": plan["hashes"],
        "agent_adapter_sha256": plan["hashes"]["scripts/tau_official_agents.py"],
        "assignments": {k: architectures()[k].model_dump(mode="json") for k in arms},
        "agent_model": AGENT,
        "user_model": USER,
        "review_model": REVIEW,
        "pricing_usd_per_million": PRICES,
        "budget": {
            "max_usd": plan["max_usd"],
            "max_calls": plan["max_calls"],
            "max_agent_calls_per_run": 24,
            "max_user_calls_per_run": 24,
            "max_review_calls_per_run": 1,
            "max_output_tokens_per_call": 1500,
            "max_steps": 100,
            "timeout_seconds_per_run": 900,
            "timeout_seconds_per_request": 120,
        },
    }, tasks


def finish(directory):
    run = directory / "run"
    result = batch(
        directory.name,
        f"自选评测 · {directory.name[:8]}",
        run,
        "自选 Retail 测试题，每个架构每题一次。成功率分母包含本批全部计划题目。",
    )
    counts = Counter(read_json(p)["status"] for p in (run / "rows").glob("*.json"))
    total = sum(g["scheduled"] for g in result["groups"])
    state = "completed" if counts["completed"] == total else "partial"
    reserved, settled = call_receipts(run / "calls.jsonl")
    return {
        "state": state,
        "result": result,
        "counts": counts,
        "billing": {
            "calls": len(reserved),
            "unsettled": len(reserved.keys() - settled.keys()),
            "known_usd": str(sum((Decimal(r["actual_usd"]) for r in settled.values()), Decimal(0))),
        },
    }


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("directory", type=Path)
    args.add_argument("host", type=Path)
    args.add_argument("--live", action="store_true")
    opts = args.parse_args()
    directory = opts.directory
    try:
        manifest, tasks = prepare(directory, opts.host)
        if not opts.live:
            print(f"Validated {len(manifest['schedule'])} runs without model calls.")
            return
        for env, filename in [
            ("AFTERCARE_OPENAI_KEY", "model-secret.json"),
            ("AFTERCARE_DEEPSEEK_KEY", "deepseek-secret.json"),
        ]:
            if env == "AFTERCARE_DEEPSEEK_KEY" and not {"S1c", "M2"}.intersection(
                manifest["architectures"]
            ):
                continue
            if not os.getenv(env) and not (
                env == "AFTERCARE_OPENAI_KEY" and os.getenv("OPENAI_API_KEY")
            ):
                os.environ[env] = read_json(opts.host / ".runtime" / filename)["api_key"]
        official.execute_campaign(
            directory / "run", manifest, tasks, True, Decimal(str(manifest["budget"]["max_usd"]))
        )
        outcome = finish(directory)
    except (Exception, SystemExit) as error:
        if not opts.live:
            raise
        message = f"评测未能完成（{type(error).__name__}），请检查本机批次日志。"
        print(message, flush=True)
        import traceback

        traceback.print_exc()
        outcome = {"state": "failed", "message": message}
        if (directory / "run/manifest.json").exists():
            try:
                outcome = {**finish(directory), **outcome}
            except (ValueError, OSError, KeyError):
                pass
    outcome["ended_at"] = time.time()
    write_json(directory / "outcome.json", outcome)


if __name__ == "__main__":
    main()
