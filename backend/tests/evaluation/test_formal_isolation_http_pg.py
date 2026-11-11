"""Opt-in acceptance over loopback TCP with only synthetic rows in a disposable PG schema."""

import os
import secrets
import socket
from contextlib import contextmanager
from datetime import timedelta
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_formal_isolation import BASE_PATH, STAMP, TESTER, _experiment, _requests

from aftercare.auth import token_hash
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.dispatch import DeliveryJournal, DispatchEvent, DispatchJob
from aftercare.evaluation.api import router
from aftercare.evaluation.progress import append_progress
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
        os.environ.get("AFTERCARE_FORMAL_ISOLATION_HTTP_PG") != "1",
        reason="explicit isolated PostgreSQL and TCP acceptance required",
    ),
]


@contextmanager
def _loopback(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()
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
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def _seed(engine, token):
    ordinary = _experiment()
    formal = _experiment(formal=True, stamp=STAMP + timedelta(seconds=1))
    result = EvaluationResult(
        id=str(uuid4()),
        experiment_id=formal.id,
        case_id="private-case",
        repetition=1,
        status="RUNNING",
        version=1,
        expected={"private": "synthetic"},
        actual=None,
        checks=[],
        trace=[],
        quota={},
        started_at=STAMP,
    )
    with Session(engine, expire_on_commit=False) as db, db.begin():
        db.add(
            User(
                id=TESTER.user_id,
                token_hash=token_hash(token),
                role=TESTER.role,
                display_name="isolated tester",
                enabled=True,
            )
        )
        db.add_all((ordinary, formal))
        db.flush()
        append_progress(db, ordinary)
        append_progress(db, formal, result)
        db.add(result)
    return ordinary.id, formal.id, result.id


def _evaluation_state(engine):
    with Session(engine) as db:
        return {
            model.__tablename__: [
                dict(row)
                for row in db.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key.columns)
                ).mappings()
            ]
            for model in (
                Experiment,
                EvaluationResult,
                EvaluationProgress,
                EvaluationMutation,
                EvaluationReview,
            )
        }


def _verify_http(client, ordinary_id, formal_id, result_id):
    assert (
        client.get(f"{BASE_PATH}/experiments?limit=1", headers={"Authorization": ""}).status_code
        == 401
    )
    first_page = client.get(f"{BASE_PATH}/experiments?limit=1")
    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == [ordinary_id]
    second_page = client.get(f"{BASE_PATH}/experiments?limit=1&offset=1")
    assert second_page.status_code == 200 and second_page.json()["items"] == []

    missing = _requests(str(uuid4()), str(uuid4()), ordinary_id)
    for (method, path, body), (_, absent, absent_body) in zip(
        _requests(formal_id, result_id, ordinary_id), missing, strict=True
    ):
        if "/progress/stream" in path:
            # Check the first HTTP response of a live stream, before reading any body.
            with client.stream(method, BASE_PATH + path.split("?")[0]) as hidden:
                assert hidden.status_code == 404
                assert not hidden.headers.get("content-type", "").startswith("text/event-stream")
                assert hidden.headers["cache-control"] == "no-store"
                hidden.read()
            with client.stream(method, BASE_PATH + absent.split("?")[0]) as unknown:
                assert unknown.status_code == 404
                unknown.read()
        else:
            hidden = client.request(method, BASE_PATH + path, json=body)
            unknown = client.request(method, BASE_PATH + absent, json=absent_body)
            assert hidden.status_code == unknown.status_code == 404, path
        assert hidden.json() == unknown.json(), path

    ordinary = f"{BASE_PATH}/experiments/{ordinary_id}"
    assert client.get(ordinary).json()["id"] == ordinary_id
    assert client.get(ordinary + "/export").json()["id"] == ordinary_id
    with client.stream("GET", ordinary + "/progress/stream?follow=false") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: evaluation-progress" in list(response.iter_lines())


def _verify_queue(engine, ordinary_id, formal_id):
    journal = DeliveryJournal(engine)
    formal_job = journal.ensure("evaluation", formal_id)
    ordinary_job = journal.ensure("evaluation", ordinary_id)
    with Session(engine) as db, db.begin():
        db.get_one(DispatchJob, formal_job).next_at = STAMP - timedelta(days=1)
        db.get_one(DispatchJob, ordinary_job).next_at = STAMP
    assert journal.due(limit=1) == [ordinary_job]
    assert journal.reserve_publish(formal_job) is None
    assert journal.claim(formal_job) is None
    assert journal.claim(ordinary_job) is not None
    with Session(engine) as db:
        hidden = db.get_one(DispatchJob, formal_job)
        assert (hidden.state, hidden.publications, hidden.claims) == ("READY", 0, 0)
        assert not db.scalar(
            select(func.count())
            .select_from(DispatchEvent)
            .where(DispatchEvent.job_id == formal_job)
        )


def test_formal_rows_hidden_over_real_http_and_pg_dispatch():
    settings = load_settings("test").model_copy(
        update={"model_enabled": False, "embedding_enabled": False, "embedding_model_path": None}
    )
    engine = guarded_engine(settings, test_only=True)
    schema = f"formal_http_{uuid4().hex}"
    token = secrets.token_urlsafe(32)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        for model in (
            User,
            Experiment,
            EvaluationResult,
            EvaluationProgress,
            EvaluationMutation,
            EvaluationReview,
            DispatchJob,
            DispatchEvent,
        ):
            model.__table__.create(scoped)
        ordinary_id, formal_id, result_id = _seed(scoped, token)
        before = _evaluation_state(scoped)
        app = FastAPI()
        app.state.engine = scoped
        app.state.evaluation = EvaluationService(scoped, settings)
        app.include_router(router, prefix="/api")
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "formal-isolation"}
        with (
            _loopback(app) as base,
            httpx.Client(base_url=base, headers=headers, trust_env=False, timeout=5) as client,
        ):
            _verify_http(client, ordinary_id, formal_id, result_id)
        _verify_queue(scoped, ordinary_id, formal_id)
        assert _evaluation_state(scoped) == before
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                assert connection.scalar(
                    text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}
                )
        finally:
            engine.dispose()
