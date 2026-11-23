"""Local tester workbench. Each preview owns an immutable configuration and evaluator copy."""

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Thread
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aftercare.business.errors import BusinessError
from aftercare.config import ROOT

from .investigations import authorize

ARMS = {
    "S1": "强单 Agent",
    "S1c": "低成本单 Agent",
    "M2": "低成本执行＋强审查",
    "S2": "强单＋自检",
    "M1": "全强多 Agent",
}
SCRIPTS = (
    "workbench_campaign.py",
    "run_tau_official_campaign.py",
    "tau_official_agents.py",
    "tau_official_agents_legacy.py",
    "tau_official_summary.py",
    "tau_official_budget.py",
    "build_campaign_dashboard.py",
)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[Annotated[str, Field(pattern=r"^\d{1,3}$")]] = Field(min_length=1, max_length=40)
    architectures: list[Literal["S1", "S1c", "S2", "M1", "M2"]] = Field(min_length=1, max_length=5)
    max_calls: int = Field(strict=True, ge=1, le=10000)
    max_usd: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False, multiple_of=0.01)]


class LaunchConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_paid: Literal[True]


def monitoring_scope(settings):
    kind = "container" if getattr(settings, "container", False) else "host"
    return os.getenv("AFTERCARE_MONITORING_SCOPE") or f"{kind}-{getattr(settings, 'profile', 'app')}"


def read_start(path):
    value = path.read_text().strip()
    if value.startswith("{"):
        return json.loads(value)
    return {"time": float(value or path.stat().st_mtime), "scope": None}


def store():
    return Path(os.getenv("AFTERCARE_BENCHMARK_JOBS") or ROOT / "reports/workbench-campaigns")


def read(path):
    return json.loads(path.read_bytes())


def catalog(user):
    authorize(user)
    try:
        report = read(ROOT / "evidence/tau_official/latest.json")
    except FileNotFoundError:
        raise BusinessError(
            "BENCHMARK_UNAVAILABLE", "当前服务缺少 Retail 题目目录。", 503
        ) from None
    descriptions = {
        r["task_id"]: r["task_summary"] for r in report["rows"] if r.get("task_summary")
    }
    return {
        "tasks": [{"id": i, "label": descriptions.get(i, "")} for i in report["task_ids"]],
        "architectures": ARMS,
        "revision": report["revision"],
    }


def runtime_gaps(arms, model_enabled):
    gaps = [] if model_enabled else ["启用模型的实时服务"]
    if not os.access(ROOT / ".runtime/tau-official-venv/bin/python", os.X_OK):
        gaps.append("官方 Python 评测环境")
    if not (ROOT / ".runtime/tau-official-source/.git").exists():
        gaps.append("官方数据集检出")

    def credential(env, filename):
        return os.getenv(env) or (ROOT / ".runtime" / filename).is_file()

    if not (os.getenv("OPENAI_API_KEY") or credential("AFTERCARE_OPENAI_KEY", "model-secret.json")):
        gaps.append("OpenAI 凭据")
    if {"S1c", "M2"}.intersection(arms) and not credential(
        "AFTERCARE_DEEPSEEK_KEY", "deepseek-secret.json"
    ):
        gaps.append("DeepSeek 凭据")
    return gaps


def preview(user, selection, model_enabled):
    options = catalog(user)
    ids, arms = selection.task_ids, selection.architectures
    if (
        len(ids) != len(set(ids))
        or len(arms) != len(set(arms))
        or set(ids) - {task["id"] for task in options["tasks"]}
    ):
        raise BusinessError(
            "INVALID_SELECTION", "请从当前数据集选择题目和架构，并移除重复项。", 422
        )
    folder = store()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    identifier = str(uuid4())
    staging = Path(tempfile.mkdtemp(prefix=".preview-", dir=folder))
    try:
        kernel = Path("backend/src/aftercare/agent_kernel")
        paths = [p.relative_to(ROOT) for p in (ROOT / kernel).glob("*.py")]
        paths += [Path("scripts") / name for name in SCRIPTS]
        paths += [Path("backend/src/aftercare/__init__.py"), Path("data/tau/manifest.json")]
        hashes = {}
        for path in sorted(paths):
            dest = staging / "source" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = (ROOT / path).read_bytes()
            dest.write_bytes(data)
            hashes[str(path)] = sha256(data).hexdigest()

        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()

        plan = dict(
            id=identifier,
            owner=user.user_id,
            created=datetime.now(UTC).isoformat(),
            revision=options["revision"],
            commit=git("rev-parse", "HEAD"),
            patched=bool(git("status", "--porcelain", "--", *map(str, paths))),
            hashes=hashes,
            snapshot=sha256(
                json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            **selection.model_dump(),
        )
        (staging / "plan.json").write_text(json.dumps(plan))
        staging.rename(folder / identifier)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise BusinessError(
            "SOURCE_UNAVAILABLE", "此环境缺少评测源码或 Git 版本信息。", 503
        ) from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return describe(folder / identifier, plan, model_enabled)


def describe(directory, plan, model_enabled):
    view = {k: v for k, v in plan.items() if k not in {"hashes", "owner"}}
    state = "preview"
    outcome = {}
    started = directory / "started"
    final = directory / "outcome.json"
    if started.exists():
        state = "running"
        # The launcher and the detached worker share the same locked descriptor.
        with (directory / "lock").open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = "interrupted"
            except BlockingIOError:
                pass
        if final.is_file():
            outcome = read(final)
            state = outcome["state"]
    counts = outcome.get("counts")
    if counts is None:
        counts = Counter(read(p)["status"] for p in (directory / "run/rows").glob("*.json"))
    planned = len(plan["task_ids"]) * len(plan["architectures"])
    return dict(
        **view,
        state=state,
        planned=planned,
        finished=sum(counts.values()) - counts.get("running", 0),
        counts=counts,
        result=outcome.get("result"),
        billing=outcome.get("billing"),
        message=outcome.get("message"),
        missing=runtime_gaps(plan["architectures"], model_enabled) if state == "preview" else [],
    )


def list_jobs(user, model_enabled):
    authorize(user)
    plans = [(p.parent, read(p)) for p in store().glob("[!.]*/plan.json")]
    return {
        "items": [
            describe(d, p, model_enabled)
            for d, p in sorted(plans, key=lambda pair: pair[1]["created"], reverse=True)
            if p["owner"] == user.user_id
        ]
    }


def launch(user, identifier: UUID, model_enabled, *, scope=None):
    authorize(user)
    directory = store() / str(identifier)
    path = directory / "plan.json"
    if not path.is_file() or (plan := read(path))["owner"] != user.user_id:
        raise BusinessError("BATCH_NOT_FOUND", "找不到这个评测批次。", 404)
    with (directory / "lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return describe(directory, plan, model_enabled)
        if not (directory / "started").exists():
            if gaps := runtime_gaps(plan["architectures"], model_enabled):
                raise BusinessError("BENCHMARK_UNAVAILABLE", "运行环境缺少" + "、".join(gaps), 503)
            marker = directory / "starting.tmp"
            marker.write_text(json.dumps({"time": datetime.now(UTC).timestamp(), "scope": scope}))
            marker.replace(directory / "started")
            try:
                with (directory / "worker.log").open("ab") as log:
                    process = subprocess.Popen(
                        [
                            str(ROOT / ".runtime/tau-official-venv/bin/python"),
                            "-B",
                            str(directory / "source/scripts/workbench_campaign.py"),
                            str(directory),
                            str(ROOT),
                            "--live",
                        ],
                        cwd=directory,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        env={
                            **os.environ,
                            "GIT_OPTIONAL_LOCKS": "0",
                            "PYTHONPATH": "",
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                        pass_fds=(handle.fileno(),),
                        start_new_session=True,
                    )
                Thread(target=process.wait, daemon=True).start()
            except OSError:
                # No child was launched. Record the failure without replaying the batch.
                temporary = directory / "launch-error.tmp"
                temporary.write_text(
                    json.dumps({"state": "failed", "ended_at": datetime.now(UTC).timestamp(), "message": "未能启动后台评测进程。"})
                )
                temporary.replace(directory / "outcome.json")
    return describe(directory, plan, model_enabled)
