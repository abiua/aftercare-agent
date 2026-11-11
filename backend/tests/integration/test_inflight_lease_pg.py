"""Real PG lease takeover with a held, zero-network model response.

Run serially on the migrated test profile with its regular worker and beat
paused: AFTERCARE_INFLIGHT_LEASE_PG=1 pytest -q -s tests/integration/test_inflight_lease_pg.py
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from threading import Event
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, event, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.dispatch import DeliveryJournal, DispatchEvent, DispatchJob
from aftercare.evaluation.runner import EvaluationRunner
from aftercare.evaluation.service import EvaluationService, ExperimentInput
from aftercare.evaluation.tables import EvaluationMutation, EvaluationResult, Experiment
from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_INFLIGHT_LEASE_PG") != "1",
        reason="requires explicit guarded test PG and paused worker/beat",
    ),
]
USAGE = {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}


class NoMaterialStorage:
    def put(self, key, data, content_type):
        raise AssertionError("This fixture must not upload materials")

    def get(self, key, expected_size):
        raise AssertionError("This fixture must not read material bytes")

    def presign(self, key, content_type):
        raise AssertionError("This fixture must not sign material links")


@pytest.fixture(scope="module")
def evidence():
    path = os.environ.get("AFTERCARE_INFLIGHT_LEASE_REPORT")
    report = {
        "source": "real_postgresql_production_evaluation_and_lease_with_controlled_model_response",
        "broker_and_provider_network": "NOT_USED",
        "material_storage": "REJECTS_ALL_ACCESS",
        "status": "IN_PROGRESS",
        "scenarios": [],
        "cleanup_verified_count": 0,
    }
    if path is None:
        yield report
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            yield report
        finally:
            report["status"] = (
                "PASSED"
                if len(report["scenarios"]) == report["cleanup_verified_count"] == 2
                else "INCOMPLETE"
            )
            json.dump(report, stream, indent=2)
            stream.write("\n")


@pytest.fixture
def database(evidence):
    settings = load_settings("test")
    assert not settings.container
    engine = guarded_engine(settings, test_only=True)

    @event.listens_for(engine, "connect")
    def bound_test_session_waits(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '12s'")
            cursor.execute("SET lock_timeout = '5s'")
        connection.commit()

    engine.dispose()  # apply the timeouts even to the original identity-check connection
    tester = UserContext(user_id=str(uuid4()), role="tester")
    try:
        with Session(engine) as db, db.begin():
            active = db.scalar(
                select(DispatchJob.id)
                .where(DispatchJob.state.in_({"READY", "PUBLISHING", "PUBLISHED", "RUNNING"}))
                .limit(1)
            )
            assert active is None, (
                "Test delivery journal is not quiet; stop and drain test worker/beat"
            )
            assert (
                db.scalar(
                    select(Experiment.id)
                    .where(Experiment.status.in_({"QUEUED", "RUNNING"}))
                    .limit(1)
                )
                is None
            ), "Test evaluations are not quiet"
            db.add(
                User(
                    id=tester.user_id,
                    token_hash=sha256(tester.user_id.encode()).hexdigest(),
                    role="tester",
                    display_name="Isolated lease acceptance",
                    enabled=True,
                )
            )
        print("Guarded test PG preflight passed. Broker and model network are unused.", flush=True)
        yield engine, settings, tester
    finally:
        with Session(engine) as db, db.begin():
            experiments = list(
                db.scalars(select(Experiment.id).where(Experiment.created_by == tester.user_id))
            )
            results = list(
                db.scalars(
                    select(EvaluationResult.id).where(
                        EvaluationResult.experiment_id.in_(experiments)
                    )
                )
            )
            jobs = list(
                db.scalars(
                    select(DispatchJob.id).where(
                        DispatchJob.kind == "evaluation", DispatchJob.target_id.in_(experiments)
                    )
                )
            )
            for result_id in results:
                schema = "eval_" + UUID(result_id).hex
                db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            db.execute(delete(DispatchEvent).where(DispatchEvent.job_id.in_(jobs)))
            db.execute(delete(DispatchJob).where(DispatchJob.id.in_(jobs)))
            db.execute(
                delete(EvaluationMutation).where(EvaluationMutation.actor_id == tester.user_id)
            )
            db.execute(delete(EvaluationResult).where(EvaluationResult.id.in_(results)))
            db.execute(delete(Experiment).where(Experiment.id.in_(experiments)))
            db.execute(delete(User).where(User.id == tester.user_id))
        assert all(not inspect(engine).has_schema("eval_" + UUID(rid).hex) for rid in results)
        evidence["cleanup_verified_count"] += 1
        engine.dispose()


def fixture_model(transport):
    spec = ModelSpec(
        alias="economy",
        provider="fixture",
        model="held-response",
        endpoint="http://127.0.0.1:9",
        api_key_env="UNUSED_FIXTURE_KEY",
        input_usd_per_million="1",
        output_usd_per_million="1",
        price_date=date(2026, 11, 10),
        source="controlled evaluation lease fixture",
    )
    return ModelRunner(
        Registry(version="lease-fixture", models=[spec], allow_unverified=True), transport=transport
    )


def graph_snapshot(scoped, saver):
    with Session(scoped) as db:
        run = db.scalars(select(AgentRun)).one()
        return {
            "run": (run.id, run.thread_id, run.cycle, run.status, run.reason, run.output),
            "events": [
                (row.seq, row.kind, row.payload)
                for row in db.scalars(select(RunEvent).order_by(RunEvent.seq))
            ],
            "checkpoints": deepcopy((dict(saver.storage), dict(saver.writes), dict(saver.blobs))),
        }


def raw_attempt(scoped):
    with Session(scoped) as db:
        row = db.scalars(select(ModelAttempt)).one()
        return {
            "identity": (row.run_id, row.cycle, row.logical_id, row.ordinal),
            "status": row.status,
            "usage": row.usage,
            "estimated_cost_usd": row.estimated_cost_usd,
        }


def detach_only_test_advisory_connection(engine, pid, lease):
    # The PID was captured on this thread's exact production pg_try_advisory_lock.
    # Verify the same PG lock key is still held by that backend before terminating it.
    with engine.begin() as conn:
        ended = conn.scalar(
            text("""
                SELECT pg_terminate_backend(pid, 5000)
                FROM pg_locks
                WHERE pid = :pid AND pid <> pg_backend_pid()
                    AND locktype = 'advisory' AND granted AND objsubid = 1
                    AND classid::bigint = :high AND objid::bigint = :low
                    AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            """),
            {"pid": pid, "high": (lease >> 32) & 0xFFFFFFFF, "low": lease & 0xFFFFFFFF},
        )
        assert ended is True


@pytest.mark.parametrize(
    "usage_first", [True, False], ids=["usage-before-recovery", "usage-after-recovery"]
)
def test_late_usage_after_cancel_and_pg_lease_replacement(database, evidence, usage_first):
    engine, settings, tester = database
    entered, release = Event(), Event()
    calls = []

    def transport(*_args):
        calls.append(1)
        entered.set()
        assert release.wait(45), "Fixture response gate was never released"
        return AIMessage(
            content="",
            tool_calls=[{"name": "read_case_facts", "args": {}, "id": "fixture-tool"}],
            usage_metadata=USAGE,
        )

    model = fixture_model(transport)
    settings = settings.model_copy(update={"model_enabled": True, "embedding_enabled": False})
    service = EvaluationService(engine, settings, model=model)
    request = ExperimentInput(
        name="Controlled PostgreSQL lease acceptance",
        dataset_id="v0.2",
        split="dev",
        mode="single",
        strategy="economy",
        repetitions=1,
        concurrency=1,
        max_calls=2,
        budget_usd="0.10",
        source="live",
        case_ids=["AC02-C081"],
    )
    plan = service.create(tester, request, str(uuid4()))
    experiment_id, result_id = plan["id"], plan["results"][0]["id"]
    assert plan["config"]["execution_driver"] == "queued"
    namespace = "eval_" + UUID(result_id).hex
    assert not inspect(engine).has_schema(namespace)
    scoped = engine.execution_options(schema_translate_map={None: namespace})
    lease = int.from_bytes(
        sha256(f"eval:{experiment_id}".encode()).digest()[:8], "big", signed=True
    )

    # A real production journal, with only time advancement controlled by the test.
    timestamp = [datetime.now(UTC)]
    journal = DeliveryJournal(engine, clock=lambda: timestamp[0], lease_seconds=10)
    job_id = journal.ensure("evaluation", experiment_id)
    old_owner = journal.claim(job_id)
    assert old_owner
    old_engine = engine.execution_options()
    advisory_pids = []

    @event.listens_for(old_engine, "after_cursor_execute")
    def remember_own_advisory_backend(conn, cursor, statement, params, context, executemany):
        if "pg_try_advisory_lock" in statement and params.get("lease") == lease:
            advisory_pids.append(conn.connection.driver_connection.info.backend_pid)

    saver = InMemorySaver()
    old = EvaluationService(
        old_engine,
        settings,
        model=model,
        attempt_admission=lambda db: journal.owned(job_id, old_owner, session=db),
    )
    storage = NoMaterialStorage()
    old_runner = EvaluationRunner(old_engine, settings, saver, model=model, storage=storage)

    with ThreadPoolExecutor(max_workers=1) as pool:
        old_work = pool.submit(old.process, experiment_id, old_runner)
        try:
            assert entered.wait(25), "The old worker did not enter its controlled model response"
            assert len(advisory_pids) == 1
            started = raw_attempt(scoped)
            assert started["status"] == "STARTED" and started["usage"] is None
            before = graph_snapshot(scoped, saver)
            assert before["checkpoints"][0] and before["run"][3] == "RUNNING"
            active = service.experiment(tester, experiment_id)
            assert active["status"] == "RUNNING" and active["results"][0]["status"] == "RUNNING"
            canceled = service.cancel(
                tester, experiment_id, expected_version=active["version"], key=str(uuid4())
            )
            assert (
                canceled["status"] == "CANCELLED" and canceled["results"][0]["status"] == "RUNNING"
            )

            timestamp[0] += timedelta(seconds=11)
            assert not journal.renew(job_id, old_owner)
            wake = journal.reserve_publish(job_id)
            assert wake and wake["generation"] == 0
            journal.published(job_id, wake["publication"])
            new_owner = journal.claim(job_id)
            assert new_owner and new_owner != old_owner

            new = EvaluationService(
                engine,
                settings,
                model=model,
                attempt_admission=lambda db: journal.owned(job_id, new_owner, session=db),
            )
            new_runner = EvaluationRunner(engine, settings, saver, model=model, storage=storage)
            new.process(experiment_id, new_runner)
            assert service.experiment(tester, experiment_id) == canceled

            if usage_first:
                release.set()
                old_work.result(timeout=20)
                assert raw_attempt(scoped)["usage"] == USAGE
            else:
                detach_only_test_advisory_connection(engine, advisory_pids[0], lease)
                assert not old_work.done()
                assert raw_attempt(scoped)["status"] == "STARTED"

            new.process(experiment_id, new_runner)
            recovered = service.experiment(tester, experiment_id)
            assert recovered["status"] == "CANCELLED"
            outcome = recovered["results"][0]
            assert (
                outcome["status"] == "ERROR" and outcome["failure_reason"] == "WORKER_INTERRUPTED"
            )
            (audit,) = outcome["actual"]["model_attempts"]
            supplemental_before = service.supplemental_usage(tester, result_id)
            assert supplemental_before["status"] == "AVAILABLE"
            assert supplemental_before["unavailable_reason"] is None
            assert supplemental_before["unmatched_attempt_count"] == 0
            assert supplemental_before["attempts"] == []
            if usage_first:
                assert audit["status"] == "SUCCEEDED" and audit["usage"] == USAGE
                assert audit["estimated_cost_usd"] == "0.00002"
            else:
                assert audit["status"] == "UNKNOWN" and audit["persisted_status"] == "STARTED"
                assert audit["usage"] is None and audit["estimated_cost_usd"] is None
                release.set()
                with pytest.raises(DBAPIError) as lost_connection:
                    old_work.result(timeout=20)
                assert lost_connection.value.connection_invalidated

            assert raw_attempt(scoped) == {
                "identity": started["identity"],
                "status": "SUCCEEDED",
                "usage": USAGE,
                "estimated_cost_usd": "0.00002",
            }
            supplemental_after = service.supplemental_usage(tester, result_id)
            assert supplemental_after["status"] == "AVAILABLE"
            assert supplemental_after["unavailable_reason"] is None
            assert supplemental_after["unmatched_attempt_count"] == 0
            if usage_first:
                assert supplemental_after["attempts"] == []
            else:
                (late,) = supplemental_after["attempts"]
                assert (
                    tuple(late[key] for key in ("run_id", "cycle", "logical_id", "ordinal"))
                    == (started["identity"])
                )
                assert (late["status"], late["usage"], late["estimated_cost_usd"]) == (
                    "SUCCEEDED",
                    USAGE,
                    "0.00002",
                )
                assert late["finished_at"]
                assert (
                    late["snapshot_status"],
                    late["snapshot_usage"],
                    late["snapshot_estimated_cost_usd"],
                ) == ("UNKNOWN", None, None)
            assert service.experiment(tester, experiment_id) == recovered
            assert graph_snapshot(scoped, saver) == before
            assert not journal.finish(job_id, old_owner)
            assert journal.finish(job_id, new_owner)
            assert journal.claim(job_id) is None and calls == [1]
            with Session(engine) as db:
                job = db.get_one(DispatchJob, job_id)
                assert (job.state, job.generation, job.claims, job.failures) == ("DONE", 0, 2, 1)
                states = list(
                    db.scalars(select(DispatchEvent.state).where(DispatchEvent.job_id == job_id))
                )
                assert states.count("LEASE_EXPIRED") == 1
            print(
                f"snapshot={audit['status']} raw=SUCCEEDED usage={USAGE['total_tokens']} "
                f"calls={len(calls)} claims=2 advisory_connection_terminated={not usage_first}",
                flush=True,
            )
            evidence["scenarios"].append(
                {
                    "order": "usage_before_recovery" if usage_first else "usage_after_recovery",
                    "advisory_connection_terminated": not usage_first,
                    "experiment_status": recovered["status"],
                    "result_status": outcome["status"],
                    "failure_reason": outcome["failure_reason"],
                    "experiment_id": experiment_id,
                    "result_id": result_id,
                    "dispatch_job_id": job_id,
                    "old_owner": old_owner,
                    "new_owner": new_owner,
                    "inner_run_status": before["run"][3],
                    "evaluation_snapshot": {
                        key: audit.get(key) for key in ("status", "usage", "estimated_cost_usd")
                    },
                    "original_attempt": raw_attempt(scoped),
                    "supplemental_usage_before_late_release": supplemental_before,
                    "supplemental_usage_final": supplemental_after,
                    "graph_events_and_result_unchanged_by_stale_owner": True,
                    "fixture_transport_calls": len(calls),
                    "lease_claims": 2,
                    "isolated_namespace": namespace,
                }
            )
        finally:
            release.set()
