"""Compile the P2 migration and optionally apply it to a disposable PostgreSQL cluster."""

import os
import re
from io import StringIO

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from aftercare.config import ROOT


def _revision(name):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    return ScriptDirectory.from_config(config).get_revision(name).module


def test_analysis_upgrade_compiles_all_foreign_keys_without_existing_metadata():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        _revision("20261111_chat_analysis").upgrade()
    sql = output.getvalue()
    assert "ALTER TABLE chat_conversations ADD COLUMN analysis_issue_id VARCHAR(36)" in sql
    assert all(
        f"CREATE TABLE {table}" in sql
        for table in ("chat_analysis_turns", "chat_model_attempts", "chat_observations")
    )
    assert sql.count("FOREIGN KEY") == 6
    assert "attachment_id VARCHAR(36) NOT NULL" in sql
    assert "FOREIGN KEY(attachment_id) REFERENCES chat_attachments (id)" in sql


@pytest.mark.pg
def test_analysis_upgrade_preserves_existing_chat_rows_on_disposable_postgres():
    dsn = os.environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat migration PostgreSQL required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "127.0.0.1" and url.port not in {5432, 55450, 55451}
    assert url.database == url.username == "chat_migration" and not url.query
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as db:
            identity = db.execute(
                text("SELECT current_database(), current_user, current_setting('cluster_name')")
            ).one()
            assert identity[:2] == ("chat_migration", "chat_migration")
            assert re.fullmatch(r"aftercare-chat-migration-[0-9a-f]{32}", identity[2])
            assert inspect(db).get_table_names() == []
            for table, length in (
                ("business_customers", 64),
                ("business_orders", 64),
                ("business_cases", 36),
            ):
                db.execute(text(f"CREATE TABLE {table} (id VARCHAR({length}) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(db)):
                _revision("20261111_conversations").upgrade()
            db.execute(text("INSERT INTO business_customers VALUES ('fixture-owner')"))
            db.execute(
                text("""
                INSERT INTO chat_conversations
                    (id, customer_id, status, revision, sequence, line_ids, input_floor,
                     require_new_case, created_at, updated_at)
                VALUES ('fixture-chat', 'fixture-owner', 'OPEN', 2, 1, '[]', 0, false, now(), now())
            """)
            )
            db.execute(
                text("""
                INSERT INTO chat_messages (id, conversation_id, sequence, role, text, cards, created_at)
                VALUES ('fixture-msg', 'fixture-chat', 1, 'customer', ' original ', '[]', now())
            """)
            )
            before = db.scalar(text("SELECT to_jsonb(c) FROM chat_conversations c"))
            with Operations.context(MigrationContext.configure(db)):
                _revision("20261111_chat_analysis").upgrade()
            after = db.scalar(text("SELECT to_jsonb(c) FROM chat_conversations c"))
            for field in ("analysis_issue_id", "analysis_from_seq", "analysis_requests"):
                assert after.pop(field) is None
            assert after == before
            assert db.scalar(text("SELECT text FROM chat_messages")) == " original "
            assert (
                sum(
                    len(inspect(db).get_foreign_keys(table))
                    for table in ("chat_analysis_turns", "chat_model_attempts", "chat_observations")
                )
                == 6
            )
    finally:
        engine.dispose()
