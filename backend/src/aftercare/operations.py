"""Tester-only, fixed PromQL queries for one application profile."""

import fcntl
import json
import math
import os
import time
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Histogram, generate_latest

from aftercare.auth import UserContext, current_user
from aftercare.config import ROOT
from aftercare.evaluation.workbench_jobs import monitoring_scope, read_start, store
from aftercare.operating_metrics import OperatingCollector, RequestTiming

JOB = '{job="aftercare-api"}'
LATENCY = "aftercare_request_seconds"
GAUGES = (
    "memory_percent",
    "api_rss_bytes",
    "disk_free_bytes",
    "queue_messages",
    "waiting_jobs",
    "oldest_wait_seconds",
    "redis_memory_bytes",
    "service_known_usd",
    "benchmark_known_usd",
    "service_unpriced_calls",
    "benchmark_unpriced_calls",
)


def window(directory, plan):
    started = directory / "started"
    if not started.is_file():
        return None
    marker = read_start(started)
    end = None
    state = "running"
    # If the worker died there is no defensible end timestamp for the window.
    with (directory / "lock").open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = "interrupted"
        except BlockingIOError:
            pass
    outcome = directory / "outcome.json"
    if outcome.is_file():
        result = json.loads(outcome.read_bytes())
        state = result["state"]
        end = result.get("ended_at", outcome.stat().st_mtime)
    return {
        "id": directory.name,
        "label": f"{directory.name[:8]} · {' / '.join(plan['architectures'])}",
        "start": marker["time"],
        "scope": marker["scope"],
        "end": end,
        "state": state,
    }


def tester(user: Annotated[UserContext, Depends(current_user)]):
    if user.role != "tester":
        raise HTTPException(
            403, detail={"code": "OPERATIONS_FORBIDDEN", "message": "运行总览仅向测试员开放。"}
        )
    return user


Tester = Annotated[UserContext, Depends(tester)]


def series_query(start):
    def rate(name):
        return f"rate(aftercare_{name}{JOB}[30s])"

    expressions = {name: f"aftercare_{name}{JOB}" for name in GAUGES}
    expressions.update(
        cpu_percent=f"100 * {rate('cpu_busy_seconds')} / {rate('cpu_seconds')}",
        network_bytes_per_second=rate("network_bytes"),
        api_p95_ms=f"1000 * histogram_quantile(0.95,sum by(le)(rate({LATENCY}_bucket{JOB}[30s])))",
        error_percent=f'100 * sum(rate({LATENCY}_count{{job="aftercare-api",status_class="5xx"}}[30s])) / sum(rate({LATENCY}_count{JOB}[30s]))',
        requests_per_second=f"sum(rate({LATENCY}_count{JOB}[30s]))",
    )
    branches = [
        f'label_replace(({expr}), "series", "{name}", "", "")' for name, expr in expressions.items()
    ]
    up = "up" + JOB
    fresh = f"(({up}) and (timestamp({up}) >= {start}) and (timestamp({up}) > time()-6))"
    samples = "(" + " or ".join(branches) + f") and on() ({fresh} == 1)"
    return f'{samples} or label_replace({fresh}, "series", "scrape_ok", "", "")'


def install(app):
    registry = CollectorRegistry()
    histogram = Histogram(
        LATENCY,
        "Business API response duration",
        ["status_class"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
        registry=registry,
    )
    for status_class in ("2xx", "3xx", "4xx", "5xx"):
        histogram.labels(status_class)
    registry.register(OperatingCollector(app))
    app.add_middleware(RequestTiming, histogram=histogram)

    @app.get("/internal/metrics", include_in_schema=False)
    def scrape():
        return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.get("/api/operations/batches")
    def batches(user: Tester):
        items, unscoped = [], []
        scope = monitoring_scope(app.state.settings)
        for path in store().glob("[!.]*/plan.json"):
            plan = json.loads(path.read_bytes())
            if plan["owner"] == user.user_id:
                item = window(path.parent, plan)
                if item and item["scope"] == scope:
                    items.append(item)
                elif item and item["scope"] is None:
                    unscoped.append(item["label"] + " · 采集环境未知")
        items.sort(key=lambda item: item["start"], reverse=True)
        archive = ROOT / "evidence/tau_official/dashboard.json"
        historical = (
            [b["label"] for b in json.loads(archive.read_bytes())] if archive.exists() else []
        )
        return {"items": items, "undated": historical + unscoped}

    @app.get("/api/operations/timeline")
    async def timeline(
        user: Tester,
        batch: UUID | None = None,
        minutes: int = Query(default=15, ge=1, le=1440),
    ):
        end = time.time()
        start = end - minutes * 60
        selected = None
        if batch is not None:
            directory = store() / str(batch)
            path = directory / "plan.json"
            if (
                not path.is_file()
                or (plan := json.loads(path.read_bytes()))["owner"] != user.user_id
            ):
                raise HTTPException(404, detail={"message": "找不到该批次。"})
            selected = window(directory, plan)
            if (
                not selected
                or selected["scope"] != monitoring_scope(app.state.settings)
                or selected["state"] == "interrupted"
            ):
                raise HTTPException(409, detail={"message": "当前环境没有该批次可核实的运行时段。"})
            start, end = selected["start"], selected["end"] or end
        end = max(start, end)
        step = max(1, math.ceil((end - start) / 320))
        # Align the last observation with the window end, including sub-second batches.
        end_ms = math.floor(end * 1000)
        first_ms = (
            end_ms - max(0, (end_ms - math.ceil(start * 1000)) // (1000 * step)) * 1000 * step
        )
        first = first_ms / 1000
        port = 19092 if app.state.settings.profile == "test" else 19090
        url = os.getenv("AFTERCARE_PROMETHEUS_URL", f"http://127.0.0.1:{port}")
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    url.rstrip("/") + "/api/v1/query_range",
                    params={"query": series_query(start), "start": first, "end": end, "step": step},
                )
                response.raise_for_status()
                data = response.json()
                if data["status"] != "success":
                    raise ValueError("query failed")
                series = {
                    r["metric"]["series"]: [
                        [t, float(v)] for t, v in r["values"] if math.isfinite(float(v))
                    ]
                    for r in data["data"]["result"]
                }
        except (httpx.HTTPError, ValueError, KeyError) as error:
            raise HTTPException(
                503, detail={"message": "无法读取 Prometheus，请检查采集服务。"}
            ) from error
        return {
            "start": start,
            "end": end,
            "step": step,
            "first": first,
            "batch": selected,
            "series": series,
            "resource_scope": "容器内可见的系统"
            if getattr(app.state.settings, "container", False)
            else "API 所在主机",
        }
