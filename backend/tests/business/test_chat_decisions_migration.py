"""P3 typed DDL and compatibility with pre-existing chat and proposal records."""

import os
import re
from io import StringIO
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from test_chat_analysis_migration import _revision


def test_decision_migration_compiles_postgresql_types():
    output = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    ):
        _revision("20261111_chat_decisions").upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE") == 6
    assert sql.count("FOREIGN KEY") == 6
    assert "decision_conversation_id VARCHAR(36)" in sql
    assert "CHECK" in sql and "ck_business_cases_decision_binding" in sql


@pytest.mark.pg
def test_decision_migration_preserves_legacy_case_and_proposal_on_disposable_pg():
    dsn = os.environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat migration PostgreSQL required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "127.0.0.1" and url.port not in {5432, 55450, 55451}
    assert url.database == url.username == "chat_migration" and not url.query
    engine = create_engine(url, hide_parameters=True)
    schema = "decision_migration_" + uuid4().hex
    try:
        with engine.begin() as db:
            identity = db.execute(
                text("SELECT current_database(), current_user, current_setting('cluster_name')")
            ).one()
            assert identity[:2] == ("chat_migration", "chat_migration")
            assert re.fullmatch(r"aftercare-chat-migration-[0-9a-f]{32}", identity[2])
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            for table, length in (
                ("business_customers", 64),
                ("business_orders", 64),
                ("business_cases", 36),
                ("business_proposals", 36),
            ):
                db.execute(text(f"CREATE TABLE {table} (id VARCHAR({length}) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(db)):
                _revision("20261111_conversations").upgrade()
                _revision("20261111_chat_analysis").upgrade()
            db.execute(text("INSERT INTO business_cases VALUES ('legacy-case')"))
            db.execute(text("INSERT INTO business_proposals VALUES ('legacy-proposal')"))
            with Operations.context(MigrationContext.configure(db)):
                _revision("20261111_chat_decisions").upgrade()
            case = db.execute(text("SELECT * FROM business_cases")).one()
            proposal = db.execute(text("SELECT * FROM business_proposals")).one()
            assert tuple(case) == ("legacy-case", None, None, None)
            assert tuple(proposal) == ("legacy-proposal", None)
            assert "chat_decision_projections" in inspect(db).get_table_names(schema=schema)
            assert len(inspect(db).get_foreign_keys("chat_decision_offers", schema=schema)) == 2
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
