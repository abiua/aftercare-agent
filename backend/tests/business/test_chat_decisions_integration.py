"""P3 route and financial transaction integration without provider calls."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import APPROVER, OTHER, OWNER, SUPPORT, _create, _evidence, _photo, _propose
from test_conversations import change, create, headers, send
from test_conversations import client as _client
from test_conversations import system as _system

from aftercare.auth import token_hash
from aftercare.business.api import router as business_router
from aftercare.business.models import Case, Execution, Material, Proposal
from aftercare.conversations.decision_delivery import pending, project
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import DecisionProjection
from aftercare.db import User
from aftercare.intelligence.service import AgentService

client = _client
system = _system
APPROVAL_TOKEN = "approval-fixture-token".ljust(40, "-")


@pytest.fixture
def configured(client, system):
    client.app.state.business = system.service
    client.app.include_router(business_router, prefix="/api")
    system.service.settings = SimpleNamespace(
        profile="test", chat_decisions_enabled=True, model_enabled=False
    )
    chat = ConversationService(system.service)
    client.app.state.conversations = chat
    agent = AgentService(system.engine, system.service.settings, system.service, InMemorySaver())
    client.app.state.agent = agent
    with Session(system.engine) as db, db.begin():
        db.add(
            User(
                id=APPROVER.user_id,
                role="approver",
                display_name="approver",
                token_hash=token_hash(APPROVAL_TOKEN),
                enabled=True,
            )
        )
    return SimpleNamespace(client=client, db=system.engine, service=system.service, agent=agent)


def _approval_headers():
    return {"Authorization": f"Bearer {APPROVAL_TOKEN}", "Idempotency-Key": uuid4().hex}


def _link(env):
    url = create(env.client)
    message = send(env.client, url, "订单 ORDER 的商品已经损坏").json()
    linked = change(env.client, url, message["conversation"]["revision"], lines=("L01",))
    assert linked.status_code == 200, linked.text
    assert linked.json()["features"]["chat_decisions"] is True
    return url, linked.json()["conversation"]["active_case_id"]


def _proposal(env, case_id, *, amount=39800):
    version = env.service.context(case_id, SUPPORT)["case"]["version"]
    return env.client.post(
        f"/api/cases/{case_id}/proposals",
        headers=headers(SUPPORT),
        json={
            "expected_version": version,
            "action": "REFUND",
            "notes": "人工核对订单",
            "lines": [{"line_id": "L01", "amount_cents": amount}],
            "policy_ids": [],
        },
    )


def _approve(env, case_id, proposal):
    version = env.service.context(case_id, SUPPORT)["case"]["version"]
    return env.client.post(
        f"/api/cases/{case_id}/approvals",
        headers=_approval_headers(),
        json={
            "expected_version": version,
            "proposal_id": proposal["id"],
            "decision": "APPROVED",
            "notes": "财务独立确认",
        },
    )


def _execute(env, case_id, proposal):
    version = env.service.context(case_id, SUPPORT)["case"]["version"]
    return env.client.post(
        f"/api/cases/{case_id}/execute",
        headers=headers(SUPPORT),
        json={
            "expected_version": version,
            "proposal_id": proposal["id"],
        },
    )


def _ref(payload):
    return {key: payload[key] for key in ("id", "version", "sha256")}


def _offer(env, url, case_id):
    # Execute the existing graph with no model. Its real WAITING_INPUT gate records
    # the original candidate. The worker projector reuses that persisted run.
    started = env.client.post(url + "/decisions/run", headers=headers(SUPPORT), json={})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    env.agent.process(run_id)
    assert pending(env.db) == [run_id]
    project(env.service, run_id)
    project(env.service, run_id)
    assert pending(env.db) == []
    original_response = env.client.get(url + "/decisions", headers=headers(SUPPORT))
    assert original_response.status_code == 200, original_response.text
    original = original_response.json()["snapshot"]
    assert original["origin"] == "AGENT"
    assert original["original_run_id"] == run_id
    assert original["candidate"]["options"][0]["action"] == "REQUEST_EVIDENCE"
    events = env.agent.events(case_id, run_id, SUPPORT, 0)
    assert all(
        "scope" not in event["payload"] for event in events if event["kind"] == "decision_input"
    )
    _evidence(env.service, case_id)
    with Session(env.db) as db:
        evidence_ids = list(db.scalars(select(Material.id).where(Material.case_id == case_id)))
    review = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(original),
            "expected_review_id": None,
            "review": {
                "action": "MODIFY",
                "reason": "已经按规定核对原始材料",
                "replacement": {
                    "id": "verified-refund",
                    "action": "REFUND",
                    "lines": [{"line_id": "L01", "amount_cents": 39800}],
                    "evidence_ids": evidence_ids,
                    "policy_ids": ["POL01"],
                    "rationale": "人工核对完整金额",
                },
                "new_evidence_ids": evidence_ids,
            },
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["program_check"]["status"] == "PASSED", review.json()
    assert review.json()["snapshot"]["id"] != original["id"]
    historical = env.client.get(
        url + "/decisions/history/" + original["id"], headers=headers(SUPPORT)
    )
    assert historical.json() == original
    customer = env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"]
    assert set(customer) == {
        "id",
        "version",
        "sha256",
        "action",
        "lines",
        "policy_ids",
        "expires_at",
        "customer_decision",
    }
    assert customer["lines"] == [{"line_id": "L01", "amount_cents": 39800}]
    assert env.client.get(url + "/decisions/offer", headers=headers(OTHER)).status_code == 404
    assert env.client.get(url + "/decisions", headers=headers()).status_code == 404
    return customer


def _accept(env, url, offer):
    body = {"ref": _ref(offer), "response": {"action": "ACCEPT"}}
    actor_headers = headers()
    first = env.client.post(url + "/decisions/offer/respond", headers=actor_headers, json=body)
    assert first.status_code == 200, first.text
    assert (
        env.client.post(url + "/decisions/offer/respond", headers=actor_headers, json=body).json()
        == first.json()
    )
    return first.json()


@pytest.mark.parametrize("change_after_approval", [None, "text", "material"])
def test_managed_case_requires_offer_customer_and_finance_in_existing_routes(
    configured, change_after_approval
):
    env = configured
    url, case_id = _link(env)
    assert _proposal(env, case_id).status_code == 409
    offer = _offer(env, url, case_id)
    denied = _proposal(env, case_id)
    assert (
        denied.status_code == 409
        and denied.json()["detail"]["code"] == "DECISION_CUSTOMER_CONSENT_REQUIRED"
    )
    consent = _accept(env, url, offer)
    proposal = _proposal(env, case_id)
    assert proposal.status_code == 201, proposal.text
    assert _execute(env, case_id, proposal.json()).status_code == 409
    # The lifecycle version and a clearly irrelevant acknowledgement leave the offer valid.
    assert send(env.client, url, "谢谢！").status_code == 201
    approval = _approve(env, case_id, proposal.json())
    assert approval.status_code == 201, approval.text
    if change_after_approval == "text":
        assert send(env.client, url, "我不要退款，要换货").status_code == 201
    elif change_after_approval == "material":
        version = env.service.context(case_id, SUPPORT)["case"]["version"]
        env.service.upload(
            case_id,
            OWNER,
            kind="ITEM",
            content=_photo("red"),
            expected_version=version,
            idempotency_key="changed-evidence",
        )
    execution = _execute(env, case_id, proposal.json())
    if change_after_approval:
        assert execution.status_code == 409
        assert execution.json()["detail"]["code"] in {"DECISION_STALE", "STALE_PROPOSAL"}
    else:
        assert execution.status_code == 200 and execution.json()["status"] == "SUCCEEDED", (
            execution.text
        )
    with Session(env.db) as db:
        binding = db.get_one(Proposal, proposal.json()["id"]).decision_binding
        assert binding["offer"] == _ref(offer)
        assert binding["customer_decision_id"] == consent["id"]
        assert len(list(db.scalars(select(Execution)))) == int(change_after_approval is None)
        assert db.scalar(select(DecisionProjection.state)) == "PUBLISHED"


def test_existing_case_enrolls_without_duplicate_and_legacy_proposal_remains_readable(configured):
    env = configured
    original_case = _create(env.service)
    _evidence(env.service, original_case)
    legacy = _propose(env.service, original_case)
    _, linked = _link(env)
    assert linked == original_case
    with Session(env.db) as db:
        case = db.get_one(Case, linked)
        assert case.decision_conversation_id is not None
        assert case.decision_legacy_through_version >= legacy["version"]
        assert db.get_one(Proposal, legacy["id"]).decision_binding is None
        assert len(list(db.scalars(select(Case)))) == 1
    env.service.settings.chat_decisions_enabled = False
    assert _proposal(env, linked).status_code == 409
    assert (
        _approve(env, linked, legacy).json()["detail"]["code"]
        == "DECISION_CUSTOMER_CONSENT_REQUIRED"
    )
    stored = env.client.get(f"/api/cases/{linked}/proposals", headers=headers(SUPPORT)).json()
    assert stored["items"][0]["id"] == legacy["id"]
    assert _execute(env, linked, legacy).status_code == 409


def test_frozen_business_policy_and_original_messages_survive_mutable_updates(configured):
    from aftercare.business.models import Policy

    env = configured
    url, case_id = _link(env)
    offer = _offer(env, url, case_id)
    response = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()
    original = response["snapshot"]
    frozen = original["input"]["frozen_context"]
    assert frozen["messages"][0]["text"] == "订单 ORDER 的商品已经损坏"
    assert frozen["order"]["items"][0]["paid_cents"] == 39800
    assert frozen["policies"]["documents"]
    assert frozen["materials"] and frozen["reviews"]
    assert "frozen_context" not in offer and "input" not in offer
    document = frozen["policies"]["documents"][0]
    with Session(env.db) as db, db.begin():
        policy = db.get_one(Policy, (document["id"], document["version"]))
        policy.data = {**policy.data, "text": document["text"] + " 测试中的新条款内容。"}
        case = db.get_one(Case, case_id)
        case.facts = {**case.facts, "test_mutation": "new"}
        case.facts_version += 1
    lookup = env.client.get(url + "/decisions/history/" + original["id"], headers=headers(SUPPORT))
    assert lookup.status_code == 200 and lookup.json() == original
    assert env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"] is None


def test_customer_rejection_cannot_authorize_proposal_and_latest_review_is_required(configured):
    env = configured
    url, case_id = _link(env)
    offer = _offer(env, url, case_id)
    rejected = env.client.post(
        url + "/decisions/offer/respond",
        headers=headers(),
        json={
            "ref": _ref(offer),
            "response": {"action": "REQUEST_CHANGE", "reason": "需要考虑其他方案"},
        },
    )
    assert rejected.status_code == 200
    assert _proposal(env, case_id).json()["detail"]["code"] == "DECISION_CUSTOMER_CONSENT_REQUIRED"
    current = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()
    assert current["latest_review"]["reason"] == "已经按规定核对原始材料"
    assert current["customer_decision"]["action"] == "REQUEST_CHANGE"
    assert current["customer_decision"]["reason"] == "需要考虑其他方案"
    assert current["customer_decision"]["offer"] == _ref(offer)
    request = {
        "ref": _ref(current["snapshot"]),
        "expected_review_id": None,
        "review": {"action": "TAKE_OVER", "reason": "请客服重新核对"},
    }
    assert (
        env.client.post(
            url + "/decisions/review", headers=headers(SUPPORT), json=request
        ).status_code
        == 409
    )
    request["expected_review_id"] = current["expected_review_id"]
    taken = env.client.post(url + "/decisions/review", headers=headers(SUPPORT), json=request)
    assert taken.status_code == 200 and taken.json()["offer"] is None
    conversation = env.client.get(url, headers=headers()).json()["conversation"]
    assert conversation["status"] == "HUMAN_HANDOFF"
    assert env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"] is None


def test_staff_readback_retains_failed_accept_recheck(configured, monkeypatch):
    from aftercare.business.errors import BusinessError

    env = configured
    url, case_id = _link(env)
    _offer(env, url, case_id)
    current = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()
    candidate = current["snapshot"]["candidate"]["options"][0]

    def no_longer_eligible(*_args, **_kwargs):
        raise BusinessError("ACTION_NOT_ELIGIBLE", "受控复核规则拒绝")

    monkeypatch.setattr(env.service, "_validate_plan", no_longer_eligible)
    review = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(current["snapshot"]),
            "expected_review_id": current["expected_review_id"],
            "review": {"action": "ACCEPT", "option_id": candidate["id"], "reason": "重新核对"},
        },
    )
    assert review.status_code == 200 and review.json()["offer"] is None
    persisted = env.client.get(url + "/decisions", headers=headers(SUPPORT))
    assert persisted.headers["cache-control"] == "no-store"
    assert persisted.json()["latest_review"]["program_check"] == {
        "status": "BLOCKED",
        "code": "ACTION_NOT_ELIGIBLE",
    }
    assert env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"] is None


def test_adopted_or_explicit_evidence_gap_is_public_but_internal_reason_is_private(configured):
    env = configured
    url, _ = _link(env)
    run = env.client.post(url + "/decisions/run", headers=headers(SUPPORT), json={}).json()
    env.agent.process(run["run_id"])
    project(env.service, run["run_id"])
    current = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()
    original = current["snapshot"]
    choice = original["candidate"]["options"][0]
    assert choice["action"] == "REQUEST_EVIDENCE"
    response = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(original),
            "expected_review_id": None,
            "review": {
                "action": "ACCEPT",
                "option_id": choice["id"],
                "reason": "内部复核理由甲",
            },
        },
    )
    assert response.status_code == 200 and response.json()["offer"] is None
    history = env.client.get(url, headers=headers()).json()["messages"]
    public = history[-1]["text"]
    assert "商品照片" in public and "包裹外观照片" in public and "内部复核理由甲" not in public
    response = env.client.post(
        url + "/decisions/review",
        headers=headers(SUPPORT),
        json={
            "ref": _ref(original),
            "expected_review_id": response.json()["id"],
            "review": {"action": "REQUEST_EVIDENCE", "reason": "内部复核理由乙"},
            "evidence_gaps": ["ISSUE_DESCRIPTION"],
        },
    )
    assert response.status_code == 200 and response.json()["offer"] is None
    text = env.client.get(url, headers=headers()).json()["messages"][-1]["text"]
    assert "问题发生的位置或具体表现" in text and "内部复核理由乙" not in text
    assert env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["latest_review"][
        "evidence_gaps"
    ] == ["问题发生的位置或具体表现"]


def test_same_run_http_key_replays_original_after_ack_and_projection(configured):
    env = configured
    url, _ = _link(env)
    key = headers(SUPPORT)
    first = env.client.post(url + "/decisions/run", headers=key, json={})
    assert first.status_code == 202
    env.agent.process(first.json()["run_id"])
    project(env.service, first.json()["run_id"])
    assert send(env.client, url, "谢谢！").status_code == 201
    repeat = env.client.post(url + "/decisions/run", headers=key, json={})
    assert repeat.status_code == 202 and repeat.json()["run_id"] == first.json()["run_id"]


def test_structured_customer_withdrawal_stops_an_approved_proposal(configured):
    from aftercare.decisions.tables import CustomerDecision

    env = configured
    url, case_id = _link(env)
    offer = _offer(env, url, case_id)
    initial = _accept(env, url, offer)
    proposal = _proposal(env, case_id)
    assert _approve(env, case_id, proposal.json()).status_code == 201
    body = {"ref": _ref(offer), "response": {"action": "REQUEST_CHANGE", "reason": "想换货"}}
    key = headers()
    withdrew = env.client.post(url + "/decisions/offer/respond", headers=key, json=body)
    assert withdrew.status_code == 200 and withdrew.json()["id"] != initial["id"]
    assert (
        env.client.post(url + "/decisions/offer/respond", headers=key, json=body).json()
        == withdrew.json()
    )
    assert (
        _execute(env, case_id, proposal.json()).json()["detail"]["code"]
        == "DECISION_CUSTOMER_CONSENT_REQUIRED"
    )
    assert (
        env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"][
            "customer_decision"
        ]["action"]
        == "REQUEST_CHANGE"
    )
    with Session(env.db) as db:
        rows = list(db.scalars(select(CustomerDecision).order_by(CustomerDecision.version)))
        assert [(row.version, row.response["action"]) for row in rows] == [
            (1, "ACCEPT"),
            (2, "REQUEST_CHANGE"),
        ]


def test_history_stays_readable_after_confirmed_new_issue(configured):
    env = configured
    url, case_id = _link(env)
    _offer(env, url, case_id)
    original = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"]
    message = send(env.client, url, "同一订单另一个商品发错了").json()
    changed = change(env.client, url, message["conversation"]["revision"], lines=("L01",), new=True)
    assert (
        changed.status_code == 200 and changed.json()["conversation"]["active_case_id"] != case_id
    )
    assert (
        env.client.get(
            url + "/decisions/history/" + original["id"], headers=headers(SUPPORT)
        ).json()
        == original
    )
    other = create(env.client)
    assert (
        env.client.get(
            other + "/decisions/history/" + original["id"], headers=headers(SUPPORT)
        ).status_code
        == 404
    )
    assert (
        env.client.get(url + "/decisions/history/" + original["id"], headers=headers()).status_code
        == 404
    )


@pytest.mark.pg
def test_pg_concurrent_customer_accept_and_withdraw_are_versioned_once(system):
    import os
    import re
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from test_business import MemoryObjects
    from test_conversations import TOKENS

    from aftercare.business.service import BusinessService, initialize
    from aftercare.conversations.api import router
    from aftercare.db import Base
    from aftercare.decisions.tables import CustomerDecision

    dsn = os.environ.get("AFTERCARE_CHAT_MIGRATION_DSN")
    if not dsn:
        pytest.skip("explicit disposable chat decision PostgreSQL required")
    connection_url = make_url(dsn)
    assert connection_url.drivername == "postgresql+psycopg"
    assert connection_url.host == "127.0.0.1" and connection_url.port not in {5432, 55450, 55451}
    assert connection_url.database == connection_url.username == "chat_migration"
    assert not connection_url.query
    engine = create_engine(connection_url, hide_parameters=True)
    schema = "decision_consent_" + uuid4().hex
    owned = False
    try:
        with engine.begin() as db:
            identity = db.execute(
                text("SELECT current_database(), current_user, current_setting('cluster_name')")
            ).one()
            assert identity[:2] == ("chat_migration", "chat_migration")
            assert re.fullmatch(r"aftercare-chat-migration-[0-9a-f]{32}", identity[2])
            db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
            owned = True
        scoped = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped)
        initialize(scoped, system.seed)
        with Session(scoped) as db, db.begin():
            for actor in (OWNER, OTHER, SUPPORT, APPROVER):
                raw_token = APPROVAL_TOKEN if actor == APPROVER else TOKENS[actor.user_id]
                db.add(
                    User(
                        id=actor.user_id,
                        role=actor.role,
                        customer_id=actor.customer_id,
                        display_name=actor.user_id,
                        token_hash=token_hash(raw_token),
                        enabled=True,
                    )
                )
        settings = SimpleNamespace(profile="test", chat_decisions_enabled=True, model_enabled=False)
        business = BusinessService(scoped, settings, storage=MemoryObjects())
        chat = ConversationService(business)
        agent = AgentService(scoped, settings, business, InMemorySaver())
        app = FastAPI()
        app.state.engine, app.state.agent = scoped, agent
        app.state.conversations, app.state.business = chat, business
        app.include_router(router, prefix="/api")
        app.include_router(business_router, prefix="/api")
        with TestClient(app) as http:
            env = SimpleNamespace(client=http, db=scoped, service=business, agent=agent)
            url, case_id = _link(env)
            offer = _offer(env, url, case_id)

            def respond(action):
                return http.post(
                    url + "/decisions/offer/respond",
                    headers=headers(),
                    json={
                        "ref": _ref(offer),
                        "response": {"action": action},
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                accepts = list(pool.map(respond, ("ACCEPT", "ACCEPT")))
                assert sorted(result.status_code for result in accepts) == [200, 409]
                proposal = _proposal(env, case_id)
                assert proposal.status_code == 201
                assert _approve(env, case_id, proposal.json()).status_code == 201
                withdrawals = list(pool.map(respond, ("REJECT", "REQUEST_CHANGE")))
                assert sorted(result.status_code for result in withdrawals) == [200, 409]
            assert _execute(env, case_id, proposal.json()).status_code == 409
            with Session(scoped) as db:
                rows = list(db.scalars(select(CustomerDecision).order_by(CustomerDecision.version)))
                assert len(rows) == 2 and [row.version for row in rows] == [1, 2]
                assert rows[0].response["action"] == "ACCEPT"
                assert rows[1].response["action"] in {"REJECT", "REQUEST_CHANGE"}
                assert db.scalar(select(Execution.id)) is None
    finally:
        if owned:
            with engine.begin() as db:
                db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                assert db.scalar(text("SELECT to_regnamespace(:name) IS NULL"), {"name": schema})
        engine.dispose()
