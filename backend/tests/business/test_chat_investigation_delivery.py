"""Recover automatic starts from published P2 turns without provider calls."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import OWNER
from test_conversation_analysis_integration import _linked, advance, work
from test_conversation_analysis_integration import enabled as _enabled
from test_conversations import client as _client
from test_conversations import send
from test_conversations import system as _system

from aftercare.conversation_analysis.tables import ChatAnalysisTurn
from aftercare.conversations import investigation_delivery as delivery
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import InvestigationRequest
from aftercare.db import User
from aftercare.intelligence.service import RunError
from aftercare.intelligence.tables import AgentRun, RunMutation
from aftercare.investigation.input import build_input

client = _client
system = _system
enabled = _enabled


def _published(env):
    env.settings.chat_decisions_enabled = True
    env.settings.chat_investigation_enabled = True
    env.chat = ConversationService(env.chat.business)
    env.client.app.state.conversations = env.chat
    url = _linked(env)
    business = env.chat.business
    assert delivery.pending(business) == []
    advance(env)
    work(env)
    pending = delivery.pending(business)
    assert len(pending) == 1
    return url, business, pending[0]


def test_staged_intent_recovers_committed_agent_start_after_worker_loss(enabled, monkeypatch):
    _, business, turn_id = _published(enabled)
    calls = []
    run_id = str(uuid4())

    def start(case_id, actor, **kwargs):
        calls.append((case_id, actor, kwargs))
        assert actor.user_id == OWNER.user_id
        assert kwargs["graph_profile"] == "chat-investigation-v1"
        assert kwargs["decision_turn_id"] == turn_id
        assert kwargs["investigation_arm"] == "auto"
        scope = kwargs["decision_scope"]
        projected = build_input(scope)
        committed = {ref.id: ref.sha256 for ref in scope.input_refs}
        for row in projected.evidence:
            if row.ref.source_kind in {"ORDER_LINE", "POLICY"}:
                assert committed[row.ref.source_id] == row.ref.version_or_sha256
        with Session(enabled.engine) as db, db.begin():
            request = db.get_one(InvestigationRequest, turn_id)
            assert request.state == "PENDING"
            assert request.scope == kwargs["decision_scope"].model_dump(mode="json")
            db.add(
                AgentRun(
                    id=run_id,
                    case_id=case_id,
                    thread_id=run_id,
                    owner_id=actor.user_id,
                    actor=actor.model_dump(),
                    mode="single",
                    strategy="routed",
                    status="NEEDS_REVIEW",
                    reason="INTERRUPTED_MODEL_CALL",
                    case_version=kwargs["expected_version"],
                    message=kwargs["message"],
                    cycle=0,
                    event_seq=0,
                    created_at=business.clock(),
                    updated_at=business.clock(),
                )
            )
            db.add(
                RunMutation(
                    case_id=case_id,
                    actor_id=actor.user_id,
                    key=kwargs["idempotency_key"],
                    digest="d" * 64,
                    run_id=run_id,
                )
            )
        return {"id": run_id}

    agent = SimpleNamespace(start=start)
    settle = delivery._settle

    def interrupted(*args, **kwargs):
        raise RuntimeError("controlled worker crash after run commit")

    monkeypatch.setattr(delivery, "_settle", interrupted)
    with pytest.raises(RuntimeError, match="after run commit"):
        delivery.process(business, agent, turn_id)
    assert delivery.pending(business) == [turn_id]
    monkeypatch.setattr(delivery, "_settle", settle)
    delivery.process(business, agent, turn_id)
    delivery.process(business, agent, turn_id)
    assert len(calls) == 1
    assert delivery.pending(business) == []
    with Session(enabled.engine) as db:
        request = db.get_one(InvestigationRequest, turn_id)
        assert (request.state, request.run_id) == ("STARTED", run_id)
        assert db.get_one(AgentRun, run_id).reason == "INTERRUPTED_MODEL_CALL"


@pytest.mark.parametrize("change", ["new_text", "handoff", "revocation", "disabled"])
def test_automatic_start_rechecks_staged_turn_and_original_owner(enabled, change):
    url, business, turn_id = _published(enabled)
    assert delivery._stage(business, turn_id) is not None
    if change == "new_text":
        assert send(enabled.client, url, "补充：位置在边缘").status_code == 201
    elif change == "handoff":
        assert (
            send(enabled.client, url, "暂时无法提供").json()["conversation"]["status"]
            == "HUMAN_HANDOFF"
        )
    elif change == "disabled":
        enabled.settings.chat_investigation_enabled = False
    else:
        with Session(enabled.engine) as db, db.begin():
            db.get_one(User, OWNER.user_id).enabled = False

    def start(*args, **kwargs):
        pytest.fail("obsolete or unauthorized automatic investigation called AgentService")

    delivery.process(business, SimpleNamespace(start=start), turn_id)
    with Session(enabled.engine) as db:
        request = db.get_one(InvestigationRequest, turn_id)
        assert request.run_id is None
        expected = {"revocation": "BLOCKED", "disabled": "PENDING"}
        assert request.state == expected.get(change, "CANCELED")
        assert db.scalar(select(RunMutation.run_id)) is None
        assert db.get_one(ChatAnalysisTurn, turn_id).state in {"PUBLISHED", "SUPERSEDED"}


def test_terminal_agent_rejection_is_retained_without_retry(enabled):
    _, business, turn_id = _published(enabled)
    calls = []

    def start(*args, **kwargs):
        calls.append(kwargs["decision_turn_id"])
        raise RunError("CASE_RUN_ACTIVE", "Existing unrelated run")

    for _ in range(2):
        delivery.process(business, SimpleNamespace(start=start), turn_id)
    assert calls == [turn_id]
    with Session(enabled.engine) as db:
        row = db.get_one(InvestigationRequest, turn_id)
        assert (row.state, row.reason) == ("BLOCKED", "CASE_RUN_ACTIVE")


def test_real_agent_program_gate_auto_projects_without_reprompting(enabled):
    from langgraph.checkpoint.memory import InMemorySaver
    from test_business import SUPPORT
    from test_conversations import cards, headers

    from aftercare.conversations.decision_delivery import project
    from aftercare.intelligence.service import AgentService
    from aftercare.intelligence.tables import ModelAttempt

    url, business, turn_id = _published(enabled)
    before = enabled.client.get(url, headers=headers()).json()
    enabled.settings.model_enabled = False
    agent = AgentService(enabled.engine, enabled.settings, business, InMemorySaver())
    delivery.process(business, agent, turn_id)
    with Session(enabled.engine) as db:
        request = db.get_one(InvestigationRequest, turn_id)
        assert (request.state, request.reason) == ("STARTED", None)
        run_id = request.run_id
    agent.process(run_id)
    project(business, run_id)
    snapshot = enabled.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"]
    assert snapshot["original_run_id"] == run_id
    assert snapshot["candidate"]["options"][0]["action"] == "REQUEST_EVIDENCE"
    after = enabled.client.get(url, headers=headers()).json()
    assert cards(before) == cards(after)
    with Session(enabled.engine) as db:
        assert db.scalar(select(ModelAttempt.run_id)) is None

    review = enabled.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": {key: snapshot[key] for key in ("id", "version", "sha256")},
            "review": {"action": "REJECT", "reason": "客服已核对并记录"},
            "expected_review_id": None,
        },
    )
    assert review.status_code == 200, review.text
    public_prompt_count = sum(card["type"] == "evidence_request" for card in cards(after))
    for text, expected in (("谢谢", "SUPERSEDED"), ("补充：箱内另有碎片", "STARTED")):
        assert send(enabled.client, url, text).status_code == 201
        advance(enabled)
        work(enabled)
        queued = delivery.pending(business)
        assert len(queued) == 1, text
        (next_turn,) = queued
        delivery.process(business, agent, next_turn)
        with Session(enabled.engine) as db:
            result = db.get_one(InvestigationRequest, next_turn)
            assert result.state == expected
            if text == "谢谢":
                assert result.run_id is None
                assert db.scalars(select(AgentRun.id)).all() == [run_id]
                assert db.scalar(select(ModelAttempt.run_id)) is None
                ack = enabled.client.get(url, headers=headers()).json()
                assert (
                    sum(card["type"] == "evidence_request" for card in cards(ack))
                    == public_prompt_count
                )
            else:
                assert result.run_id is not None and result.run_id != run_id


def test_oversized_current_turn_hands_off_once_with_originals_retained(enabled, monkeypatch):
    from test_conversations import headers

    url, business, turn_id = _published(enabled)
    before = enabled.client.get(url, headers=headers()).json()
    monkeypatch.setattr("aftercare.conversations.decisions.MAX_DECISION_INPUT_REFS", 1)

    def start(*args, **kwargs):
        pytest.fail("overflow must never reach AgentService")

    for _ in range(2):
        delivery.process(business, SimpleNamespace(start=start), turn_id)
    after = enabled.client.get(url, headers=headers()).json()
    assert after["conversation"]["status"] == "HUMAN_HANDOFF"
    assert len(after["messages"]) == len(before["messages"]) + 1
    assert after["messages"][:-1] == before["messages"]
    assert "原消息和照片会保留" in after["messages"][-1]["text"]
    with Session(enabled.engine) as db:
        request = db.get_one(InvestigationRequest, turn_id)
        assert (request.state, request.reason, request.run_id) == (
            "BLOCKED",
            "DECISION_INPUT_TOO_LARGE",
            None,
        )


@pytest.mark.pg
def test_pg_concurrent_auto_delivery_and_material_confirmation_each_commit_once(system):
    from concurrent.futures import ThreadPoolExecutor
    from os import environ
    from threading import Barrier

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from test_business import SUPPORT, _photo
    from test_conversations import TOKENS, headers

    from aftercare.auth import token_hash
    from aftercare.business.models import Material
    from aftercare.business.rules import now
    from aftercare.business.seed import initialize
    from aftercare.business.service import BusinessService
    from aftercare.conversations.api import router
    from aftercare.conversations.tables import ChatMaterialSource
    from aftercare.db import Base
    from aftercare.intelligence.service import AgentService

    dsn = environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat migration PostgreSQL required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "127.0.0.1" and url.port not in {5432, 55450, 55451}
    assert url.database == url.username == "chat_migration" and not url.query
    admin = create_engine(url, hide_parameters=True)
    schema = "investigation_concurrency_" + uuid4().hex
    db_engine = None
    created = False
    try:
        with admin.begin() as db:
            cluster = db.scalar(text("SELECT current_setting('cluster_name')"))
            assert cluster == environ["AFTERCARE_CHAT_MIGRATION_CLUSTER"]
            assert cluster.startswith("aftercare-chat-migration-")
            db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        db_engine = create_engine(
            url,
            connect_args={
                "options": f"-csearch_path={schema},public -clock_timeout=5000 -cstatement_timeout=12000"
            },
        )
        Base.metadata.create_all(db_engine)
        initialize(db_engine, system.seed)
        with Session(db_engine) as db, db.begin():
            db.add_all(
                User(
                    id=actor.user_id,
                    role=actor.role,
                    customer_id=actor.customer_id,
                    display_name=actor.user_id,
                    token_hash=token_hash(TOKENS[actor.user_id]),
                    enabled=True,
                )
                for actor in (OWNER, SUPPORT)
            )
        settings = SimpleNamespace(
            profile="test",
            model_enabled=False,
            chat_analysis_enabled=True,
            chat_decisions_enabled=True,
            chat_investigation_enabled=True,
            chat_vision_enabled=False,
            chat_analysis_max_calls=2,
            chat_analysis_max_microusd=20_000,
        )
        clock = SimpleNamespace(stamp=now())
        business = BusinessService(
            db_engine, settings, storage=system.objects, clock=lambda: clock.stamp
        )
        chat = ConversationService(business)
        agent = AgentService(db_engine, settings, business, InMemorySaver())
        app = FastAPI()
        app.state.engine, app.state.conversations = db_engine, chat
        app.include_router(router, prefix="/api")
        with TestClient(app) as client:
            env = SimpleNamespace(client=client, chat=chat, settings=settings, clock=clock)
            chat_url = _linked(env)
            advance(env)
            work(env)
            (turn_id,) = delivery.pending(business)
            barrier = Barrier(2)

            def wake():
                barrier.wait(timeout=5)
                delivery.process(business, agent, turn_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                wakes = [pool.submit(wake) for _ in range(2)]
                for wake_result in wakes:
                    wake_result.result(timeout=20)
            photo = send(client, chat_url, "原始商品照片", photos=(("image.png", _photo("green")),))
            assert photo.status_code == 201, photo.text
            materials_url = chat_url + "/decision-materials"
            candidates = client.get(materials_url, headers=headers(SUPPORT)).json()
            classify_barrier = Barrier(2)

            def classify(kind):
                classify_barrier.wait(timeout=5)
                return client.post(
                    materials_url,
                    headers=headers(SUPPORT),
                    json={
                        "attachment_id": candidates["items"][0]["attachment_id"],
                        "kind": kind,
                        "expected_issue_id": candidates["issue_id"],
                        "expected_case_id": candidates["case_id"],
                        "expected_materials_version": candidates["materials_version"],
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(classify, kind) for kind in ("ITEM", "PARCEL")]
                results = [attempt.result(timeout=20) for attempt in attempts]
            assert sorted(item.status_code for item in results) == [200, 409]
        with Session(db_engine) as db:
            request = db.get_one(InvestigationRequest, turn_id)
            assert request.state == "STARTED"
            assert db.scalars(select(AgentRun.id)).all() == [request.run_id]
            assert db.scalars(select(RunMutation.run_id)).all() == [request.run_id]
            binding = db.scalars(select(ChatMaterialSource)).one()
            material = db.scalars(select(Material)).one()
            assert binding.material_id == material.id and binding.sha256 == material.sha256
    finally:
        if db_engine:
            db_engine.dispose()
        if created:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
