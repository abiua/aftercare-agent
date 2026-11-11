"""Real TCP progress reads against production routes and a disposable PG schema."""

import json
import os
import secrets
import socket
from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from aftercare.auth import token_hash
from aftercare.business.rules import now
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.evaluation.api import router
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import (
    EvaluationMutation,
    EvaluationProgress,
    EvaluationResult,
    EvaluationReview,
    Experiment,
)

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_PROGRESS_HTTP_PG") != "1",
        reason="explicit isolated PostgreSQL and TCP acceptance required",
    ),
]


@contextmanager
def loopback(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=None,
                log_level="critical",
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=2,
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 8
            while thread.is_alive() and not server.started and monotonic() < deadline:
                sleep(0.02)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def frames(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    frame = {}
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if not line:
            if frame:
                yield frame["event"], frame.get("id"), json.loads(frame["data"])
                frame = {}
            continue
        field, value = line.split(":", 1)
        frame[field] = value.lstrip()


def create_plan(client, owner, key, **changes):
    body = {
        "name": "isolated TCP progress",
        "dataset_id": "v0.2",
        "split": "dev",
        "mode": "single",
        "strategy": "routed",
        "repetitions": 1,
        "concurrency": 1,
        "max_calls": 1,
        "budget_usd": 0,
        "source": "fixture",
        "case_ids": ["AC02-C001"],
        **changes,
    }
    result = client.post(
        "/api/evaluation/experiments",
        json=body,
        headers={**owner, "Idempotency-Key": key},
    )
    assert result.status_code == 201
    return result.json()


def progress_path(experiment_id):
    return f"/api/evaluation/experiments/{experiment_id}/progress"


def exercise(client, scoped, service, settings, credentials, legacy_id):
    owner, viewer, support = (auth(credentials[name]) for name in ("owner", "viewer", "support"))
    assert client.get("/api/evaluation/capabilities", headers=owner).json()["eval_progress_events"]

    # This is the production create/process path with a controlled zero-model result.
    small = create_plan(client, owner, "small")
    calls = []

    def run(result_id, *_args, **_kwargs):
        calls.append(result_id)
        return {
            "status": "NEEDS_REVIEW",
            "failure_reason": "HUMAN_REFERENCE_PENDING",
            "actual": None,
            "trace": [],
            "checks": [],
        }

    runner = SimpleNamespace(settings=settings, model=None, run=run)
    service.process(small["id"], runner)
    service.process(small["id"], runner)
    assert calls == [small["results"][0]["id"]]
    small_path = progress_path(small["id"])
    with client.stream("GET", small_path + "/stream", headers=owner) as response:
        completed = list(frames(response))
    assert [(kind, identifier) for kind, identifier, _ in completed] == [
        *[("evaluation-progress", str(seq)) for seq in range(1, 7)],
        ("evaluation-state", None),
    ]
    assert completed[-1][2]["terminal"] and completed[-1][2]["status"] == "COMPLETED"
    assert [item[2]["status"] for item in completed[:-1]] == [
        "QUEUED",
        "QUEUED",
        "RUNNING",
        "RUNNING",
        "NEEDS_REVIEW",
        "COMPLETED",
    ]

    # 120 planned results are created through the actual HTTP endpoint and journal writer.
    long = create_plan(client, owner, "long", case_ids=None, repetitions=2)
    path = progress_path(long["id"])
    first = client.get(path, headers=owner).json()
    tail = client.get(path, headers=owner, params={"after": 100}).json()
    journal = first["events"] + tail["events"]
    assert len(first["events"]) == 100 and len(tail["events"]) == 21
    assert first["latest_sequence"] == tail["latest_sequence"] == 121
    assert [event["sequence"] for event in journal] == list(range(1, 122))
    assert client.get(path, headers=owner, params={"after": 121}).json()["events"] == []

    with client.stream("GET", path + "/stream", headers=viewer) as response:
        stream = frames(response)
        received = [next(stream) for _ in range(10)]
        assert [frame[2] for frame in received] == journal[:10]
    with client.stream(
        "GET",
        path + "/stream",
        headers={**viewer, "Last-Event-ID": received[-1][1]},
        params={"after": 1, "follow": "false"},
    ) as response:
        resumed = list(frames(response))
    assert [frame[2] for frame in resumed[:-1]] == journal[10:]
    assert resumed[-1][0:2] == ("evaluation-state", None)
    assert resumed[-1][2]["latest_sequence"] == 121 and not resumed[-1][2]["terminal"]

    assert client.get(path).status_code == 401
    assert client.get(path, headers=support).status_code == 403
    assert client.get(path + "/stream", headers=support).status_code == 403
    future = client.get(path + "/stream", headers={**owner, "Last-Event-ID": "122"})
    assert future.status_code == 400 and future.json()["detail"]["code"] == "FUTURE_PROGRESS_CURSOR"

    historical = client.get(progress_path(legacy_id), headers=owner)
    assert historical.json() == {
        "experiment_id": legacy_id,
        "history": "HISTORICAL_SNAPSHOT",
        "status": "RUNNING",
        "terminal": False,
        "latest_sequence": 0,
        "events": [],
    }
    with client.stream("GET", progress_path(legacy_id) + "/stream", headers=owner) as response:
        assert list(frames(response)) == [
            (
                "evaluation-state",
                None,
                {k: v for k, v in historical.json().items() if k != "events"},
            )
        ]

    # Revoke a dedicated viewer while its stream is open, then commit a real new transition.
    with client.stream(
        "GET", path + "/stream", headers={**viewer, "Last-Event-ID": "121"}
    ) as response:
        stream = frames(response)
        assert next(stream)[0:2] == ("evaluation-state", None)
        with Session(scoped) as db, db.begin():
            db.get_one(User, "viewer").enabled = False
        cancelled = client.post(
            f"/api/evaluation/experiments/{long['id']}/cancel",
            headers={**owner, "Idempotency-Key": "cancel"},
            json={"expected_version": long["version"]},
        )
        assert cancelled.status_code == 200
        assert list(stream) == []
    assert (
        client.get(path + "/stream", headers={**viewer, "Last-Event-ID": "121"}).status_code == 401
    )
    settled = client.get(path, headers=owner, params={"after": 121}).json()
    assert settled["terminal"] and settled["status"] == "CANCELLED"
    assert settled["latest_sequence"] == 242
    assert settled["events"][0]["status"] == "CANCELLED"


def test_live_http_evaluation_progress_and_revocation():
    settings = load_settings("test").model_copy(
        update={"model_enabled": False, "embedding_enabled": False, "embedding_model_path": None}
    )
    engine = guarded_engine(settings, test_only=True)
    schema = "progress_http_" + uuid4().hex
    legacy_id = str(uuid4())
    credentials = {name: secrets.token_urlsafe(32) for name in ("owner", "viewer", "support")}
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        for table in (
            User,
            Experiment,
            EvaluationResult,
            EvaluationReview,
            EvaluationMutation,
            EvaluationProgress,
        ):
            table.__table__.create(scoped)
        with Session(scoped) as db, db.begin():
            for name, token in credentials.items():
                db.add(
                    User(
                        id=name,
                        display_name=name,
                        role="support" if name == "support" else "tester",
                        token_hash=token_hash(token),
                        enabled=True,
                    )
                )
            db.add(
                Experiment(
                    id=legacy_id,
                    name="legacy",
                    created_by="owner",
                    status="RUNNING",
                    version=1,
                    config={"source": "fixture"},
                    created_at=now(),
                    updated_at=now(),
                )
            )
        service = EvaluationService(scoped, settings, execution_driver="inline")
        app = FastAPI()
        app.state.evaluation, app.state.engine = service, scoped
        app.include_router(router, prefix="/api")
        with (
            loopback(app) as base,
            httpx.Client(base_url=base, trust_env=False, timeout=5) as client,
        ):
            exercise(client, scoped, service, settings, credentials, legacy_id)
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                assert connection.scalar(
                    text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}
                )
        finally:
            engine.dispose()
