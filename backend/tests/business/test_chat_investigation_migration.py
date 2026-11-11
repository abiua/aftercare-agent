"""Compile the new outbox revision using Alembic's standalone PG metadata."""

from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_chat_analysis_migration import _revision


def test_investigation_outbox_postgresql_migration_has_typed_foreign_keys():
    output = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    ):
        _revision("20261111_chat_investigation").upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE chat_investigation_requests" in sql
    assert "turn_id VARCHAR(36) NOT NULL" in sql
    assert "PRIMARY KEY (turn_id)" in sql
    assert "UNIQUE (run_id)" in sql
    assert sql.count("FOREIGN KEY") == 4


def test_investigation_outbox_real_postgresql_upgrade():
    from os import environ
    from uuid import uuid4

    import pytest
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url

    dsn = environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat migration PostgreSQL required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "127.0.0.1" and url.port not in {5432, 55450, 55451}
    assert url.database == url.username == "chat_migration" and not url.query
    engine = create_engine(url, hide_parameters=True)
    schema = "investigation_migration_" + uuid4().hex
    try:
        with engine.begin() as db:
            cluster = db.scalar(text("SELECT current_setting('cluster_name')"))
            assert cluster == environ["AFTERCARE_CHAT_MIGRATION_CLUSTER"]
            assert cluster.startswith("aftercare-chat-migration-")
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            for table in (
                "business_customers",
                "business_orders",
                "business_cases",
                "business_proposals",
                "business_materials",
                "agent_runs",
            ):
                db.execute(text(f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(db)):
                for revision in (
                    "20261111_conversations",
                    "20261111_chat_analysis",
                    "20261111_chat_decisions",
                ):
                    _revision(revision).upgrade()
                db.execute(text("INSERT INTO business_cases VALUES ('legacy-case')"))
                _revision("20261111_chat_investigation").upgrade()
            assert db.scalar(text("SELECT id FROM business_cases")) == "legacy-case"
            assert (
                len(inspect(db).get_foreign_keys("chat_investigation_requests", schema=schema)) == 4
            )
            with Operations.context(MigrationContext.configure(db)):
                _revision("20261111_chat_material_source").upgrade()
            assert db.scalar(text("SELECT id FROM business_cases")) == "legacy-case"
            assert len(inspect(db).get_foreign_keys("chat_material_sources", schema=schema)) == 5
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
