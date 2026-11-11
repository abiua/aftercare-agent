"""Real test HTTP load, plus fixture-gated cancellation over isolated HTTP.

Run from the project root with the test API, worker and beat ready:
    PYTHONPATH=backend/src PYTHONPYCACHEPREFIX="$HOME/.cache/aftercare-agent/pycache" \
      "$HOME/.cache/aftercare-agent/backend-venv/bin/python" scripts/verify_evaluation_http.py \
      --output reports/evaluation-load-cancel-http.json
"""

import argparse
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import uvicorn
from aftercare.auth import token_hash
from aftercare.business.models import Material
from aftercare.business.storage import S3ObjectStore, verify_object
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from aftercare.evaluation.api import router
from aftercare.evaluation.runner import EvaluationRunner, ScriptReplay
from aftercare.evaluation.service import EvaluationService
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from fastapi import FastAPI
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

BASE = "http://127.0.0.1:8022"
EVALUATION = "/api/evaluation/experiments"
CASE_IDS = ["AC02-C001", "AC02-C041"]
READS_PER_CASE = 5


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def client_for(base, token):
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        follow_redirects=False,
        timeout=10,
    )


def request(client, path, *, expected=200, body=None, key=None):
    response = (
        client.get(path)
        if body is None
        else client.post(path, json=body, headers={"Idempotency-Key": key or str(uuid4())})
    )
    require(
        response.status_code == expected, f"EXPECTED_HTTP_{expected}_GOT_{response.status_code}"
    )
    return response.json()


def plan(source, concurrency):
    body = {
        "name": f"HTTP acceptance {source} {uuid4().hex[:8]}",
        "dataset_id": "v0.2",
        "split": "dev",
        "mode": "multi",
        "strategy": "routed",
        "repetitions": 1,
        "concurrency": concurrency,
        "max_calls": 1,
        "budget_usd": 0,
        "source": source,
        "case_ids": CASE_IDS,
    }
    if source == "load":
        body["load_reads"] = READS_PER_CASE
    return body


def namespace(result_id):
    return "eval_" + UUID(result_id).hex


def isolated(engine, result_id):
    return engine.execution_options(schema_translate_map={None: namespace(result_id)})


def no_attempts(engine, result_id):
    with Session(isolated(engine, result_id)) as db:
        require(db.scalar(select(ModelAttempt.run_id).limit(1)) is None, "MODEL_ATTEMPT_FOUND")


def verify_objects(engine, settings, row):
    result_id = row["id"]
    ns = namespace(result_id)
    require(row["actual"]["namespace"] == ns, "LOAD_NAMESPACE_MISMATCH")
    require(inspect(engine).has_schema(ns), "LOAD_POSTGRES_SCHEMA_NOT_FOUND")
    no_attempts(engine, result_id)
    with Session(isolated(engine, result_id)) as db:
        materials = list(db.scalars(select(Material)))
    store = S3ObjectStore(settings)
    for item in materials:
        content = store.get(f"evaluation/{ns}/{item.object_key}", item.size)
        verify_object(content, item.size, item.sha256)
    require(
        len(materials) == (2 if row["case_id"] == CASE_IDS[0] else 0),
        "LOAD_MATERIAL_FIXTURE_MISMATCH",
    )
    return len(materials)


def verify_load(engine, settings, client, tester_id, report):
    started = time.monotonic()
    who = request(client, "/api/me")
    require(who["user_id"] == tester_id and who["role"] == "tester", "TEST_HTTP_IDENTITY_MISMATCH")
    run = request(client, EVALUATION, expected=201, body=plan("load", 2))
    report["experiment_id"] = run["id"]
    path = f"{EVALUATION}/{run['id']}"
    print(f"Queued two-case load experiment {run['id']}.", flush=True)
    while time.monotonic() < started + 120:
        run = request(client, path)
        if run["status"] not in {"QUEUED", "RUNNING"}:
            break
        time.sleep(0.5)
    config = run["config"]
    metrics = {row["key"]: row for row in run["metrics"]}
    report.update(
        experiment_status=run["status"],
        counts=run["counts"],
        wall_ms=round((time.monotonic() - started) * 1000),
        metrics={
            key: metrics[key]
            for key in (
                "task_success",
                "latency_p50",
                "latency_p95",
                "load_operation_success",
                "load_latency_p95",
                "load_peak_concurrency",
                "load_operations_per_second",
            )
            if key in metrics
        },
        cases=[
            {
                "case_id": row["case_id"],
                "status": row["status"],
                "failure_reason": row["failure_reason"],
                "elapsed_ms": (row["actual"] or {}).get("elapsed_ms"),
                "load_reads": (row["actual"] or {}).get("load_operations", []),
            }
            for row in run["results"]
        ],
    )
    require(run["status"] == "COMPLETED", "LOAD_EXPERIMENT_NOT_COMPLETED")
    require(
        config["source"] == "load"
        and config["execution_driver"] == "queued"
        and config["concurrency"] == 2
        and config["budget_usd"] == 0
        and run["counts"]["started"] == run["counts"]["finished"] == 2
        and sorted(row["case_id"] for row in run["results"]) == CASE_IDS,
        "LOAD_PLAN_OR_COUNTS_MISMATCH",
    )
    operations = []
    for row in run["results"]:
        actual = row["actual"] or {}
        require(
            actual.get("observed_source") == "application_load"
            and actual.get("model_audit_complete") is True
            and actual.get("model_attempts") == []
            and row["machine_status"] not in {"FAILED", "ERROR", "TIMEOUT"},
            "LOAD_CASE_EVIDENCE_INVALID",
        )
        samples = actual.get("load_operations") or []
        require(
            len(samples) == READS_PER_CASE
            and all(
                item["operation"] == "case_and_event_read"
                and item["ok"] is True
                and type(item["elapsed_ms"]) is int
                and item["elapsed_ms"] >= 0
                for item in samples
            ),
            "LOAD_READS_NOT_ALL_SUCCESSFUL",
        )
        operations.extend(samples)
        count = verify_objects(engine, settings, row)
        next(item for item in report["cases"] if item["case_id"] == row["case_id"])[
            "s3_objects_verified"
        ] = count
    success, latency = metrics["load_operation_success"], metrics["load_latency_p95"]
    count = len(operations)
    p95 = sorted(item["elapsed_ms"] for item in operations)[(95 * count + 99) // 100 - 1]
    passed = sum(row["status"] == "PASSED" for row in run["results"])
    task = metrics["task_success"]
    require(
        (success["numerator"], success["denominator"], success["value"]) == (count, count, 1)
        and latency["value"] == p95
        and metrics["load_peak_concurrency"]["value"] == 2
        and (task["numerator"], task["denominator"], task["value"]) == (passed, 2, passed / 2),
        "LOAD_METRIC_MISMATCH",
    )
    report.update(status="PASSED", model_attempts=0)
    print("Load complete. Checked PostgreSQL isolation, S3 bytes and HTTP metrics.", flush=True)


@contextmanager
def isolated_http(engine, service):
    app = FastAPI()
    app.state.engine, app.state.evaluation = engine, service
    app.include_router(router, prefix="/api")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
        )
        server_thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            require(server.started, "ISOLATED_HTTP_START_FAILED")
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)
            require(not server_thread.is_alive(), "ISOLATED_HTTP_DID_NOT_STOP")


def durable_audit(engine, result_id):
    with Session(isolated(engine, result_id)) as db:
        run = db.scalars(select(AgentRun)).one()
        events = list(
            db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))
        )
        require(run.status == "WAITING_INPUT" and bool(events), "FIRST_GRAPH_AUDIT_NOT_PERSISTED")
        return [
            {
                "seq": row.seq,
                "kind": row.kind,
                "payload": row.payload,
                "created_at": row.created_at.isoformat(),
            }
            for row in events
        ]


def verify_cancel(engine, settings, token, report):
    settings = settings.model_copy(update={"model_enabled": False})
    dsn = make_url(settings.database_url).set(drivername="postgresql")
    with PostgresSaver.from_conn_string(dsn.render_as_string(hide_password=False)) as saver:
        service = EvaluationService(engine, settings, execution_driver="inline")
        runner = EvaluationRunner(engine, settings, saver)
        reached, released = Event(), Event()
        actual_apply = ScriptReplay._apply

        def gated_apply(replay, step):
            result = actual_apply(replay, step)
            if replay.case["id"] == CASE_IDS[0] and step["id"] == "e02":
                reached.set()
                require(released.wait(timeout=35), "CANCEL_GATE_RELEASE_TIMEOUT")
            return result

        with (
            isolated_http(engine, service) as base,
            client_for(base, token) as client,
            patch.object(ScriptReplay, "_apply", gated_apply),
            ThreadPoolExecutor(max_workers=1) as execution,
        ):
            experiment = request(client, EVALUATION, expected=201, body=plan("fixture", 1))
            report["experiment_id"] = experiment["id"]
            path = f"{EVALUATION}/{experiment['id']}"
            work = execution.submit(service.process, experiment["id"], runner)
            try:
                require(reached.wait(timeout=45), "CANCEL_FIXTURE_GATE_NOT_REACHED")
                running = request(client, path)
                rows = {row["case_id"]: row for row in running["results"]}
                first, second = (rows[case] for case in CASE_IDS)
                require(
                    running["status"] == "RUNNING"
                    and first["status"] == "RUNNING"
                    and second["status"] == "QUEUED"
                    and second["started_at"] is None
                    and running["config"]["execution_driver"] == "inline",
                    "CANCEL_DID_NOT_TARGET_ACTIVE_EXPERIMENT",
                )
                before = durable_audit(engine, first["id"])
                require(
                    not inspect(engine).has_schema(namespace(second["id"])),
                    "SECOND_CASE_SCHEMA_EXISTS_BEFORE_CANCEL",
                )
                print(
                    "First case paused after a committed graph audit. Cancelling over HTTP.",
                    flush=True,
                )
                body, key = {"expected_version": running["version"]}, str(uuid4())
                cancelled = request(client, path + "/cancel", body=body, key=key)
                replay = request(client, path + "/cancel", body=body, key=key)
                require(
                    cancelled["status"] == "CANCELLED" and cancelled == replay,
                    "HTTP_CANCEL_OR_REPLAY_MISMATCH",
                )
            finally:
                released.set()
            work.result(timeout=35)
            finished = request(client, path)
        after = durable_audit(engine, first["id"])
        results = {row["case_id"]: row for row in finished["results"]}
        active, unstarted = (results[case] for case in CASE_IDS)
        report.update(
            experiment_status=finished["status"],
            counts=finished["counts"],
            case_statuses={row["case_id"]: row["status"] for row in finished["results"]},
            first_case_durable_audit_count=len(before),
            first_case_durable_audit_identical=before == after,
            first_case_durable_audit_sha256=sha256(
                json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            second_case_schema_created=inspect(engine).has_schema(namespace(unstarted["id"])),
        )
        no_attempts(engine, active["id"])
        published = [event for event in active["trace"] if "seq" in event]
        require(
            finished["status"] == "CANCELLED"
            and finished["counts"]["started"] == finished["counts"]["finished"] == 1
            and finished["counts"]["not_run"] == 1
            and active["machine_status"] == "ERROR"
            and active["failure_reason"] == "EXPERIMENT_CANCELLED"
            and active["actual"]["model_attempts"] == []
            and set(active["actual"]["events"]) == {"e01", "e02"}
            and before == after == published
            and unstarted["machine_status"] == "NOT_RUN"
            and unstarted["failure_reason"] == "EXPERIMENT_CANCELLED"
            and unstarted["started_at"] is None
            and unstarted["actual"] is None
            and unstarted["trace"] == []
            and report["second_case_schema_created"] is False,
            "CANCEL_AUDIT_RETENTION_OR_NEXT_CASE_FENCE_FAILED",
        )
        report.update(status="PASSED", model_attempts=0, idempotent_http_cancel=True)
        print(
            "Cancellation retained the first audit and left the second case unstarted.", flush=True
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private evidence JSON")
    args = parser.parse_args()
    report = {
        "status": "FAILED",
        "load": {
            "status": "NOT_RUN",
            "source": "regular_test_http_celery_postgresql_s3",
            "read_latency_scope": "direct_business_service_postgresql_not_http_transport",
        },
        "cancel": {
            "status": "NOT_RUN",
            "source": "isolated_tcp_http_inline_fixture_gate_postgresql",
            "celery": False,
            "paid_model": False,
        },
    }
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new private evidence file.\n")
    with os.fdopen(descriptor, "w") as stream:
        try:
            settings = load_settings("test")
            require(
                not settings.container and settings.object_endpoint == "http://127.0.0.1:58351",
                "HOST_TEST_OBJECT_STORAGE_REQUIRED",
            )
            engine = guarded_engine(settings, test_only=True)
            try:
                credentials = json.loads((ROOT / ".runtime/users-test.json").read_text())
                token = next(row["token"] for row in credentials if row["role"] == "tester")
                with Session(engine) as db:
                    tester_id = db.scalar(
                        select(User.id).where(
                            User.role == "tester",
                            User.enabled.is_(True),
                            User.token_hash == token_hash(token),
                        )
                    )
                require(tester_id is not None, "TEST_TESTER_IDENTITY_NOT_FOUND")
                report["load"]["status"] = "FAILED"
                with client_for(BASE, token) as client:
                    verify_load(engine, settings, client, tester_id, report["load"])
                report["cancel"]["status"] = "FAILED"
                verify_cancel(engine, settings, token, report["cancel"])
            finally:
                engine.dispose()
            report["status"] = "PASSED"
        except CheckFailed as error:
            report["failure"] = str(error)
        except Exception as error:  # noqa: BLE001 - reports must never expose credentials or bodies
            report["failure"] = type(error).__name__
        finally:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
