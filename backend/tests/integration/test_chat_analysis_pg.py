"""Concurrency acceptance on a private, disposable PostgreSQL container."""

import os
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Case, Customer, Order
from aftercare.conversation_analysis.service import AnalysisLimits, AnalysisService
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, ChatModelAttempt, Observation
from aftercare.conversations.tables import Attachment, Conversation, Message
from aftercare.db import Base, User

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.getenv("AFTERCARE_CHAT_ANALYSIS_PG") != "1", reason="explicit disposable PG"
    ),
]
OWNER = "fixture-owner"
CHAT = "fixture-conversation"
LOGICAL = {"logical_id": "vision:controlled"}


def _docker(*args, timeout=30):
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed for the disposable PostgreSQL fixture")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    folder = tmp_path_factory.mktemp("chat-analysis-pg")
    password = secrets.token_hex(24)
    env_file = folder / "pg.env"
    env_file.touch(mode=0o600)
    env_file.write_text(f"POSTGRES_PASSWORD={password}\n", encoding="ascii")
    name = f"ac-chat-analysis-{uuid4().hex}"
    container = _docker(
        "run",
        "--detach",
        "--rm",
        "--pull",
        "never",
        "--name",
        name,
        "--env-file",
        str(env_file),
        "--publish",
        "127.0.0.1::5432",
        "postgres:17-alpine",
        "-c",
        f"cluster_name={name}",
    )
    engine = None
    try:
        port = int(_docker("port", container, "5432/tcp").rsplit(":", 1)[1])
        engine = create_engine(
            URL.create(
                "postgresql+psycopg",
                username="postgres",
                password=password,
                host="127.0.0.1",
                port=port,
                database="postgres",
            ),
            connect_args={"connect_timeout": 2, "options": "-c statement_timeout=8000"},
        )
        until = monotonic() + 20
        while True:
            try:
                with engine.connect() as connection:
                    assert connection.scalar(text("SHOW cluster_name")) == name
                break
            except OperationalError:
                if monotonic() >= until:
                    raise RuntimeError("Disposable PostgreSQL was not ready") from None
                sleep(0.2)
        yield engine
    finally:
        env_file.unlink(missing_ok=True)
        if engine is not None:
            engine.dispose()
        _docker("rm", "--force", container)


@pytest.fixture
def state(postgres):
    schema = f"chat_test_{uuid4().hex}"
    with postgres.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = postgres.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                item.__table__
                for item in (
                    Customer,
                    Order,
                    Case,
                    User,
                    Conversation,
                    Message,
                    Attachment,
                    ChatAnalysisTurn,
                    ChatModelAttempt,
                    Observation,
                )
            ],
        )
        stamp = SimpleNamespace(value=datetime(2026, 11, 11, tzinfo=UTC))
        limits = AnalysisLimits(2, 100, quiet_seconds=2, lease_seconds=5)
        service = AnalysisService(engine, limits, clock=lambda: stamp.value)
        with Session(engine) as db, db.begin():
            db.add(Customer(id=OWNER, data={}))
            db.flush()
            db.add(
                User(
                    id=OWNER,
                    role="customer",
                    customer_id=OWNER,
                    enabled=True,
                    token_hash="t" * 64,
                    display_name="fixture",
                )
            )
            db.add(
                Conversation(
                    id=CHAT,
                    customer_id=OWNER,
                    status="OPEN",
                    revision=0,
                    sequence=0,
                    line_ids=[],
                    input_floor=0,
                    require_new_case=False,
                    created_at=stamp.value,
                    updated_at=stamp.value,
                )
            )
        yield SimpleNamespace(engine=engine, service=service, limits=limits, stamp=stamp)
    finally:
        with postgres.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def _advance(state, seconds):
    state.stamp.value += timedelta(seconds=seconds)


def _stage(state, db=None):
    if db is None:
        with Session(state.engine) as session, session.begin():
            return _stage(state, session)
    chat = db.get_one(Conversation, CHAT, with_for_update=True)
    chat.sequence += 1
    chat.revision += 1
    row = Message(
        id=str(uuid4()),
        conversation_id=chat.id,
        actor_id=OWNER,
        sequence=chat.sequence,
        role="customer",
        text="合成消息",
        cards=[],
        created_at=state.stamp.value,
    )
    db.add(row)
    db.flush()
    db.add(
        Attachment(
            id=str(uuid4()),
            message_id=row.id,
            position=0,
            filename="test.png",
            object_key=str(uuid4()),
            content_type="image/png",
            size=1,
            sha256="a" * 64,
        )
    )
    return state.service.stage(db, chat, input_from_seq=1).id


def _claim_bound(state):
    ident = _stage(state)
    _advance(state, 2)
    lease = state.service.claim(ident)
    assert lease is not None
    photo = state.service.input_snapshot(lease)["attachments"][-1]
    evidence = {
        "attachment_id": photo["id"],
        "message_id": photo["message_id"],
        "original_sha256": photo["sha256"],
        "sent_sha256": "b" * 64,
        "transform_version": "fixture",
    }
    state.service.bind_vision_input(lease.turn_id, lease.token, {**LOGICAL, "photos": [evidence]})
    return lease, {
        **evidence,
        **LOGICAL,
        "source_kind": "MODEL_OBSERVATION",
        "verification_state": "UNVERIFIED",
        "candidate_kind": "ITEM",
        "uncertainty": "LIMITED",
        "findings": ["POSSIBLE_CRACK"],
    }


def _code(operation):
    try:
        return operation()
    except BusinessError as error:
        return error.code


def _wait_for_conversation_lock(engine):
    until = monotonic() + 4
    while monotonic() < until:
        with engine.connect() as connection:
            blocked = connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock' AND query LIKE '%chat_conversations%')"
                )
            )
        if blocked:
            return
        sleep(0.02)
    pytest.fail("Contending transaction never reached the PostgreSQL conversation lock")


def test_real_pg_duplicate_claim_and_different_turn_reserves(state):
    ident = _stage(state)
    _advance(state, 2)
    assert state.service.due() == [ident]
    barrier = Barrier(4, timeout=5)

    def claim(_index):
        barrier.wait()
        return state.service.claim(ident)

    with ThreadPoolExecutor(max_workers=4) as workers:
        leases = [lease for lease in workers.map(claim, range(4)) if lease is not None]
    assert len(leases) == 1
    current, _ = _claim_bound(state)
    attempts = Barrier(2, timeout=5)

    def reserve(lease):
        attempts.wait()
        return _code(lambda: state.service.reserve(lease.turn_id, lease.token, 80, LOGICAL))

    with ThreadPoolExecutor(max_workers=2) as workers:
        old, new = workers.map(reserve, (leases[0], current))
    assert old == "ANALYSIS_STALE" and new[1] > 0
    next_lease, _ = _claim_bound(state)
    assert (
        _code(lambda: state.service.reserve(next_lease.turn_id, next_lease.token, 21, LOGICAL))
        == "ANALYSIS_BUDGET_EXCEEDED"
    )
    with Session(state.engine) as db:
        rows = db.scalars(select(ChatModelAttempt)).all()
        assert [(row.turn_id, row.reserved_microusd) for row in rows] == [(current.turn_id, 80)]


def test_real_pg_new_message_commits_before_stale_publish(state):
    lease, observation = _claim_bound(state)
    attempt, _ = state.service.reserve(lease.turn_id, lease.token, 20, LOGICAL)
    state.service.finish_attempt(
        attempt, status="SUCCEEDED", usage={}, estimated_cost_microusd=10, response_evidence={}
    )
    started = Event()

    def publish():
        started.set()
        return _code(
            lambda: state.service.publish(
                lease.turn_id,
                lease.token,
                [observation],
                lambda *_: pytest.fail("stale publish"),
            )
        )

    with ThreadPoolExecutor(max_workers=1) as workers:
        with Session(state.engine) as db, db.begin():
            replacement = _stage(state, db)
            pending = workers.submit(publish)
            assert started.wait(5)
            _wait_for_conversation_lock(state.engine)
        assert pending.result(timeout=8) == "ANALYSIS_STALE"
    with Session(state.engine) as db:
        assert db.get_one(ChatAnalysisTurn, replacement).state == "PENDING"
        assert db.get_one(ChatAnalysisTurn, lease.turn_id).state == "STALE"
        assert db.scalars(select(Observation)).all() == []


def test_real_pg_publish_commits_before_concurrent_new_message(state):
    lease, observation = _claim_bound(state)
    attempt, _ = state.service.reserve(lease.turn_id, lease.token, 20, LOGICAL)
    state.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 2, "output_tokens": 1},
        estimated_cost_microusd=10,
        response_evidence={},
        validated_result={
            "result": {
                "photos": [
                    {
                        "image_index": 1,
                        **{
                            key: observation[key]
                            for key in ("candidate_kind", "uncertainty", "findings")
                        },
                    }
                ]
            }
        },
    )
    locked, release, submitted = Event(), Event(), Event()

    def emit(db, chat, _rows):
        chat.sequence += 1
        db.add(
            Message(
                id=str(uuid4()),
                conversation_id=chat.id,
                role="assistant",
                sequence=chat.sequence,
                text="公开观察",
                cards=[],
                created_at=state.stamp.value,
            )
        )
        locked.set()
        assert release.wait(5)

    def next_input():
        submitted.set()
        return _stage(state)

    with ThreadPoolExecutor(max_workers=2) as workers:
        publisher = workers.submit(
            state.service.publish, lease.turn_id, lease.token, [observation], emit
        )
        assert locked.wait(5)
        future = workers.submit(next_input)
        assert submitted.wait(5)
        _wait_for_conversation_lock(state.engine)
        release.set()
        publisher.result(timeout=8)
        next_id = future.result(timeout=8)
    with Session(state.engine) as db:
        assert db.get_one(ChatAnalysisTurn, lease.turn_id).state == "PUBLISHED"
        assert db.get_one(ChatAnalysisTurn, next_id).state == "PENDING"
        assert [
            (row.sequence, row.role)
            for row in db.scalars(select(Message).order_by(Message.sequence))
        ] == [(1, "customer"), (2, "assistant"), (3, "customer")]


def test_real_pg_process_restart_preserves_unknown_and_never_replays(state):
    lease, _ = _claim_bound(state)
    attempt, _ = state.service.reserve(lease.turn_id, lease.token, 70, LOGICAL)
    _advance(state, 5)
    # A separate pool and service have no lease token or original process memory.
    restarted_engine = create_engine(state.engine.url).execution_options(
        schema_translate_map=state.engine.get_execution_options()["schema_translate_map"],
    )
    try:
        fresh = AnalysisService(restarted_engine, state.limits, clock=lambda: state.stamp.value)
        assert fresh.due() == [lease.turn_id]
        assert fresh.claim(lease.turn_id) is None
        assert fresh.due() == []
        with Session(restarted_engine) as db, db.begin():
            chat = db.get_one(Conversation, CHAT, with_for_update=True)
            assert fresh.stage(db, chat, input_from_seq=1).id == lease.turn_id
            row = db.get_one(ChatModelAttempt, attempt)
            assert row.state == "UNKNOWN" and row.reserved_microusd == 70
        assert (
            _code(lambda: fresh.reserve(lease.turn_id, lease.token, 1, LOGICAL)) == "ANALYSIS_STALE"
        )
    finally:
        restarted_engine.dispose()
