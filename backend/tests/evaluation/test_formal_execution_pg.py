"""Opt-in formal control acceptance on the test PG cluster, with no model transport."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_formal_execution import (
    CREATOR,
    NEXT,
    ORDER,
    TOKENS,
    fixture_arguments,
    fixture_model,
    outcome,
    run,
)

from aftercare.business.errors import BusinessError
from aftercare.business.rules import now
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.evaluation.formal import execution
from aftercare.evaluation.formal.execution import FormalEvaluationService
from aftercare.evaluation.progress import read_progress
from aftercare.evaluation.runner import EvaluationRunner, _isolated_engine, _namespace
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import (
    EvaluationProgress,
    EvaluationResult,
    EvaluationReview,
    Experiment,
)
from aftercare.intelligence.tables import AgentRun, ModelAttempt

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_FORMAL_EXECUTION_PG") != "1",
        reason="explicit isolated PostgreSQL acceptance required",
    ),
]


class AcceptanceService(FormalEvaluationService):
    def _runner(self, package):
        return self.runner


class ControlledRunner:
    def __init__(self, engine, settings, model):
        self.engine, self.settings, self.model = engine, settings, model
        self.opened = []
        self.calls = []
        self.control = None

    def recover_audit(self, result_id):
        return EvaluationRunner.recover_audit(self, result_id)

    def run(
        self,
        result_id,
        case,
        _reference,
        config,
        quota,
        *,
        attempt_admission,
        attempt_continuation,
        **_,
    ):
        self.calls.append(result_id)
        if self.control is None:
            return outcome([])
        self.opened.append(_namespace(result_id))
        with _isolated_engine(self.engine, result_id) as (isolated, _namespace_name):
            with Session(isolated) as db, db.begin():
                assert attempt_admission(db)
            return self.control(isolated, case, config, attempt_admission, attempt_continuation)


@pytest.fixture
def pg_system(tmp_path):
    settings = load_settings("test").model_copy(
        update={"model_enabled": True, "embedding_enabled": False, "embedding_model_path": None}
    )
    engine = guarded_engine(settings, test_only=True)
    schema = f"formal_exec_test_{uuid4().hex}"
    with engine.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    parent = engine.execution_options(schema_translate_map={None: schema})
    runner = ControlledRunner(engine, settings, fixture_model())
    service = AcceptanceService(parent, settings, None, model=runner.model)
    service.runner = runner
    try:
        for model in (User, Experiment, EvaluationResult, EvaluationProgress, EvaluationReview):
            model.__table__.create(parent)
        with Session(parent) as db, db.begin():
            for actor in (CREATOR, NEXT):
                db.add(
                    User(
                        id=actor.user_id,
                        display_name=actor.user_id,
                        role="tester",
                        enabled=True,
                        token_hash=TOKENS[actor.user_id],
                    )
                )
        args, _ = fixture_arguments(tmp_path, service)
        args["freeze_sha256"] = sha256(uuid4().bytes).hexdigest()
        yield SimpleNamespace(
            service=service, args=args, parent=parent, engine=engine, runner=runner
        )
    finally:
        try:
            with engine.begin() as db:
                for target in (*runner.opened, schema):
                    db.execute(text(f'DROP SCHEMA IF EXISTS "{target}" CASCADE'))
                    assert db.scalar(
                        text("SELECT to_regnamespace(:target) IS NULL"), {"target": target}
                    )
        finally:
            engine.dispose()


def _report(system, actor=NEXT):
    return system.service.private_report(
        actor,
        freeze_sha256=system.args["freeze_sha256"],
        draft=system.args["draft"],
        credential_token_hash=TOKENS[actor.user_id],
    )


def test_two_testers_serialize_stable_campaign_and_ordinary_reads_stay_hidden(pg_system):
    entered, proceed = Event(), Event()

    def pause(_isolated, _case, _config, *_callbacks):
        pg_system.runner.control = None
        entered.set()
        assert proceed.wait(timeout=10)
        return outcome([])

    pg_system.runner.control = pause
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_run = executor.submit(run, pg_system)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(BusinessError) as error:
                run(pg_system, NEXT)
            assert error.value.code == "FORMAL_EXECUTION_BUSY"
        finally:
            proceed.set()
        first = first_run.result(timeout=15)
    assert first["status"] == "COMPLETED"
    assert run(pg_system, NEXT) == first
    assert len(pg_system.runner.calls) == 24
    detail = _report(pg_system)
    assert [block["formal_condition_key"] for block in detail["conditions"]] == ORDER
    with Session(pg_system.parent) as db:
        rows = list(db.scalars(select(Experiment)))
        assert len(rows) == 4 and {row.created_by for row in rows} == {CREATOR.user_id}
    ordinary = EvaluationService(pg_system.parent, pg_system.service.settings)
    assert ordinary.experiments(CREATOR) == []
    for condition in detail["conditions"]:
        with pytest.raises(BusinessError) as error:
            read_progress(pg_system.parent, condition["id"], 0, 100)
        assert error.value.status == 404


def _attempt(config, case, runner, *, status):
    stamp = now()
    model = runner.model.registry.models[0]
    run_id = str(uuid4())
    run_row = AgentRun(
        id=run_id,
        thread_id=run_id,
        case_id=case["id"],
        owner_id="owner",
        actor={"user_id": "support", "role": "support"},
        status="RUNNING",
        mode=config["mode"],
        strategy=config["strategy"],
        message=case["message"],
        case_version=1,
        cycle=0,
        event_seq=0,
        deadline=stamp + timedelta(minutes=1),
        created_at=stamp,
        updated_at=stamp,
    )
    settled = status == "SUCCEEDED"
    attempt = ModelAttempt(
        run_id=run_id,
        cycle=0,
        logical_id="fixture",
        ordinal=1,
        status=status,
        started_at=stamp,
        finished_at=stamp if settled else None,
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3} if settled else None,
        estimated_cost_usd="0.000003" if settled else None,
        metadata_json={
            "run_id": run_id,
            "alias": model.alias,
            "provider": model.provider,
            "model": model.model,
            "base_registry_digest": runner.model.base_registry_digest,
            "transport_evidence": {
                "version": "ac-attempt-transport-v1",
                "path": "DEFAULT_STREAM",
                "response": "SDK_RETURNED" if settled else "UNOBSERVED",
            },
        },
    )
    return run_row, attempt


@pytest.mark.parametrize("status", ["STARTED", "UNKNOWN", "SUCCEEDED"])
def test_actual_pg_raw_attempts_control_crash_recovery(pg_system, status):
    def interrupt(isolated, case, config, *_callbacks):
        pg_system.runner.control = None
        run_row, attempt = _attempt(config, case, pg_system.runner, status=status)
        with Session(isolated) as db, db.begin():
            db.add(run_row)
            db.flush()
            db.add(attempt)
        raise KeyboardInterrupt

    pg_system.runner.control = interrupt
    with pytest.raises(KeyboardInterrupt):
        run(pg_system)
    original_id = pg_system.runner.calls[0]
    resumed = run(pg_system, NEXT)
    private = _report(pg_system)["conditions"][0]
    row = private["results"][0]
    assert row["id"] == original_id
    assert row["machine_status"] == "ERROR" and row["failure_reason"] == "WORKER_INTERRUPTED"
    assert pg_system.runner.calls.count(original_id) == 1
    assert row["actual"]["model_attempts"][0]["status"] == (
        "SUCCEEDED" if status == "SUCCEEDED" else "UNKNOWN"
    )
    assert len(pg_system.runner.calls) == (24 if status == "SUCCEEDED" else 1)
    if status == "SUCCEEDED":
        assert resumed["status"] == "COMPLETED"
        assert private["recovery_accounting"][0]["known_cost_usd"] == "0.000003"
    else:
        assert resumed["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
        assert private["recovery_accounting"] == []


def _lease_pid(system, db):
    freeze = system.args["freeze_sha256"]
    key = int.from_bytes(sha256(f"eval:formal:{freeze}".encode()).digest()[:8], "big", signed=True)
    return db.execute(
        text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
            "AND classid::bigint = :high AND objid::bigint = :low "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ),
        {"high": (key >> 32) & 0xFFFFFFFF, "low": key & 0xFFFFFFFF},
    ).scalar_one()


@pytest.mark.parametrize("stop", ["revoked", "lease"])
def test_actual_pg_revocation_or_pinned_connection_loss_denies_later_admission(pg_system, stop):
    def stop_execution(isolated, case, config, admit, continuation):
        pg_system.runner.control = None
        if stop == "revoked":
            with Session(pg_system.parent) as db, db.begin():
                db.get_one(User, CREATOR.user_id).token_hash = "0" * 64
        else:
            run_row, attempt = _attempt(config, case, pg_system.runner, status="STARTED")
            with Session(isolated) as db, db.begin():
                db.add(run_row)
                db.flush()
                db.add(attempt)
            with pg_system.engine.begin() as db:
                pid = _lease_pid(pg_system, db)
                assert db.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})
        with Session(isolated) as db, db.begin():
            assert not continuation(db)
            assert not admit(db)
        return outcome([])

    pg_system.runner.control = stop_execution
    summary = run(pg_system)
    assert summary["status"] == "INCOMPLETE"
    assert summary["reason"] == (
        "FORMAL_EXECUTION_ACTOR_REVOKED" if stop == "revoked" else "FORMAL_EXECUTION_LEASE_LOST"
    )
    assert len(pg_system.runner.calls) == 1
    continued = run(pg_system, NEXT)
    assert continued["status"] == ("COMPLETED" if stop == "revoked" else "INCOMPLETE")


def test_lease_pid_query_failure_does_not_return_connection_with_advisory_lock(pg_system):
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    def fail_pid(_connection, _cursor, statement, parameters, _context, _many):
        if statement == "SELECT pg_backend_pid()":
            return "SELECT 1 / 0", parameters
        return statement, parameters

    event.listen(pg_system.parent, "before_cursor_execute", fail_pid, retval=True)
    try:
        with (
            pytest.raises(DBAPIError),
            execution._campaign_lease(pg_system.parent, pg_system.args["freeze_sha256"]),
        ):
            pytest.fail("A lease with an unverified PID cannot be used")
    finally:
        event.remove(pg_system.parent, "before_cursor_execute", fail_pid)
    # Independent connection, proving the first connection did not retain the lock.
    with (
        pg_system.parent.connect(),
        execution._campaign_lease(pg_system.parent, pg_system.args["freeze_sha256"]) as held,
    ):
        assert held and held()


def test_actual_pg_interleaves_every_case_and_reuses_stable_rows(pg_system):
    from test_formal_interleaving import interleaved

    interleaved(pg_system)
    summary = run(pg_system)
    assert summary["status"] == "COMPLETED"
    with Session(pg_system.parent) as db:
        rows = db.execute(select(
            EvaluationResult.id, EvaluationResult.repetition, EvaluationResult.case_id,
            Experiment.formal_condition_key,
        ).join(Experiment, EvaluationResult.experiment_id == Experiment.id)).all()
    actual = {row.id: tuple(row[1:]) for row in rows}
    expected = [(rep, case, key) for rep in (1, 2)
                for case in sorted({row.case_id for row in rows}) for key in ORDER]
    assert [actual[ident] for ident in pg_system.runner.calls] == expected
    assert run(pg_system, NEXT) == summary
    assert len(pg_system.runner.calls) == 24


def test_actual_pg_interleaved_later_condition_crash_fences_other_conditions(pg_system):
    from test_formal_interleaving import interleaved

    interleaved(pg_system)

    def interrupt_second(isolated, case, config, *_callbacks):
        if len(pg_system.runner.calls) == 2:
            run_row, attempt = _attempt(config, case, pg_system.runner, status="STARTED")
            with Session(isolated) as db, db.begin():
                db.add(run_row)
                db.flush()
                db.add(attempt)
            raise KeyboardInterrupt
        return outcome([])

    pg_system.runner.control = interrupt_second
    with pytest.raises(KeyboardInterrupt):
        run(pg_system)
    assert len(pg_system.runner.calls) == 2
    resumed = run(pg_system, NEXT)
    assert resumed["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert len(pg_system.runner.calls) == 2
    with Session(pg_system.parent) as db:
        assert len(list(db.scalars(select(EvaluationResult).where(
            EvaluationResult.status == "QUEUED")))) == 22
