"""Optional integration test with real, isolated local Prometheus, Redis and a Celery worker.

Provide AFTERCARE_TEST_PROMETHEUS and AFTERCARE_TEST_REDIS_SERVER binary paths.
Only the loopback interfaces and a temporary SQLite database are used.
"""

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import redis
import uvicorn
from celery import Celery
from celery.contrib.testing.worker import start_worker
from fastapi import FastAPI
from sqlalchemy import create_engine

import aftercare.conversations.tables  # noqa: F401 -- register database foreign keys
from aftercare.auth import current_user
from aftercare.config import ROOT
from aftercare.db import Base
from aftercare.operations import install


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(check, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx.TransportError, redis.ConnectionError):
            pass
        time.sleep(0.25)
    pytest.fail("Timed out waiting for the isolated monitoring stack")


@pytest.mark.skipif(
    not all(os.getenv(k) for k in ("AFTERCARE_TEST_PROMETHEUS", "AFTERCARE_TEST_REDIS_SERVER")),
    reason="requires local server binaries",
)
def test_real_scrapes_during_offline_task(tmp_path, monkeypatch):
    api_port, broker_port, prom_port = (free_port() for _ in range(3))
    api = f"http://127.0.0.1:{api_port}"
    prom = f"http://127.0.0.1:{prom_port}"
    broker = f"redis://127.0.0.1:{broker_port}/0"
    monkeypatch.setenv("AFTERCARE_PROMETHEUS_URL", prom)
    monkeypatch.setenv("AFTERCARE_BENCHMARK_JOBS", str(tmp_path / "jobs"))
    directory = tmp_path / "jobs" / str(uuid4())
    directory.mkdir(parents=True)
    (directory / "plan.json").write_text(
        json.dumps({"owner": "offline", "architectures": ["离线汇总"]})
    )
    (directory / "lock").touch()

    app = FastAPI()
    app.state.settings = SimpleNamespace(redis_url=broker, profile="test")
    app.state.engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite'}")
    Base.metadata.create_all(app.state.engine)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        role="tester", user_id="offline"
    )
    install(app)

    @app.get("/api/offline-task-status")
    def status():
        return {"finished": (directory / "outcome.json").exists()}

    celery = Celery("monitoring-offline", broker=broker)
    celery.conf.update(task_default_queue="aftercare-test", task_ignore_result=True)
    completed = threading.Event()
    sampled_after_claim = threading.Event()

    @celery.task(name="monitoring.summarize_offline")
    def summarize_offline():
        archive = (ROOT / "evidence/tau_official/dashboard.json").read_bytes()
        result = {
            "state": "completed",
            "offline_digest": hashlib.sha256(archive).hexdigest(),
            "groups": sum(len(b["groups"]) for b in json.loads(archive)),
        }
        assert sampled_after_claim.wait(20)
        (directory / "outcome.json").write_text(json.dumps(result))
        completed.set()

    cfg = tmp_path / "prometheus.yml"
    cfg.write_text(
        (ROOT / "infra/monitoring/prometheus.yml")
        .read_text()
        .replace("runtime-api:8000", f"127.0.0.1:{api_port}")
    )
    with ExitStack() as stack:

        def server(args):
            proc = subprocess.Popen(
                args, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            def stop():
                proc.terminate()
                proc.wait(timeout=10)

            stack.callback(stop)

        server(
            [
                os.environ["AFTERCARE_TEST_REDIS_SERVER"],
                "--bind",
                "127.0.0.1",
                "--port",
                str(broker_port),
                "--save",
                "",
                "--appendonly",
                "no",
            ]
        )
        eventually(lambda: redis.Redis.from_url(broker).ping())
        http = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error")
        )
        pool = stack.enter_context(ThreadPoolExecutor(1))
        future = pool.submit(http.run)

        def stop_http():
            http.should_exit = True
            future.result(timeout=5)

        stack.callback(stop_http)
        server(
            [
                os.environ["AFTERCARE_TEST_PROMETHEUS"],
                f"--config.file={cfg}",
                f"--web.listen-address=127.0.0.1:{prom_port}",
                f"--storage.tsdb.path={tmp_path / 'data'}",
            ]
        )
        client = stack.enter_context(httpx.Client(base_url=api, trust_env=False))

        def series(batch=False):
            path = "/api/operations/timeline"
            return (
                client.get(path, params={"batch": directory.name} if batch else {"minutes": 1})
                .json()
                .get("series", {})
            )

        eventually(lambda: series().get("queue_messages"))
        import fcntl

        with (directory / "lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            (directory / "started").write_text(
                json.dumps({"time": time.time(), "scope": "host-test"})
            )
            summarize_offline.delay()

            def collected():
                client.get("/api/offline-task-status").raise_for_status()
                s = series(True)
                return (
                    any(v == 1 for _, v in s.get("queue_messages", []))
                    and s.get("api_p95_ms")
                    and s.get("cpu_percent")
                    and s.get("service_known_usd")
                )

            eventually(collected)
            with start_worker(celery, perform_ping_check=False, pool="solo", concurrency=1):
                eventually(
                    lambda: any(v == 0 for _, v in series(True).get("queue_messages", [])[-1:])
                )
                sampled_after_claim.set()
                assert completed.wait(10)
        eventually(lambda: any(v == 0 for _, v in series().get("queue_messages", [])[-1:]))
        result = series(True)
        assert all(
            result.get(k)
            for k in (
                "queue_messages",
                "cpu_percent",
                "api_p95_ms",
                "api_rss_bytes",
                "service_known_usd",
                "benchmark_known_usd",
            )
        )
        assert all(
            v == 0 for k in ("service_known_usd", "benchmark_known_usd") for _, v in result[k]
        )
        assert {v for _, v in result["queue_messages"]} == {0, 1}
        end = (directory / "outcome.json").stat().st_mtime
        assert all(t <= end + 0.001 for points in result.values() for t, _ in points)
        print(
            "Offline batch samples:",
            {
                k: len(result[k])
                for k in ("cpu_percent", "api_p95_ms", "queue_messages", "benchmark_known_usd")
            },
            "peak queue:",
            max(v for _, v in result["queue_messages"]),
        )
