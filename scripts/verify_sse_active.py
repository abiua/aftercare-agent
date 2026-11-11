"""Exercise the production run SSE router over loopback TCP using synthetic test PG rows.

Run from the project root using the locked backend environment:
    PYTHONPATH=backend/src python scripts/verify_sse_active.py \
        --run-test-postgresql --output reports/sse-active-synthetic.json
"""

import argparse
import json
import os
import secrets
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.business.models import (
    Approval, Case, Customer, Execution, Inventory, Material, MaterialReview,
    ObjectUpload, Order, OrderLine, Policy, Product, Proposal,
)
from aftercare.business.service import BusinessService
from aftercare.config import load_settings
from aftercare.db import Base, User, guarded_engine
from aftercare.intelligence.api import router
from aftercare.intelligence.service import AgentService, EVENT_PAGE_SIZE
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from verify_sse_http import CheckFailed, expect_error, replay, require, sse_frames

HISTORY_COUNT = 405
ACTIVE_STATUS = "RUNNING"
HISTORY_STATUS = "COMPLETED"
FIXTURE_KIND = "synthetic_sse_fixture"
TABLES = [
    model.__table__ for model in (
        User, Customer, Product, Order, OrderLine, Inventory, Policy, Case,
        ObjectUpload, Material, MaterialReview, Proposal, Approval, Execution,
        AgentRun, RunEvent, ModelAttempt,
    )
]


@contextmanager
def isolated_engine(engine, report):
    namespace = "sse_acceptance_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: namespace})

    @event.listens_for(scoped, "begin")
    def protect_scope(connection):
        connection.exec_driver_sql("SET LOCAL search_path = pg_catalog")
        connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")

    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
    report["cleanup"] = "PENDING"
    try:
        # All allowlisted tables and foreign keys are translated to this schema.
        Base.metadata.create_all(scoped, tables=TABLES, checkfirst=False)
        yield scoped
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
            connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
        report["cleanup"] = "DROPPED_OWN_FIXTURE_SCHEMA"


def seed(engine, credential):
    stamp = datetime.now(UTC)
    owner = UserContext(
        user_id=str(uuid4()), role="customer", customer_id=str(uuid4()),
        display_name="Synthetic SSE acceptance customer",
    )
    order_id, case_id = str(uuid4()), str(uuid4())
    run_specs = [(str(uuid4()), HISTORY_STATUS, HISTORY_COUNT), (str(uuid4()), ACTIVE_STATUS, 1)]
    with Session(engine) as db, db.begin():
        db.add_all([
            User(
                id=owner.user_id, role=owner.role, customer_id=owner.customer_id,
                display_name=owner.display_name, token_hash=token_hash(credential), enabled=True,
            ),
            Customer(id=owner.customer_id, data={}),
        ])
        db.flush()
        db.add(Order(id=order_id, customer_id=owner.customer_id, data={"status": "DELIVERED"}))
        db.flush()
        db.add(Case(
            id=case_id, ticket_id=str(uuid4()), order_id=order_id, customer_id=owner.customer_id,
            intent="refund_status", description="Synthetic fixture for SSE transport verification",
            scope_hash="synthetic", line_ids=[], created_at=stamp,
        ))
        for run_id, status, count in run_specs:
            db.add(AgentRun(
                id=run_id, thread_id=f"synthetic-{run_id}", case_id=case_id,
                owner_id=owner.user_id, actor=owner.model_dump(), status=status,
                mode="single", strategy="routed", message="synthetic fixture",
                case_version=1, event_seq=count, created_at=stamp, updated_at=stamp,
            ))
        db.flush()
        for run_id, _, count in run_specs:
            db.add_all(
                RunEvent(
                    run_id=run_id, seq=seq, kind=FIXTURE_KIND,
                    payload={"source": "synthetic_fixture", "ordinal": seq}, created_at=stamp,
                )
                for seq in range(1, count + 1)
            )
    paths = [f"/api/cases/{case_id}/runs/{run_id}" for run_id, _, _ in run_specs]
    return owner, paths


def fixture_app(engine, settings):
    def disabled_dispatch(_run_id):
        raise RuntimeError("SSE_FIXTURE_DISPATCH_DISABLED")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.agent = AgentService(
        engine, settings, BusinessService(engine, settings), checkpointer=None,
        dispatch=disabled_dispatch,
    )
    app.include_router(router, prefix="/api")
    return app


@contextmanager
def loopback(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, log_level="critical", access_log=False, proxy_headers=False,
            timeout_graceful_shutdown=3,
        ))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = monotonic() + 8
            while worker.is_alive() and not server.started and monotonic() < deadline:
                sleep(0.02)
            require(server.started, "FIXTURE_TCP_SERVER_START_FAILED")
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            require(not worker.is_alive(), "FIXTURE_TCP_SERVER_SHUTDOWN_FAILED")


def event_page(client, path, after):
    response = client.get(f"{path}/events", params={"after": after})
    require(response.status_code == 200, "SYNTHETIC_EVENT_PAGE_HTTP_FAILED")
    return response.json()


def verify_page_boundary(client, path, report):
    require(EVENT_PAGE_SIZE == 200, "UNEXPECTED_PRODUCTION_EVENT_PAGE_SIZE")
    pages = [event_page(client, path, after) for after in (0, 200, 400, HISTORY_COUNT)]
    lengths = [len(page) for page in pages]
    require(lengths == [200, 200, 5, 0], "SYNTHETIC_EVENT_PAGE_LENGTHS_MISMATCH")
    rows = [row for page in pages for row in page]
    require([row["seq"] for row in rows] == list(range(1, HISTORY_COUNT + 1)),
            "SYNTHETIC_EVENT_SEQUENCE_MISMATCH")
    require(all(
        row["kind"] == FIXTURE_KIND
        and row["payload"] == {"source": "synthetic_fixture", "ordinal": row["seq"]}
        for row in rows
    ), "SYNTHETIC_EVENT_PAYLOAD_MISMATCH")

    prefix, idle = replay(client, path, stop_after=200)
    require(prefix == rows[:200] and idle is None, "PAGE_BOUNDARY_INITIAL_STREAM_MISMATCH")
    # A contradictory query cursor ensures the reconnect honors Last-Event-ID.
    suffix, idle = replay(client, path, after=1, header=prefix[-1]["seq"])
    require(suffix == rows[200:] and idle == HISTORY_STATUS, "PAGE_BOUNDARY_RECONNECT_MISMATCH")
    report["pagination"] = {
        "page_lengths": lengths, "disconnected_at": 200, "reconnected_from": 201,
        "replayed_through": HISTORY_COUNT, "replayed_count": len(suffix),
    }
    report["checks"]["page_boundary_observed"] = "PASSED"


def verify_active_revocation(engine, client, path, owner, credential, report):
    require(client.get(path).json().get("status") == ACTIVE_STATUS, "ACTIVE_FIXTURE_NOT_RUNNING")
    with client.stream("GET", f"{path}/stream") as response:
        require(response.status_code == 200, "ACTIVE_STREAM_OPEN_FAILED")
        require(response.headers.get("content-type", "").startswith("text/event-stream"),
                "ACTIVE_STREAM_CONTENT_TYPE_MISMATCH")
        frames = sse_frames(response)
        kind, event_id, payload = next(frames, (None, None, None))
        require(kind == "message" and event_id == 1 and payload.get("kind") == FIXTURE_KIND,
                "ACTIVE_STREAM_INITIAL_EVENT_MISMATCH")
        for _ in range(2):
            require(next(frames, None) == ("comment", None, ": heartbeat"),
                    "ACTIVE_STREAM_HEARTBEAT_MISSING")
        with Session(engine) as db, db.begin():
            disabled = db.scalar(
                update(User)
                .where(User.id == owner.user_id, User.token_hash == token_hash(credential),
                       User.enabled.is_(True))
                .values(enabled=False)
                .returning(User.id)
            )
            require(disabled == owner.user_id, "SYNTHETIC_CREDENTIAL_REVOCATION_FAILED")
        revoked_at = monotonic()
        trailing_heartbeats = 0
        for frame in frames:
            require(frame == ("comment", None, ": heartbeat") and trailing_heartbeats == 0,
                    "ACTIVE_STREAM_CONTINUED_AFTER_REVOCATION")
            trailing_heartbeats += 1
        elapsed = monotonic() - revoked_at
        require(elapsed < 4, "ACTIVE_STREAM_REVOCATION_TIMEOUT")

    expect_error(client.get(f"{path}/stream", headers={"Last-Event-ID": "1"}), 401, "AUTH_REQUIRED")
    report["revocation"] = {
        "heartbeats_before_revocation": 2, "trailing_heartbeats": trailing_heartbeats,
        "clean_eof": True, "close_seconds": round(elapsed, 3), "reconnect_http": 401,
    }
    report["checks"]["already_connected_token_revocation"] = "PASSED"


def verify(engine, settings, report):
    credential = secrets.token_urlsafe(32)
    owner, (history, active) = seed(engine, credential)
    with loopback(fixture_app(engine, settings)) as base_url, httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {credential}"},
        timeout=httpx.Timeout(3.0), trust_env=False, follow_redirects=False,
    ) as client:
        verify_page_boundary(client, history, report)
        verify_active_revocation(engine, client, active, owner, credential, report)
    with Session(engine) as db:
        expected = {HISTORY_STATUS: HISTORY_COUNT, ACTIVE_STATUS: 1}
        require(dict(db.execute(select(AgentRun.status, AgentRun.event_seq)).all()) == expected
                and db.scalar(select(func.count()).select_from(RunEvent)) == HISTORY_COUNT + 1,
                "SYNTHETIC_RUN_HISTORY_CHANGED")
        attempts = db.scalar(select(func.count()).select_from(ModelAttempt))
        require(attempts == 0, "SYNTHETIC_FIXTURE_MODEL_ATTEMPT_FOUND")
        report["model_attempts"] = attempts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-test-postgresql", action="store_true",
                        help="Explicitly allow creating a throwaway schema in the guarded test PG")
    parser.add_argument("--output", type=Path, required=True, help="New evidence JSON file")
    args = parser.parse_args()
    if not args.run_test_postgresql:
        parser.error("--run-test-postgresql is required")
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new private evidence file.\n")
    report = {
        "status": "FAILED",
        "source": "synthetic_fixture_real_loopback_tcp_test_postgresql",
        "scope": "production_agent_sse_router_and_services_in_minimal_test_app",
        "queue": "disabled",
        "model": "disabled",
        "business_end_to_end": False,
        "cleanup": "NOT_CREATED",
        "checks": {
            "page_boundary_observed": "NOT_RUN",
            "already_connected_token_revocation": "NOT_RUN",
        },
    }
    with os.fdopen(descriptor, "w") as stream:
        try:
            settings = load_settings("test")
            require(not settings.container, "HOST_TEST_PROFILE_REQUIRED")
            settings = settings.model_copy(update={
                "model_enabled": False, "embedding_enabled": False, "embedding_model_path": None,
            })
            engine = guarded_engine(settings, test_only=True)
            try:
                with isolated_engine(engine, report) as scoped:
                    verify(scoped, settings, report)
            finally:
                engine.dispose()
            report["status"] = "PASSED"
        except CheckFailed as error:
            report["failure"] = str(error)
        except Exception as error:
            # SQL driver exception details can contain private connection data.
            report["failure"] = type(error).__name__
        finally:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] == "FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
