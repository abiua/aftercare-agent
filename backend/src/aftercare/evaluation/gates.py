"""Versioned release criteria and evidence-derived technical gate decisions."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .metrics import _gold

Ratio = Annotated[float, Field(ge=0, le=1)]
Usd = Annotated[float, Field(ge=0)]


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min_coverage: Ratio = 1
    min_task_success: Ratio
    min_independent_cases: Annotated[int, Field(ge=1, le=144)]
    min_human_gold: Annotated[float, Field(gt=0, le=1)] = 1
    max_total_cost_usd: Usd | None = None
    max_cost_per_success: Usd | None = None
    max_final_violation_rate: Ratio = 0
    require_live: bool = True
    baseline_task_success: Ratio | None = None
    max_success_regression: Ratio | None = None

    @model_validator(mode="after")
    def pair_baseline(self):
        if (self.baseline_task_success is None) != (self.max_success_regression is None):
            raise ValueError("Baseline and regression threshold must be configured together")
        return self


class AdditionalThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    operator: Literal["min", "max"]
    value: float
    require_complete_audit: bool = True


class GatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    source: Annotated[str, Field(min_length=1, max_length=400)]
    thresholds: Thresholds
    required_checks: Annotated[list[str], Field(max_length=80)] = Field(default_factory=list)
    additional_metrics: Annotated[dict[str, AdditionalThreshold], Field(max_length=25)] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def unique_checks(self):
        if len(self.required_checks) != len(set(self.required_checks)):
            raise ValueError("Duplicate required checks")
        return self


def read_policies(path: Path | None = None) -> list[GatePolicy]:
    if path is None:
        location = os.environ.get("AFTERCARE_EVALUATION_GATE_PATH")
        if not location:
            return []
        path = Path(location)
    with path.open("rb") as file:
        document = file.read(131_073)
    if len(document) > 131_072:
        raise ValueError("Gate catalogue is too large")
    rows = TypeAdapter(list[GatePolicy]).validate_json(document)
    if len(rows) > 30 or len({row.id for row in rows}) != len(rows):
        raise ValueError("Gate catalogue has duplicate IDs or too many policies")
    return rows


class GoldApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: Literal["v0.2"]
    case_id: Annotated[str, Field(pattern=r"^[A-Z0-9-]{1,64}$")]
    case_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reference_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reviewer_id: Annotated[str, Field(min_length=1, max_length=120)]
    reviewed_at: datetime
    source: Annotated[str, Field(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def offset_required(self):
        if (
            self.reviewed_at.utcoffset() is None
            or not self.reviewer_id.strip()
            or not self.source.strip()
        ):
            raise ValueError("Human approval requires a reviewer, source, and offset timestamp")
        return self


def read_gold_approvals(path: Path | None = None) -> dict[str, GoldApproval]:
    if path is None:
        location = os.environ.get("AFTERCARE_EVALUATION_GOLD_PATH")
        if not location:
            return {}
        path = Path(location)
    with path.open("rb") as file:
        document = file.read(262_145)
    if len(document) > 262_144:
        raise ValueError("Gold approvals catalogue is too large")
    rows = TypeAdapter(list[GoldApproval]).validate_json(document)
    mapping = {row.case_id: row for row in rows}
    if len(rows) > 144 or len(mapping) != len(rows):
        raise ValueError("Gold approvals catalogue has duplicate cases or too many entries")
    return mapping


def evaluate(policy_data, rows, counts, metrics, *, source, status):
    by_key = {metric["key"]: metric for metric in metrics}
    checks = []
    pending_quality = any(
        row["status"] in {"NEEDS_REVIEW", "RUNNING", "QUEUED", "NOT_RUN"} for row in rows
    )

    def check(name, result, reason):
        checks.append({"name": name, "status": result, "reason": reason})

    def bound(name, key, threshold, operator="min", *, complete=False):
        if threshold is None:
            return
        metric = by_key.get(key) or {}
        value = None if key == "task_success" and pending_quality else metric.get("value")
        if value is None or (complete and metric.get("unknown", 0)):
            check(name, "UNKNOWN", "缺少满足门槛的完整观察。")
            return
        passed = value >= threshold if operator == "min" else value <= threshold
        check(name, "PASS" if passed else "FAIL", f"观察值 {value}，阈值 {threshold}。")

    finished = (
        status == "COMPLETED"
        and bool(counts["planned"])
        and counts["planned"] == counts["finished"]
    )
    check(
        "用例执行覆盖",
        "PASS" if finished else "UNKNOWN",
        f"{counts['finished']} / {counts['planned']} 已结束",
    )
    machine_checks = [item for row in rows for item in row.get("checks", [])]
    severe = any(
        item.get("status") == "FAIL" and item.get("severity") == "critical"
        for item in machine_checks
    )
    proven_violation = any(
        (row.get("actual") or {}).get("financial_safety", {}).get("status") == "FAIL"
        for row in rows
    )
    check(
        "严重违规",
        "FAIL" if severe or proven_violation else "PASS" if machine_checks else "UNKNOWN",
        "按严重检查和有证据的最终违规判定。",
    )

    if policy_data is None:
        if any(item.get("status") == "FAIL" for item in machine_checks):
            check("确定性检查", "FAIL", "已记录规则检查失败。")
        check("发布质量及费用阈值", "UNKNOWN", "该实验未冻结门槛版本。")
    else:
        policy = GatePolicy.model_validate(policy_data)
        limits = policy.thresholds
        bound("计划覆盖率", "coverage", limits.min_coverage)
        bound("任务成功率", "task_success", limits.min_task_success)
        bound("估算总成本", "cost", limits.max_total_cost_usd, "max")
        bound("每成功案估算费用", "cost_per_success", limits.max_cost_per_success, "max")
        bound(
            "最终违规率",
            "final_violation_rate",
            limits.max_final_violation_rate,
            "max",
            complete=True,
        )
        if limits.baseline_task_success is not None and limits.max_success_regression is not None:
            bound(
                "相对冻结基准的质量回退",
                "task_success",
                max(0, limits.baseline_task_success - limits.max_success_regression),
            )
        for key, target in policy.additional_metrics.items():
            bound(key, key, target.value, target.operator, complete=target.require_complete_audit)
        cases = {row["case_id"] for row in rows if row.get("finished_at")}
        check(
            "独立案例数量",
            "PASS" if len(cases) >= limits.min_independent_cases else "UNKNOWN",
            f"{len(cases)} / {limits.min_independent_cases}",
        )
        gold = {row["case_id"] for row in rows if _gold(row)}
        required_gold = {
            row["case_id"]
            for row in rows
            if (row.get("expected") or {}).get("human_review_required", True)
        }
        gold_pass = (
            bool(cases)
            and required_gold <= gold
            and len(gold) / len(cases) >= limits.min_human_gold
        )
        check(
            "独立人工金标",
            "PASS" if gold_pass else "UNKNOWN",
            f"独立复核 {len(gold)}，要求人工核验 {len(required_gold)}",
        )
        for path in policy.required_checks:
            observations = [
                item.get("status") for item in machine_checks if item.get("path") == path
            ]
            decision = (
                "FAIL"
                if "FAIL" in observations
                else "PASS"
                if observations and all(value == "PASS" for value in observations)
                else "UNKNOWN"
            )
            check(path, decision, "来自冻结的确定性检查。")
        if limits.require_live:
            live = (
                source == "live"
                and all(
                    (row.get("actual") or {}).get("observed_source") == "live_provider"
                    for row in rows
                )
                and bool(rows)
            )
            check("真实模型证据", "PASS" if live else "UNKNOWN", "需要真实模型尝试。")
        reviewed = bool(rows) and all(
            row["status"] not in {"NEEDS_REVIEW", "RUNNING", "QUEUED", "NOT_RUN"} for row in rows
        )
        check("案例结果复核", "PASS" if reviewed else "UNKNOWN", "待人工复核案例保留。")

    failed = any(item["status"] == "FAIL" for item in checks)
    passed = policy_data is not None and all(item["status"] == "PASS" for item in checks)
    return {
        "status": "FAILED" if failed else "PASS" if passed else "INCOMPLETE",
        "checks": checks,
        "policy_id": policy_data.get("id") if policy_data else None,
        "policy_version": policy_data.get("version") if policy_data else None,
        "release_status": "PENDING_INDEPENDENT_APPROVAL" if passed else "BLOCKED",
    }
