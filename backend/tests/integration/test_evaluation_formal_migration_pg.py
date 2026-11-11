"""Opt-in PostgreSQL downgrade race check in a disposable test schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine
from aftercare.evaluation.tables import Experiment

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_FORMAL_MIGRATION_PG") != "1",
        reason="explicit isolated PG acceptance",
    ),
]


def test_upgrade_and_empty_formal_downgrade_preserve_ordinary_rows():
    engine = guarded_engine(load_settings("test"), test_only=True)
    schema = f"formal_migration_{uuid4().hex}"
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("20261110_eval_formal").module
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        Experiment.__table__.create(engine.execution_options(schema_translate_map={None: schema}))
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            # Reconstruct the predecessor table shape without touching public data.
            connection.execute(text("ALTER TABLE eval_experiments DROP CONSTRAINT uq_eval_experiments_formal_condition"))
            connection.execute(text("ALTER TABLE eval_experiments DROP CONSTRAINT ck_eval_experiments_formal_pair"))
            connection.execute(text("ALTER TABLE eval_experiments DROP COLUMN formal_freeze_sha256, DROP COLUMN formal_condition_key"))
            connection.execute(text(
                "INSERT INTO eval_experiments (id,name,created_by,status,version,config,created_at,updated_at) "
                "VALUES (:id,'ordinary','tester','COMPLETED',2,CAST(:config AS json),now(),now())"
            ), {"id": str(uuid4()), "config": '{"legacy":true}'})
            before = connection.scalar(text("SELECT to_jsonb(e)::text FROM eval_experiments e"))
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
            assert connection.scalar(text(
                "SELECT (to_jsonb(e)-'formal_freeze_sha256'-'formal_condition_key')::text FROM eval_experiments e"
            )) == before
            assert connection.scalar(text(
                "SELECT formal_freeze_sha256 IS NULL AND formal_condition_key IS NULL FROM eval_experiments"
            ))
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()
            assert connection.scalar(text("SELECT to_jsonb(e)::text FROM eval_experiments e")) == before
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_downgrade_waits_for_writer_and_preserves_concurrent_formal_binding():
    engine = guarded_engine(load_settings("test"), test_only=True)
    schema = f"formal_migration_{uuid4().hex}"
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("20261110_eval_formal").module
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})

    def downgrade(pid_out):
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            pid_out.put(connection.scalar(text("SELECT pg_backend_pid()")))
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()

    def writer_is_blocking(connection, writer_pid, migration_pid):
        return connection.scalar(
            text("SELECT :writer = ANY(pg_blocking_pids(:migration))"),
            {"writer": writer_pid, "migration": migration_pid},
        )

    try:
        Experiment.__table__.create(scoped)
        formal_id = str(uuid4())
        digest = "b" * 64
        with ThreadPoolExecutor(max_workers=1) as executor, engine.begin() as writer:
            writer.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            writer_pid = writer.scalar(text("SELECT pg_backend_pid()"))
            writer.execute(
                text(
                    "INSERT INTO eval_experiments "
                    "(id, name, created_by, status, version, config, created_at, updated_at, "
                    "formal_freeze_sha256, formal_condition_key) VALUES "
                    "(:id, 'isolated', 'tester', 'QUEUED', 1, '{}', now(), now(), :digest, 'single/routed')"
                ),
                {"id": formal_id, "digest": digest},
            )
            pid_out = Queue()
            migration = executor.submit(downgrade, pid_out)
            migration_pid = pid_out.get(timeout=5)
            deadline = monotonic() + 5
            while not writer_is_blocking(writer, writer_pid, migration_pid):
                assert monotonic() < deadline, "migration did not wait on this test writer"
                sleep(0.02)
        with pytest.raises(DBAPIError, match="FORMAL_EVALUATION_DOWNGRADE_BLOCKED"):
            migration.result()
        with engine.connect() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("eval_experiments", schema)
            }
            assert {"formal_freeze_sha256", "formal_condition_key"} <= columns
            persisted = connection.execute(
                text(
                    f'SELECT formal_freeze_sha256, formal_condition_key FROM "{schema}".eval_experiments WHERE id = :id'
                ),
                {"id": formal_id},
            ).one()
            assert tuple(persisted) == (digest, "single/routed")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
