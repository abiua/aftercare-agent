"""Guarded disposable PostgreSQL migration and concurrent blind-review stages."""

import os
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from test_chat_analysis_migration import _revision

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest, now
from aftercare.db import User
from aftercare.evaluation.chat.contracts import InitialJudgment, Versioned
from aftercare.evaluation.chat.service import BlindReviewService
from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindTask
from aftercare.evaluation.tables import EvaluationMutation


def test_chat_blind_review_migration_compiles_typed_fk():
    output = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    ):
        _revision("20261111_chat_blind_review").upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE") == sql.count("FOREIGN KEY") == 4
    assert "reviewer_id VARCHAR(36) NOT NULL" in sql
    assert "UNIQUE (reviewer_id, input_sha256)" in sql


@pytest.mark.pg
def test_pg_blind_review_serializes_concurrent_seal_and_reveal():
    dsn = os.environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat migration PostgreSQL required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "127.0.0.1" and url.port not in {5432, 55450, 55451}
    assert url.database == url.username == "chat_migration" and not url.query
    admin = create_engine(url, hide_parameters=True)
    schema = "blind_review_" + uuid4().hex
    engine = None
    created = False
    reviewer = UserContext(user_id=str(uuid4()), role="tester")
    try:
        with admin.begin() as db:
            cluster = db.scalar(text("SELECT current_setting('cluster_name')"))
            assert cluster == os.environ["AFTERCARE_CHAT_MIGRATION_CLUSTER"]
            assert cluster.startswith("aftercare-chat-migration-")
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(
            url,
            hide_parameters=True,
            connect_args={
                "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=12000"
            },
        )
        with engine.begin() as db:
            User.__table__.create(db)
            EvaluationMutation.__table__.create(db)
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    id=reviewer.user_id,
                    token_hash="a" * 64,
                    role="tester",
                    display_name="Fixture reviewer",
                    enabled=True,
                )
            )
        with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
            _revision("20261111_chat_blind_review").upgrade()
        task_id = str(uuid4())
        frozen = {"schema_version": "chat-blind-input-v1", "facts": [], "policies": []}
        with Session(engine) as db, db.begin():
            db.add(
                BlindTask(
                    id=task_id,
                    reviewer_id=reviewer.user_id,
                    case_label="P5-CONCURRENCY-FIXTURE",
                    input_sha256=digest(frozen),
                    source_sha256="b" * 64,
                    input=frozen,
                    image_objects={},
                    candidates=[{"id": "fixture", "status": "UNAVAILABLE", "options": []}],
                    version=1,
                    created_at=now(),
                )
            )
        service = BlindReviewService(
            SimpleNamespace(engine=engine, settings=SimpleNamespace(profile="test"))
        )
        for operation, body in (
            ("initial", InitialJudgment(expected_version=1, judgment="UNDETERMINED")),
            ("reveal", Versioned(expected_version=2)),
        ):
            barrier = Barrier(2)

            def write(operation=operation, body=body, barrier=barrier):
                barrier.wait(timeout=5)
                try:
                    service.write(reviewer, task_id, operation, body, uuid4().hex)
                    return 200
                except BusinessError as error:
                    assert error.code == "BLIND_REVIEW_STAGE_CHANGED"
                    return error.status

            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(write) for _ in range(2)]
                assert sorted(item.result(timeout=20) for item in jobs) == [200, 409]
        with Session(engine) as db:
            assert db.scalars(select(BlindInitial.task_id)).all() == [task_id]
            assert db.scalars(select(BlindExposure.task_id)).all() == [task_id]
            assert (
                db.get_one(BlindExposure, task_id).initial_sha256
                == db.get_one(BlindInitial, task_id).sha256
            )
            assert db.get_one(User, reviewer.user_id).display_name == "Fixture reviewer"
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
