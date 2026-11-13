#!/usr/bin/env python3
"""Freeze and run a one-trial, matched official τ²-bench retail campaign."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from decimal import Decimal
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tau_official_budget import AGENT, PRICES, REVIEW, USER, Budget, RowTransport
from tau_official_summary import ARCHITECTURES, read_json, row_key, summarize, write_json, call_receipts
from tau_official_summary import combined_summary, write_repair_comparison

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
SOURCE = ROOT / ".runtime/tau-official-source"
REVISION = "da0835f6c67baef58bdda18f2a3b037dfb3a1754"
PUBLIC = ROOT / "evidence/tau_official/latest.json"
ACTIVE_ROW: ContextVar[RowTransport] = ContextVar("active_tau_row")
CALL_NAME: ContextVar[str | None] = ContextVar("tau_call_name", default=None)


def load_official_source():
    revision = subprocess.check_output(["git", "-C", SOURCE, "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise SystemExit("Official checkout differs from the pinned revision")
    dirty = subprocess.check_output(
        ["git", "-C", SOURCE, "status", "--porcelain", "--untracked-files=no"], text=True,
    )
    if dirty:
        raise SystemExit("Official checkout contains tracked changes")
    provenance = read_json(ROOT / "data/tau/manifest.json")
    retail = SOURCE / "data/tau2/domains/retail"
    hashes = {}
    for filename in ("db.json", "tasks.json", "split_tasks.json", "policy.md"):
        hashes[filename] = sha256((retail / filename).read_bytes()).hexdigest()
        if hashes[filename] != provenance["files"][filename]["sha256"]:
            raise SystemExit(f"Official retail hash mismatch for {filename}")
    os.environ["TAU2_DATA_DIR"] = str(SOURCE / "data")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    sys.path.insert(0, str(SOURCE / "src"))
    return hashes, read_json(retail / "split_tasks.json")["test"]


def train_task_ids():
    return read_json(SOURCE / "data/tau2/domains/retail/split_tasks.json")["train"]


def initialize_official():
    from loguru import logger
    from tau2.config import DEFAULT_LLM_NL_ASSERTIONS
    from tau2.registry import registry
    from tau2.utils import llm_utils

    from tau_official_agents import create_agent

    if DEFAULT_LLM_NL_ASSERTIONS != REVIEW:
        raise SystemExit("Pinned evaluator model differs from the budgeted review model")
    logger.disable("tau2")

    def factory(architecture, tools, domain_policy, llm, llm_args, **_unused):
        # Registry also supplies the full task to factories. Do not forward it.
        return create_agent(architecture, tools, domain_policy, llm, llm_args)

    from aftercare.agent_kernel.contracts import architectures as kernel_architectures
    for architecture in (*ARCHITECTURES, *kernel_architectures()):
        registry.register_agent_factory(partial(factory, architecture), architecture)

    def completion(**kwargs):
        return ACTIVE_ROW.get().completion(call_name=CALL_NAME.get(), **kwargs)

    original_generate_once = llm_utils._generate_once

    def generate_once(*args, call_name=None, **kwargs):
        token = CALL_NAME.set(call_name)
        try:
            return original_generate_once(*args, call_name=call_name, **kwargs)
        finally:
            CALL_NAME.reset(token)

    llm_utils.completion = completion
    llm_utils._generate_once = generate_once
    llm_utils.get_response_cost = lambda response: response._hidden_params["aftercare_cost_usd"]


def run_row(item, task, limits, budget, client, directory, deepseek_client=None, split="test"):
    from tau2.data_model.simulation import TextRunConfig
    from tau2.runner import build_text_orchestrator, run_simulation

    key = row_key(item)
    row_path = directory / "rows" / f"{key}.json"
    row_transport = RowTransport(key, limits, budget, client, deepseek_client)
    row = {
        **item, "status": "running", "official_reward": None, "official_components": {},
        "duration_seconds": None, **row_transport.metrics(),
    }
    write_json(row_path, row)
    start = time.monotonic()
    context = ACTIVE_ROW.set(row_transport)
    orchestrator = None
    try:
        config = TextRunConfig(
            domain="retail", task_split_name=split, agent=item["architecture"],
            user="user_simulator", llm_agent=AGENT, llm_user=USER,
            llm_args_agent={"reasoning_effort": "low"}, llm_args_user={"reasoning_effort": "low"},
            timeout=limits["timeout_seconds_per_run"], max_steps=limits["max_steps"],
            auto_review=False,
        )
        orchestrator = build_text_orchestrator(config, task, seed=item["seed"])
        simulation = run_simulation(orchestrator)
        raw = simulation.model_dump(mode="json")
        raw["private_trace"] = getattr(orchestrator.agent, "private_trace", [])
        write_json(directory / "official" / f"{key}.json", raw)
        row["termination_reason"] = raw["termination_reason"]
        row["status"] = (
            row_transport.failure_status
            or {"timeout": "timeout", "infrastructure_error": "error",
                "unexpected_error": "error", "agent_error": "error",
                "user_error": "error", "context_window_exceeded": "error"}
            .get(raw["termination_reason"], "completed")
        )
        # Keep upstream's actual reward, including its zero for early termination.
        reward = raw["reward_info"]
        row["official_reward"] = reward["reward"]
        row["official_components"] = reward["reward_breakdown"] or {}
    except Exception as error:
        row["status"] = row_transport.failure_status or "error"
        row["error_type"] = type(error).__name__
        if orchestrator is not None:
            write_json(directory / "official" / f"{key}.json", {
                "status": row["status"],
                "private_trace": getattr(orchestrator.agent, "private_trace", []),
                "messages": [message.model_dump(mode="json") for message in orchestrator.get_trajectory()],
            })
    finally:
        ACTIVE_ROW.reset(context)
    row.update(row_transport.metrics(), duration_seconds=round(time.monotonic() - start, 3))
    write_json(row_path, row)
    print(f"{key} {row['status']} reward={row['official_reward']}", flush=True)


def new_campaign(args):
    hashes, task_ids = load_official_source()
    initialize_official()

    from tau2.runner import build_environment, get_tasks

    if len(task_ids) != 40 or len(set(task_ids)) != 40:
        raise SystemExit("Unexpected official retail test population")
    split = "test"
    names = tuple(args.architectures.split(",")) if args.architectures else ARCHITECTURES
    from aftercare.agent_kernel.contracts import architectures as kernel_architectures
    if not set(names) <= {*ARCHITECTURES, *kernel_architectures()} or "S0" in names:
        raise SystemExit("Unknown or historical-only architecture")
    if args.scope == "wiring":
        # Connection check only: training tasks, never scored against the test population.
        split = "train"
        task_ids = args.task_ids.split(",")
        if not task_ids or not set(task_ids) <= set(train_task_ids()):
            raise SystemExit("Wiring checks use training tasks only")
    elif args.task_ids:
        raise SystemExit("Task selection is only available for wiring checks")
    else:
        task_ids = task_ids[:1] if args.scope == "smoke" else task_ids
    tasks = {task.id: task for task in get_tasks("retail", task_split_name=split, task_ids=task_ids)}
    raw_tasks = {
        task["id"]: task for task in
        read_json(SOURCE / "data/tau2/domains/retail/tasks.json") if task["id"] in tasks
    }
    limits = {
        "max_agent_calls_per_run": args.agent_calls, "max_user_calls_per_run": args.user_calls,
        "max_review_calls_per_run": 1, "max_output_tokens_per_call": args.max_output_tokens,
        "max_steps": 100, "timeout_seconds_per_run": args.run_timeout,
        "timeout_seconds_per_request": args.request_timeout,
        "max_calls": args.max_calls, "max_usd": float(args.max_usd),
    }
    schedule = []
    for i, task_id in enumerate(task_ids):
        description = raw_tasks[task_id].get("description") or {}
        # Never publish the simulator's private user instructions.
        public_description = description.get("purpose")
        for offset in range(len(names)):
            architecture = names[(i + offset) % len(names)]
            schedule.append({"task_id": task_id, "architecture": architecture, "seed": 42 + i,
                             "task_summary": public_description[:500] if public_description else None})
    manifest = {
        "schema_version": 1, "benchmark": "tau2-bench", "domain": "retail", "split": split,
        "architectures": list(names),
        "kernel_sha256": {path.name: sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT / "backend/src/aftercare/agent_kernel").glob("*.py"))},
        "scope": args.scope, "revision": REVISION, "task_ids": task_ids, "trials_per_task": 1,
        "agent_model": AGENT, "user_model": USER, "review_model": REVIEW,
        "tool_names": sorted(tool.name for tool in build_environment("retail").get_tools()),
        "budget": limits, "dataset_sha256": hashes, "schedule": schedule,
        "evaluation_type": "ALL", "reasoning_effort": "low",
        "agent_adapter_sha256": sha256((ROOT / "scripts/tau_official_agents.py").read_bytes()).hexdigest(),
        "pricing_usd_per_million": PRICES,
        "pricing_sources": ["https://developers.openai.com/api/docs/pricing",
                            "https://developers.openai.com/api/docs/models/gpt-5.4-mini"],
        "price_order": ["input", "output", "cached_input"],
        "cache_mode": "explicit with no breakpoints for gpt-6.1 models",
        "seed_scope": "official orchestrator; Responses does not expose a provider seed",
        "concurrency": args.concurrency,
    }
    execute_campaign(args.directory, manifest, tasks, args.live, args.max_usd)


def execute_campaign(directory, manifest, tasks, live, max_usd):
    if not live:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return

    from openai import OpenAI

    api_key = os.getenv("AFTERCARE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        api_key = read_json(ROOT / ".runtime/model-secret.json")["api_key"]
    limits = manifest["budget"]
    schedule = manifest["schedule"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("rows", "official"):
        (directory / name).mkdir(mode=0o700)
    write_json(directory / "manifest.json", manifest)
    budget = Budget(limits["max_calls"], max_usd, directory / "calls.jsonl")
    deepseek = None
    if {"S1c", "M2"} & {item["architecture"] for item in schedule}:
        deepseek = OpenAI(
            api_key=os.getenv("AFTERCARE_DEEPSEEK_KEY")
            or read_json(ROOT / ".runtime/deepseek-secret.json")["api_key"],
            base_url="https://api.deepseek.com", max_retries=0,
            timeout=limits["timeout_seconds_per_request"],
        )
    with OpenAI(
        api_key=api_key, base_url="https://api.openai.com/v1",
        max_retries=0, timeout=limits["timeout_seconds_per_request"],
    ) as client, ThreadPoolExecutor(max_workers=manifest["concurrency"]) as pool:
        futures = [
            pool.submit(run_row, item, tasks[item["task_id"]].model_copy(deep=True),
                        limits, budget, client, directory, deepseek, manifest["split"])
            for item in schedule
        ]
        for future in futures:
            future.result()
    if deepseek is not None:
        deepseek.close()
    if set(manifest.get("architectures", ARCHITECTURES)) <= set(ARCHITECTURES):
        print_summary(directory, publish=False)


def select_repairs(source, manifest):
    schedule = []
    original_attempts = {}
    for item in manifest["schedule"]:
        key = row_key(item)
        path = source / "rows" / f"{key}.json"
        if not path.is_file():
            raise SystemExit("Original campaign has unfinished rows")
        row = read_json(path)
        if row["status"] in {"running", "not_run"}:
            raise SystemExit("Original campaign has unfinished rows")
        if row["status"] not in {"unknown", "error"} or row["official_reward"] is not None:
            continue
        if any(row[field] != item[field] for field in ("architecture", "task_id", "seed")):
            raise SystemExit("Original row does not match the frozen schedule")
        schedule.append(item)
        original_attempts[key] = {
            "status": row["status"], "official_reward": None, "error_type": row.get("error_type"),
            "row_sha256": sha256(path.read_bytes()).hexdigest(),
        }
    return schedule, original_attempts


def remaining_campaign_budget(source, original):
    """Unknown calls keep their reservation; repairs share the original total ceiling."""
    journal = source / "calls.jsonl"
    if not journal.exists() or (journal.read_bytes() and not journal.read_bytes().endswith(b"\n")):
        raise SystemExit("Original billing journal is missing or incomplete")
    reservations, settlements = call_receipts(journal)
    if set(settlements) - set(reservations):
        raise SystemExit("Billing settlement has no reservation")
    committed = Decimal(0)
    for ident, event in reservations.items():
        reserve = Decimal(event["reserve_usd"])
        actual = Decimal(settlements[ident]["actual_usd"]) if ident in settlements else reserve
        if actual > reserve or actual < 0:
            raise SystemExit("Original accounting bound requires reconciliation")
        committed += actual
    return max(Decimal(0), Decimal(str(original["budget"]["max_usd"])) - committed)


def repair_campaign(args):
    source = args.source.resolve()
    original = read_json(source / "manifest.json")
    if original.get("repair_of") or original.get("schema_version") != 1:
        raise SystemExit("Repair requires an original official campaign")
    schedule, original_attempts = select_repairs(source, original)
    if not schedule:
        raise SystemExit("No unscored technical failures are eligible for repair")
    hashes, official_ids = load_official_source()
    checks = {
        "benchmark": "tau2-bench", "domain": "retail", "split": "test", "revision": REVISION,
        "dataset_sha256": hashes, "agent_model": AGENT, "user_model": USER, "review_model": REVIEW,
        "evaluation_type": "ALL", "reasoning_effort": "low",
        "agent_adapter_sha256": sha256((ROOT / "scripts/tau_official_agents.py").read_bytes()).hexdigest(),
        "pricing_usd_per_million": {model: list(prices) for model, prices in PRICES.items()},
    }
    if any(original.get(key) != value for key, value in checks.items()):
        raise SystemExit("Current runner does not match the frozen benchmark configuration")
    directory = source.with_name(f"{source.name}.repair")
    if directory.exists():
        raise SystemExit("A repair directory already exists for this original campaign")
    task_ids = list(dict.fromkeys(item["task_id"] for item in schedule))
    if not set(task_ids) <= set(official_ids):
        raise SystemExit("Repair selected an unofficial task")
    if original.get("kernel_sha256") != {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "backend/src/aftercare/agent_kernel").glob("*.py"))
    }:
        raise SystemExit("Kernel differs from original campaign")
    remaining = remaining_campaign_budget(source, original)
    if args.max_usd > remaining:
        raise SystemExit("Repair budget exceeds remaining original campaign ceiling")
    limits = dict(original["budget"])
    limits["max_calls"] = len(schedule) * sum(limits[f"max_{role}_calls_per_run"]
                                               for role in ("agent", "user", "review"))
    limits["max_usd"] = float(args.max_usd)
    manifest = {
        **original, "scope": "repair", "task_ids": task_ids, "schedule": schedule,
        "budget": limits, "concurrency": 1, "original_attempts": original_attempts,
        "repair_of": {
            "directory": source.name,
            "manifest_sha256": sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "budget": original["budget"],
        },
    }
    initialize_official()
    from tau2.runner import get_tasks

    tasks = {task.id: task for task in get_tasks("retail", task_split_name="test", task_ids=task_ids)}
    execute_campaign(directory, manifest, tasks, args.live, args.max_usd)
    if args.live and set(original.get("architectures", ARCHITECTURES)) <= set(ARCHITECTURES):
        write_repair_comparison(source, directory)


def print_summary(directory, publish, *, mark_interrupted=False, include_repair=False):
    summary = (
        combined_summary(directory) if include_repair
        else summarize(directory, mark_interrupted=mark_interrupted)
    )
    output = "summary-with-repair.json" if include_repair else "summary.json"
    write_json(directory / output, summary)
    if publish:
        if summary.get("repair_of"):
            raise SystemExit("Publish the original campaign; the technical repair has a separate comparison")
        PUBLIC.parent.mkdir(parents=True, exist_ok=True)
        if include_repair:
            source = directory.resolve()
            repair = source.with_name(f"{source.name}.repair")
            write_json(PUBLIC.with_name("primary.json"), summarize(source))
            write_json(PUBLIC.with_name("repair.json"), summarize(repair))
        write_json(PUBLIC, summary)
    print(json.dumps(summary["architectures"], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run")
    run.add_argument("--scope", choices=("smoke", "full", "wiring"), required=True)
    run.add_argument("--architectures", help="comma-separated; default is the first-round three")
    run.add_argument("--task-ids", help="comma-separated training task ids (wiring scope only)")
    run.add_argument("--directory", type=Path, required=True)
    run.add_argument("--max-usd", type=Decimal, required=True)
    run.add_argument("--max-calls", type=int, required=True)
    run.add_argument("--agent-calls", type=int, default=24)
    run.add_argument("--user-calls", type=int, default=24)
    run.add_argument("--max-output-tokens", type=int, default=1500)
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--run-timeout", type=int, default=900)
    run.add_argument("--request-timeout", type=int, default=120)
    run.add_argument("--live", action="store_true")
    repair = actions.add_parser("repair", help="retry unscored technical failures once in a sibling directory")
    repair.add_argument("source", type=Path)
    repair.add_argument("--max-usd", type=Decimal, required=True)
    repair.add_argument("--live", action="store_true")
    report = actions.add_parser("summarize")
    report.add_argument("directory", type=Path)
    report.add_argument("--publish", action="store_true")
    report.add_argument("--include-repair", action="store_true")
    report.add_argument("--mark-interrupted", action="store_true",
                        help="report rows left running by a confirmed stopped process as unknown")
    args = parser.parse_args()
    if args.action == "summarize":
        if args.include_repair and args.mark_interrupted:
            parser.error("combine only fully terminated campaigns")
        print_summary(args.directory, args.publish, mark_interrupted=args.mark_interrupted,
                      include_repair=args.include_repair)
        return
    if not args.max_usd.is_finite() or args.max_usd <= 0:
        parser.error("cost limit must be positive")
    if args.action == "repair":
        if args.max_usd > 3:
            parser.error("repair cost limit cannot exceed $3")
        repair_campaign(args)
        return
    if min(
        args.max_calls, args.agent_calls, args.user_calls, args.max_output_tokens,
        args.concurrency, args.run_timeout, args.request_timeout,
    ) < 1:
        parser.error("all budget and runtime limits must be positive")
    new_campaign(args)


if __name__ == "__main__":
    main()
