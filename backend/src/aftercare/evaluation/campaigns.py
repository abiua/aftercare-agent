"""Tester-only readback of the published official retail benchmark report."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from aftercare.business.errors import BusinessError

from .investigations import authorize

Architecture = Literal["strong_single", "staged_single", "multi_agent"]
Cost = float | str | None
Status = Literal["completed", "error", "timeout", "budget_exhausted", "unknown", "running", "not_run"]
REPORT_PATH = Path("evidence/tau_official/latest.json")
REPORTS = {
    "tau-official": REPORT_PATH,
    "tau-official-primary": REPORT_PATH.with_name("primary.json"),
    "tau-official-repair": REPORT_PATH.with_name("repair.json"),
}


class Usage(BaseModel):
    agent_calls: int
    user_calls: int
    review_calls: int
    agent_cost_usd: Cost
    user_cost_usd: Cost
    review_cost_usd: Cost
    duration_seconds: float | None = None


class Attempt(Usage):
    status: Status
    official_reward: float | None


class Row(Attempt):
    task_id: str
    architecture: Architecture
    seed: int
    task_summary: str | None = None
    attempt_count: Literal[1, 2] | None = None
    score_source: Literal["original", "technical_retry", "unscored"] | None = None
    initial_status: Status | None = None
    original_attempt: Attempt | None = None
    repair_attempt: Attempt | None = None


class Group(Usage):
    architecture: Architecture
    scheduled: int
    completed: int
    passed: int
    failed: int
    unscored: int
    pass_rate: float | None
    mean_official_reward: float | None


class Budget(BaseModel):
    max_agent_calls_per_run: int | None = None
    max_user_calls_per_run: int | None = None
    max_review_calls_per_run: int | None = None
    max_output_tokens_per_call: int | None = None
    max_steps: int | None = None
    timeout_seconds_per_run: float | None = None
    max_calls: int | None = None
    max_usd: Cost = None


class Billing(BaseModel):
    known_usd: Cost
    unsettled_calls: int
    unsettled_reserved_usd: Cost


class Campaign(BaseModel):
    schema_version: Literal[1]
    benchmark: Literal["tau2-bench"]
    domain: Literal["retail"]
    split: Literal["test"]
    scope: Literal["smoke", "full", "repair"]
    revision: str
    agent_model: str
    user_model: str
    review_model: str | None = None
    task_ids: list[str]
    trials_per_task: Literal[1]
    complete: bool
    all_scored: bool | None = None
    architectures: list[Group]
    original_directory: str | None = None
    repair_directory: str | None = None
    original_architectures: list[Group] | None = None
    rows: list[Row]
    tool_names: list[str] = []
    budget: Budget | None = None
    billing: Billing | None = None


def _read(root: Path, name: str, relative_path: Path):
    path = root / relative_path
    if path.is_symlink() or path.resolve() != root.resolve() / relative_path or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        report = Campaign.model_validate(raw)
    except (OSError, ValueError):
        raise BusinessError("CAMPAIGN_INVALID", "官方评测报告暂时无法读取。", 503) from None
    return {"id": name, **report.model_dump()}


def campaigns(root: Path, user):
    authorize(user)
    report = _read(root, "tau-official", REPORT_PATH)
    return {"items": [report] if report else []}


def read_campaign(root: Path, user, name: str):
    authorize(user)
    relative_path = REPORTS.get(name)
    if relative_path is not None:
        report = _read(root, name, relative_path)
        if report is not None:
            return report
    raise BusinessError("CAMPAIGN_NOT_FOUND", "没有找到这组官方评测。", 404)
