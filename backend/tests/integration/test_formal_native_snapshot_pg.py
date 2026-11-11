"""Actual read-only native validation snapshot over isolated synthetic PG rows."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.evaluation.formal.contracts import NativeValidation, ValidationPlan
from aftercare.evaluation.formal.register import _sha
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import EvaluationResult, EvaluationReview, Experiment
from aftercare.intelligence.tables import AgentRun, ModelAttempt

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_NATIVE_SNAPSHOT_PG") != "1",
        reason="explicit isolated PostgreSQL acceptance required",
    ),
]
STAMP = datetime(2026, 11, 10, 10, tzinfo=UTC)
TESTER = UserContext(user_id="native-tester", role="tester")
DISABLED = UserContext(user_id="native-disabled", role="tester")
WRONG_ROLE = UserContext(user_id="native-support", role="tester")
CONDITIONS = [
    ("single", "economy"),
    ("single", "analysis"),
    ("single", "routed"),
    ("multi", "routed"),
]
MODEL = {"alias": "economy", "provider": "synthetic-only", "model": "ledger-fixture"}


class IsolatedSchemas:
    def __init__(self, engine):
        self.engine = engine
        self.tables = {}

    def create(self, schema, *models):
        assert schema.startswith(("native_acceptance_", "eval_")) and schema[-32:].isalnum()
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        self.tables[schema] = models
        scoped = self.engine.execution_options(schema_translate_map={None: schema})
        for model in models:
            model.__table__.create(scoped)
        return scoped

    def snapshot(self):
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            return {
                (schema, table.__tablename__): tuple(
                    conn.execute(
                        text(
                            "SELECT count(*), encode(sha256(convert_to(coalesce("
                            "string_agg(row_json, E'\\n' ORDER BY row_json), ''), 'UTF8')), 'hex') "
                            f'FROM (SELECT to_jsonb(t)::text AS row_json FROM "{schema}".'
                            f'"{table.__tablename__}" t) row_set'
                        )
                    ).one()
                )
                for schema, models in self.tables.items()
                for table in models
            }

    def close(self):
        with self.engine.begin() as conn:
            for schema in reversed(self.tables):
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                assert conn.scalar(
                    text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}
                )


def seed_audit(schemas, result_id, *, status, estimated):
    # Independent construction from UUID is intentional. Do not use the runtime's helper here.
    audit = schemas.create(f"eval_{result_id.replace('-', '')}", AgentRun, ModelAttempt)
    run_id = str(uuid4())
    with Session(audit) as db, db.begin():
        db.add(
            AgentRun(
                id=run_id,
                thread_id=f"synthetic:{run_id}",
                case_id="synthetic-business-case",
                owner_id=TESTER.user_id,
                actor={},
                status="COMPLETED",
                mode="single",
                strategy="economy",
                message="synthetic ledger only",
                case_version=1,
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.flush()
        db.add(
            ModelAttempt(
                run_id=run_id,
                cycle=0,
                logical_id="synthetic",
                ordinal=1,
                metadata_json={**MODEL, "reserved_cost_usd": "0.04"},
                status=status,
                started_at=STAMP,
                finished_at=STAMP if estimated else None,
                estimated_cost_usd=estimated,
            )
        )
    return audit, run_id


def seed_validation(schemas, scoped):
    reference = {"intent": "damaged", "label_source": "SYNTHETIC_REFERENCE"}
    cases = {
        key: {"case_sha256": char * 64, "reference_sha256": digest(reference)}
        for key, char in (("V1", "a"), ("V2", "b"))
    }
    configs, experiment_ids, result_ids, experiment_entries = {}, [], [], []
    late_audit = late_run_id = None
    with Session(scoped) as db, db.begin():
        for actor, enabled, role in (
            (TESTER, True, "tester"),
            (DISABLED, False, "tester"),
            (WRONG_ROLE, True, "support"),
        ):
            db.add(
                User(
                    id=actor.user_id,
                    role=role,
                    display_name=actor.user_id,
                    enabled=enabled,
                    token_hash=uuid4().hex,
                )
            )

        for index, (mode, strategy) in enumerate(CONDITIONS):
            experiment_id = str(uuid4())
            experiment_ids.append(experiment_id)
            if mode == "single":
                experiment_entries.append({"group": "A", "experiment_id": experiment_id})
            if strategy == "routed":
                experiment_entries.append({"group": "B", "experiment_id": experiment_id})
            config = {
                "dataset_id": "v0.2",
                "split": "validation",
                "source": "live",
                "mode": mode,
                "strategy": strategy,
                "max_calls": 2,
                "budget_usd": 0.1,
                "code_digest": "c" * 64,
                "scorer_version": "test-scorer",
                "metrics_version": "test-metrics",
                "model_snapshot": {"models": [MODEL]},
                "input_digest": digest({key: value["case_sha256"] for key, value in cases.items()}),
                "reference_digest": digest(
                    {key: value["reference_sha256"] for key, value in cases.items()}
                ),
            }
            configs[f"{mode}/{strategy}"] = digest(config)
            db.add(
                Experiment(
                    id=experiment_id,
                    name="synthetic native ledger",
                    created_by=TESTER.user_id,
                    status="CANCELLED" if index == 2 else "COMPLETED",
                    version=1,
                    config=config,
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            db.flush()
            for key, entry in cases.items():
                result_id = str(uuid4())
                started = key == "V1"
                actual = None
                status = "NOT_RUN"
                if started:
                    result_ids.append(result_id)
                    status = "RUNNING" if index == 2 else "NEEDS_REVIEW"
                    attempt = {
                        "cycle": 0,
                        "logical_id": "synthetic",
                        "ordinal": 1,
                        "status": "UNKNOWN",
                        "estimated_cost_usd": None,
                        "usage": None,
                    }
                    if index in (0, 1):
                        audit, run_id = seed_audit(
                            schemas, result_id, status="UNKNOWN", estimated=None
                        )
                        if index == 1:
                            late_audit, late_run_id = audit, run_id
                            attempt["run_id"] = run_id
                    actual = {
                        "observed_source": "live_provider",
                        # A forged snapshot namespace is irrelevant. The lookup derives it from result_id.
                        "namespace": "public",
                        "model_attempts": [attempt] if index in (0, 1) else [],
                        "model_audit_complete": index != 2,
                    }
                elif index == 3:
                    # An anomalous raw charge in a result reported as not started.
                    seed_audit(schemas, result_id, status="FAILED", estimated="0.06")
                db.add(
                    EvaluationResult(
                        id=result_id,
                        experiment_id=experiment_id,
                        case_id=key,
                        repetition=1,
                        status=status,
                        version=1,
                        intent="damaged",
                        expected=reference,
                        actual=actual,
                        checks=[{"path": "synthetic", "status": "PASS"}] if started else [],
                        trace=[],
                        quota={"calls": 1, "budget_usd": "0.05", "case_hash": entry["case_sha256"]},
                        started_at=STAMP if started else None,
                        finished_at=STAMP if status == "NEEDS_REVIEW" else None,
                    )
                )
    plan = ValidationPlan(
        dataset_id="v0.2",
        cases=cases,
        repetitions=1,
        config_sha256=configs,
        policies_digest="d" * 64,
        retrieval_digest="e" * 64,
    )
    request = NativeValidation(
        plan_sha256=_sha(plan),
        experiments=experiment_entries,
        selected_experiment_id=experiment_ids[0],
        rationale="synthetic native ledger, no provider",
        selection={
            "actor": "synthetic fixture",
            "at": STAMP,
            "source": "isolated integration test",
        },
    )
    return plan, request, experiment_ids, result_ids[1], late_audit, late_run_id


def test_native_pg_snapshot_does_not_mix_reviews_and_late_costs(monkeypatch):
    engine = guarded_engine(load_settings("test"), test_only=True)
    schemas = IsolatedSchemas(engine)
    try:
        scoped = schemas.create(
            "native_acceptance_" + uuid4().hex,
            User,
            Experiment,
            EvaluationResult,
            EvaluationReview,
        )
        plan, request, ids, late_result_id, late_audit, late_run_id = seed_validation(
            schemas, scoped
        )
        service = EvaluationService(scoped, load_settings("test"))
        initial_rows = schemas.snapshot()
        for actor in (DISABLED, WRONG_ROLE, UserContext(user_id="not-found", role="tester")):
            with pytest.raises(BusinessError) as error:
                service.native_validation_snapshot(actor, plan, request)
            assert error.value.code == "EVALUATION_FORBIDDEN"
        baseline = service.native_validation_snapshot(TESTER, plan, request)
        assert schemas.snapshot() == initial_rows
        assert baseline.source_verification == "NATIVE_LEDGER_ONLY"
        assert baseline.human_identity == "UNAUTHENTICATED_DECLARATION"
        first, second, cancelled, anomaly = (baseline.evidence[key] for key in ids)
        assert first.findings["AUDIT_UNMATCHED"] == 2
        assert second.attempt_statuses == second.frozen_attempt_statuses == {"UNKNOWN": 1}
        assert second.findings["ATTEMPT_COST_UNKNOWN"] == 1
        assert cancelled.findings["UNSETTLED_RESULTS"] == 1
        assert anomaly.findings["UNSTARTED_RESULT_WITH_ATTEMPTS"] == 1
        assert anomaly.findings["ATTEMPT_QUOTA_EXCEEDED"] == 1
        assert anomaly.attempt_statuses == {"FAILED": 1}

        arrived, release = Event(), Event()
        detail = service._detail
        observed_settings = []

        def inspect_transaction(conn):
            return tuple(
                conn.execute(
                    text(
                        "SELECT pg_backend_pid(), current_setting('transaction_isolation'), "
                        "current_setting('transaction_read_only'), transaction_timestamp()"
                    )
                ).one()
            )

        def before_other_reviews(db, experiment, **kwargs):
            report = detail(db, experiment, **kwargs)
            if experiment.id == ids[0] and not arrived.is_set():
                observed_settings.append(inspect_transaction(db.connection()))
                arrived.set()
                assert release.wait(10), "concurrent writer did not release the reader"
            return report

        from aftercare.evaluation.formal import native

        read_isolated = native.read_isolated

        def verify_isolated_connection(conn, result_id):
            observed_settings.append(inspect_transaction(conn))
            return read_isolated(conn, result_id)

        with monkeypatch.context() as patch:
            patch.setattr(service, "_detail", before_other_reviews)
            patch.setattr(native, "read_isolated", verify_isolated_connection)
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(service.native_validation_snapshot, TESTER, plan, request)
                try:
                    assert arrived.wait(10), "reader did not open its snapshot"
                    with Session(scoped) as db, db.begin():
                        db.add(
                            EvaluationReview(
                                id=str(uuid4()),
                                result_id=late_result_id,
                                version=2,
                                decision="PASS",
                                notes="synthetic concurrent review",
                                reviewer_id=TESTER.user_id,
                                created_at=STAMP + timedelta(seconds=1),
                                annotations={},
                            )
                        )
                        db.get_one(EvaluationResult, late_result_id).version = 2
                    with Session(late_audit) as db, db.begin():
                        raw = db.get_one(ModelAttempt, (late_run_id, 0, "synthetic", 1))
                        raw.status = "SUCCEEDED"
                        raw.finished_at = STAMP + timedelta(seconds=1)
                        raw.usage = {"input_tokens": 17, "output_tokens": 3}
                        raw.estimated_cost_usd = "0.07"
                    committed_rows = schemas.snapshot()
                finally:
                    release.set()
                during = reading.result(timeout=10)
        assert len(observed_settings) == 9
        assert len(set(observed_settings)) == 1
        _, isolation, read_only, read_at = observed_settings[0]
        assert (isolation, read_only, read_at) == ("repeatable read", "on", during.read_at)
        assert during.model_dump(exclude={"read_at"}) == baseline.model_dump(exclude={"read_at"})
        assert schemas.snapshot() == committed_rows

        subsequent = service.native_validation_snapshot(TESTER, plan, request)
        latest = subsequent.evidence[ids[1]]
        assert latest.machine_statuses == {"NEEDS_REVIEW": 1, "NOT_RUN": 1}
        assert latest.effective_statuses == {"PASSED": 1, "NOT_RUN": 1}
        assert latest.attempt_statuses == {"SUCCEEDED": 1}
        assert latest.frozen_attempt_statuses == {"UNKNOWN": 1}
        assert latest.findings["LATE_OR_CHANGED_ATTEMPT"] == 1
        assert latest.findings["ATTEMPT_QUOTA_EXCEEDED"] == 1
        assert "PROVIDER_RESPONSE_PROVENANCE_UNAVAILABLE" in latest.findings
        assert subsequent.source_verification == "NATIVE_LEDGER_ONLY"
        assert schemas.snapshot() == committed_rows
        print(
            "RR/READ ONLY shared one backend transaction across results, reviews, and 8 derived audit reads."
        )
        print("Late review/cost only in next snapshot; no audit rewrites or provider calls.")
    finally:
        try:
            schemas.close()
        finally:
            engine.dispose()
