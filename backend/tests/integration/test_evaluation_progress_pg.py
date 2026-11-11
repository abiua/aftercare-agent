"""Exercise journal locking and rollback in an isolated real PostgreSQL schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aftercare.business.rules import now
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.evaluation.progress import append_progress, read_progress
from aftercare.evaluation.tables import EvaluationProgress, EvaluationResult, Experiment

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_PROGRESS_PG") != "1",
        reason="explicit isolated PostgreSQL acceptance required",
    ),
]


def test_concurrent_journal_writers_and_transaction_rollback():
    engine = guarded_engine(load_settings("test"), test_only=True)
    schema = "progress_acceptance_" + uuid4().hex
    experiment_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        for table in (Experiment, EvaluationResult, EvaluationProgress):
            table.__table__.create(scoped)
        with Session(scoped) as db, db.begin():
            db.add(
                Experiment(
                    id=experiment_id,
                    name="isolated journal contention",
                    created_by="audit",
                    status="RUNNING",
                    version=1,
                    progress_seq=0,
                    config={},
                    created_at=now(),
                    updated_at=now(),
                )
            )

        def write(_):
            with Session(scoped) as db, db.begin():
                row = db.scalar(
                    select(Experiment).where(Experiment.id == experiment_id).with_for_update()
                )
                row.version += 1
                append_progress(db, row)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(32)))
        before = read_progress(scoped, experiment_id, 0, 100)
        assert [event["sequence"] for event in before["events"]] == list(range(1, 33))
        assert [event["experiment_version"] for event in before["events"]] == list(range(2, 34))
        with pytest.raises(RuntimeError, match="rollback probe"), Session(scoped) as db, db.begin():
            row = db.scalar(
                select(Experiment).where(Experiment.id == experiment_id).with_for_update()
            )
            row.status = "COMPLETED"
            row.version += 1
            append_progress(db, row)
            db.flush()
            raise RuntimeError("rollback probe")
        assert read_progress(scoped, experiment_id, 0, 100) == before
        write(None)
        resumed = read_progress(scoped, experiment_id, 32, 100)
        assert resumed["latest_sequence"] == 33
        assert [event["sequence"] for event in resumed["events"]] == [33]
        assert not resumed["terminal"]
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_upgrade_preserves_old_experiment_without_inventing_history():
    engine = guarded_engine(load_settings("test"), test_only=True)
    schema = "progress_migration_" + uuid4().hex
    path = Path(__file__).parents[2] / "migrations/versions/20261110_evaluation_progress.py"
    spec = importlib.util.spec_from_file_location("progress_migration_probe", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(
                text(
                    "CREATE TABLE eval_experiments (id varchar(36) PRIMARY KEY, "
                    "status varchar(16) NOT NULL, config json NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO eval_experiments VALUES ('legacy', 'COMPLETED', '{\"source\":\"fixture\"}')"
                )
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            row = connection.execute(text("SELECT * FROM eval_experiments")).mappings().one()
            assert dict(row) == {
                "id": "legacy",
                "status": "COMPLETED",
                "config": {"source": "fixture"},
                "progress_seq": None,
            }
            assert connection.scalar(text("SELECT count(*) FROM eval_progress_events")) == 0
            migration.downgrade()
            row = connection.execute(text("SELECT * FROM eval_experiments")).mappings().one()
            assert dict(row) == {
                "id": "legacy",
                "status": "COMPLETED",
                "config": {"source": "fixture"},
            }
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
