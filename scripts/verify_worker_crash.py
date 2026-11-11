"""Test a real solo worker SIGKILL and natural lease recovery on test only.

Stop the regular test worker first and leave the test API and beat running.
From the project root run:
    PYTHONPATH=backend/src PYTHONPYCACHEPREFIX="$HOME/.cache/aftercare-agent/pycache" \
      "$HOME/.cache/aftercare-agent/backend-venv/bin/python" scripts/verify_worker_crash.py \
      --output reports/worker-crash.json
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from aftercare.auth import token_hash
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from aftercare.dispatch import DispatchEvent, DispatchJob
from aftercare.evaluation.tables import EvaluationResult, Experiment
from aftercare.intelligence.tables import AgentRun, ModelAttempt
from aftercare.queue import make_queue
from sqlalchemy import select, text
from sqlalchemy.orm import Session

BASE = "http://127.0.0.1:8022"
TEST_REDIS = "redis://127.0.0.1:56380/0"
RECOVERY_SECONDS = 150
WORKER_BOOT = """
import sys
from aftercare import runtime
from aftercare.worker import app

load = runtime.load_settings
def offline(profile):
    if profile != 'test':
        raise ValueError('TEST_PROFILE_REQUIRED')
    return load(profile).model_copy(update={'model_enabled': False})
runtime.load_settings = offline
app.worker_main(sys.argv[1:])
"""


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def progress(message):
    print(message, flush=True)


def request(client, route, *, expected=200, body=None):
    if body is None:
        response = client.get("/api" + route)
    else:
        response = client.post("/api" + route, json=body, headers={"Idempotency-Key": str(uuid4())})
    require(
        response.status_code == expected, f"EXPECTED_HTTP_{expected}_GOT_{response.status_code}"
    )
    return response.json()


def page_items(client, resource):
    for offset in range(0, 3000, 300):
        rows = request(client, f"/{resource}?limit=300&offset={offset}")["items"]
        yield from rows
        if len(rows) < 300:
            return
    raise CheckFailed("TEST_FIXTURE_SCAN_LIMIT")


def prepare_run(client, report):
    used = {row["order_id"] for row in page_items(client, "cases")}
    order = next(
        (
            row
            for row in page_items(client, "orders")
            if row["id"] not in used
            and row["status"] == "DELIVERED"
            and row["payment_status"] == "CONFIRMED"
            and not row["refunded_cents"]
        ),
        None,
    )
    require(order is not None, "UNUSED_DELIVERED_TEST_ORDER_NOT_FOUND")
    case = request(
        client,
        "/cases",
        expected=201,
        body={
            "order_id": order["id"],
            "intent": "damaged",
            "description": "测试进程中断恢复，商品破损，尚未提交商品照和外包装照片。",
            "expected_version": order["version"],
        },
    )
    case_id = str(UUID(case["id"]))
    report["case_id"] = case_id
    case_path = f"/cases/{case_id}"
    detail = request(client, case_path)
    eligibility = detail["eligibility"]
    require(
        eligibility["status"] == "WAITING_INPUT"
        and set(eligibility["missing_materials"]) == {"ITEM", "PARCEL"}
        and not detail["materials"],
        "MISSING_MATERIAL_GATE_NOT_ESTABLISHED",
    )
    run = request(
        client,
        case_path + "/runs",
        expected=202,
        body={"expected_version": case["version"], "mode": "multi", "strategy": "routed"},
    )
    run_id = str(UUID(run["id"]))
    report.update(run_id=run_id, thread_id=run["thread_id"])
    require(run["status"] == "QUEUED" and run["cycle"] == 0, "RUN_ALREADY_CONSUMED")
    return case_path + "/runs/" + run_id, run


def require_quiet(engine, queue):
    with Session(engine) as db:
        for identifier, state, active in (
            (AgentRun.id, AgentRun.status, {"QUEUED", "RUNNING"}),
            (Experiment.id, Experiment.status, {"QUEUED", "RUNNING"}),
            (EvaluationResult.id, EvaluationResult.status, {"RUNNING"}),
            (DispatchJob.id, DispatchJob.state, {"READY", "PUBLISHING", "PUBLISHED", "RUNNING"}),
        ):
            pending = db.scalar(select(identifier).where(state.in_(active)).limit(1))
            require(pending is None, "TEST_DATABASE_HAS_ACTIVE_WORK")
    require(not queue.control.inspect(timeout=2).ping(), "TEST_WORKER_STILL_RESPONDING")


def start_worker(logs, index):
    hostname = f"aftercare-crash-check-{index}-{uuid4().hex[:12]}"
    path = logs / f"worker-{index}.log"
    env = {
        **os.environ,
        "AFTERCARE_PROFILE": "test",
        "PYTHONPATH": str(ROOT / "backend/src"),
    }
    env.pop("AFTERCARE_OPENAI_KEY", None)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                WORKER_BOOT,
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--queues=aftercare-test",
                f"--hostname={hostname}@%h",
                "--loglevel=INFO",
                "--without-mingle",
                "--without-gossip",
            ],
            cwd=ROOT / "backend",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    return worker, path


@contextmanager
def private_worker(logs, index):
    worker, log = start_worker(logs, index)
    try:
        yield worker, log
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)


def await_value(worker, until, check, failure, *, announce=False):
    started = time.monotonic()
    next_progress = started + 15
    while time.monotonic() < until:
        require(worker.poll() is None, "PRIVATE_WORKER_EXITED_EARLY")
        value = check()
        if value is not None:
            return value
        if announce and time.monotonic() >= next_progress:
            progress(f"Waiting for beat recovery ({int(time.monotonic() - started)}s).")
            next_progress += 15
        time.sleep(0.25)
    raise CheckFailed(failure)


def job_for(engine, run_id):
    with Session(engine) as db:
        return db.scalar(
            select(DispatchJob).where(
                DispatchJob.kind == "agent",
                DispatchJob.target_id == run_id,
                DispatchJob.generation == 0,
            )
        )


def first_claim(engine, run_id, holder):
    job = job_for(engine, run_id)
    if job is None:
        return None
    require(job.claims <= 1, "MORE_THAN_ONE_INITIAL_CLAIM")
    if job.state != "RUNNING" or job.claims != 1:
        return None
    with engine.connect() as conn:
        blocked = list(
            conn.scalars(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() AND :holder = ANY(pg_blocking_pids(pid)) "
                    "AND query ILIKE '%agent_runs%' AND query ILIKE '%FOR UPDATE%'"
                ),
                {"holder": holder},
            )
        )
    if not blocked:
        return None
    require(job.owner and job.lease_until, "INITIAL_CLAIM_MISSING_LEASE")
    return job, blocked


def kill_claiming_worker(engine, run, logs, report):
    with Session(engine) as db, db.begin():
        lock_holder = db.scalar(text("SELECT pg_backend_pid()"))
        stored = db.get(AgentRun, run["id"], with_for_update=True)
        require(
            stored is not None and stored.status == "QUEUED" and stored.cycle == 0,
            "AGENT_RUN_NOT_QUEUED_BEFORE_LOCK",
        )
        with private_worker(logs, 1) as (worker, _):
            report["killed_worker_pid"] = worker.pid
            progress(f"Started the first test worker (PID {worker.pid}).")
            job, blocked = await_value(
                worker,
                time.monotonic() + 35,
                lambda: first_claim(engine, run["id"], lock_holder),
                "INITIAL_CLAIM_OR_AGENT_ROW_BLOCK_UNOBSERVED",
            )
            worker.kill()
            killed_at = time.monotonic()
            require(worker.wait(timeout=5) == -signal.SIGKILL, "SIGKILL_EXIT_NOT_OBSERVED")
            report.update(
                killed_worker_exit=-signal.SIGKILL,
                job_id=job.id,
                lease_owner_before=job.owner,
                lease_until_before=job.lease_until.isoformat(),
                blocked_postgresql_backends=blocked,
            )
            progress(f"SIGKILL confirmed for PID {worker.pid}. Releasing the run row lock.")
    remaining = job_for(engine, run["id"])
    require(
        remaining is not None
        and remaining.state == "RUNNING"
        and remaining.claims == 1
        and remaining.owner == job.owner
        and remaining.lease_until == job.lease_until,
        "FIRST_LEASE_NOT_RETAINED_AFTER_SIGKILL",
    )
    return job, killed_at


def finished_job(engine, run_id):
    job = job_for(engine, run_id)
    if job is not None and job.state in {"DONE", "FAILED", "CANCELLED"}:
        require(job.state == "DONE", "RECOVERY_JOB_TERMINATED_WITHOUT_SUCCESS")
        return job
    return None


def recovery_events(engine, initial, recovered, report):
    require(recovered.id == initial.id and recovered.claims == 2, "RECOVERY_CLAIM_COUNT_MISMATCH")
    require(recovered.failures >= 1 and recovered.lease_until is None, "RECOVERY_JOURNAL_MISMATCH")
    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(DispatchEvent)
                .where(DispatchEvent.job_id == recovered.id)
                .order_by(DispatchEvent.created_at, DispatchEvent.id)
            )
        )
    expired = [row for row in rows if row.state == "LEASE_EXPIRED"]
    claims = [row for row in rows if row.state == "RUNNING"]
    republished = [row for row in rows if row.state == "PUBLISHING"]
    require(
        len(expired) == 1
        and expired[0].code == "WORKER_LEASE_EXPIRED"
        and len(claims) == 2
        and claims[0].owner == initial.owner
        and claims[1].owner != initial.owner
        and expired[0].created_at >= initial.lease_until
        and claims[1].created_at >= initial.lease_until
        and any(
            expired[0].created_at <= row.created_at <= claims[1].created_at for row in republished
        )
        and 59 <= (initial.lease_until - claims[0].created_at).total_seconds() <= 60,
        "NATURAL_LEASE_EXPIRY_NOT_PROVEN",
    )
    report.update(
        job_state=recovered.state,
        job_claims=recovered.claims,
        job_failures=recovered.failures,
        lease_expired_at=expired[0].created_at.isoformat(),
        second_claim_at=claims[1].created_at.isoformat(),
        first_lease_duration_seconds=round(
            (initial.lease_until - claims[0].created_at).total_seconds(), 3
        ),
        dispatch_states=[row.state for row in rows],
    )


def run_snapshot(client, engine, path, run):
    current = request(client, path)
    require(
        current["id"] == run["id"]
        and current["case_id"] == run["case_id"]
        and current["thread_id"] == run["thread_id"]
        and current["cycle"] == 0
        and current["status"] == "WAITING_INPUT"
        and set(current["output"]["missing_materials"]) == {"ITEM", "PARCEL"},
        "RECOVERED_RUN_IDENTITY_OR_GATE_MISMATCH",
    )
    events = request(client, path + "/events")
    require(
        bool(events) and len(events) < 200 and all(row["kind"] != "attempt" for row in events),
        "RECOVERY_TRACE_INCOMPLETE_OR_MODEL_ATTEMPT_FOUND",
    )
    with Session(engine) as db:
        attempt = db.scalar(
            select(ModelAttempt.run_id).where(ModelAttempt.run_id == run["id"]).limit(1)
        )
        stored = db.get(AgentRun, run["id"])
        require(attempt is None and stored.event_seq == len(events), "RECOVERY_AUDIT_MISMATCH")
    return current, events


def stale_delivery(queue, worker, log, client, engine, path, run, recovered, report):
    before = run_snapshot(client, engine, path, run)
    task_id = str(uuid4())
    queue.send_task(
        "aftercare.process", args=[run["id"]], kwargs={"job_id": recovered.id}, task_id=task_id
    )
    marker = f"Task aftercare.process[{task_id}] succeeded".encode()
    await_value(
        worker,
        time.monotonic() + 20,
        lambda: True if marker in log.read_bytes() else None,
        "STALE_REDELIVERY_COMPLETION_UNOBSERVED",
    )
    after = run_snapshot(client, engine, path, run)
    job = job_for(engine, run["id"])
    require(
        before == after
        and job is not None
        and job.state == "DONE"
        and (job.claims, job.failures, job.publications)
        == (recovered.claims, recovered.failures, recovered.publications),
        "STALE_REDELIVERY_CHANGED_RUN_OR_JOB",
    )
    report.update(
        stale_redelivery_task_id=task_id,
        stale_redelivery_no_effect=True,
        recovered_run_status=after[0]["status"],
        recovered_events=len(after[1]),
        model_attempts=0,
    )


def verify(engine, queue, logs, report):
    require_quiet(engine, queue)
    progress("Verified the test database is idle and no test worker responded.")
    credentials = json.loads((ROOT / ".runtime/users-test.json").read_text())
    token = next(row["token"] for row in credentials if row["role"] == "support")
    with Session(engine) as db:
        support_id = db.scalar(
            select(User.id).where(
                User.role == "support", User.enabled.is_(True), User.token_hash == token_hash(token)
            )
        )
        require(support_id is not None, "TEST_SUPPORT_IDENTITY_NOT_FOUND")
    with httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        follow_redirects=False,
        timeout=12,
    ) as client:
        actor = request(client, "/me")
        require(
            actor["user_id"] == support_id and actor["role"] == "support",
            "TEST_HTTP_IDENTITY_MISMATCH",
        )
        path, run = prepare_run(client, report)
        progress("Created a queued test run with both required photos missing.")
        initial, killed_at = kill_claiming_worker(engine, run, logs, report)
        with private_worker(logs, 2) as (worker, log):
            report["recovery_worker_pid"] = worker.pid
            require(worker.pid != report["killed_worker_pid"], "WORKER_PID_WAS_REUSED")
            progress(f"Started the recovery worker (PID {worker.pid}). Waiting for lease expiry.")
            recovered = await_value(
                worker,
                killed_at + RECOVERY_SECONDS,
                lambda: finished_job(engine, run["id"]),
                "NATURAL_RECOVERY_DEADLINE_EXCEEDED",
                announce=True,
            )
            report["elapsed_after_sigkill_seconds"] = round(time.monotonic() - killed_at, 3)
            recovery_events(engine, initial, recovered, report)
            progress("Lease expired and beat recovered the same job. Sending a stale duplicate.")
            stale_delivery(queue, worker, log, client, engine, path, run, recovered, report)
            progress("The stale task completed and left the run and journal unchanged.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New evidence JSON file")
    args = parser.parse_args()
    report = {
        "status": "FAILED",
        "source": "real_test_http_postgresql_redis_celery_solo",
        "private_worker_model_enabled": False,
        "maximum_recovery_seconds": RECOVERY_SECONDS,
    }
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new private evidence file.\n")
    with os.fdopen(descriptor, "w") as stream:
        try:
            settings = load_settings("test")
            require(
                not settings.container and settings.redis_url == TEST_REDIS,
                "HOST_TEST_REDIS_REQUIRED",
            )
            engine = guarded_engine(settings, test_only=True)
            try:
                logs = Path(tempfile.mkdtemp(prefix="worker-crash-", dir=ROOT / ".runtime"))
                report["private_logs"] = str(logs.relative_to(ROOT))
                verify(engine, make_queue("test"), logs, report)
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
