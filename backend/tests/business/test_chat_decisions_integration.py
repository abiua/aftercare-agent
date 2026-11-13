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


@pytest.mark.parametrize("change_after_approval", [None, "text", "material", "correction"])
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
    elif change_after_approval == "correction":
        corrected = send(env.client, url, "更正：裂痕在左上角")
        assert corrected.status_code == 201
        body = corrected.json()
        response = env.client.post(
            url + "/correction/confirm",
            headers=headers(SUPPORT),
            json={
                "message_id": body["memory"]["correction"]["message_id"],
                "expected_revision": body["conversation"]["revision"],
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "CORRECTION_SCOPE_CHANGED"
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
    history_path = url + "/decisions/history"
    first = env.client.get(history_path, params={"limit": 1}, headers=headers(SUPPORT))
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    page = first.json()
    assert page["issue_id"] == original["input"]["issue_id"]
    (latest,) = page["items"]
    assert {key: latest[key] for key in ("id", "version", "origin")} == {
        key: original[key] for key in ("id", "version", "origin")
    }
    assert set(latest) == {"id", "version", "origin", "created_at"}
    assert latest["created_at"].endswith("+00:00")
    assert page["next_before_version"] == original["version"]
    second = env.client.get(
        history_path,
        params={"limit": 1, "before_version": page["next_before_version"]},
        headers=headers(SUPPORT),
    ).json()
    assert second["next_before_version"] is None
    assert len(second["items"]) == 1
    assert second["items"][0]["id"] == original["parent_id"]
    assert second["items"][0]["origin"] == "AGENT"
    assert second["items"][0]["version"] < original["version"]
    assert env.client.get(history_path, headers=headers()).status_code == 404
    assert (
        env.client.get(history_path, params={"limit": 51}, headers=headers(SUPPORT)).status_code
        == 422
    )
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
    new_issue_page = env.client.get(history_path, headers=headers(SUPPORT)).json()
    assert new_issue_page["issue_id"] != page["issue_id"]
    assert new_issue_page["items"] == []
    assert new_issue_page["next_before_version"] is None
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


def test_chat_execution_progress_is_receipt_backed_and_bound_to_active_issue(configured):
    from aftercare.conversations.tables import Conversation

    env = configured
    url, case_id = _link(env)
    detail = lambda: env.client.get(url, headers=headers()).json()
    assert detail()["execution_progress"] is None
    offer = _offer(env, url, case_id)
    _accept(env, url, offer)
    proposal = _proposal(env, case_id)
    assert proposal.status_code == 201, proposal.text
    assert _approve(env, case_id, proposal.json()).status_code == 201
    assert detail()["execution_progress"] is None
    result = _execute(env, case_id, proposal.json())
    assert result.status_code == 200, result.text
    execution_id = result.json()["id"]
    progress = detail()["execution_progress"]
    assert progress == {
        "id": execution_id,
        "case_id": case_id,
        "action": "REFUND",
        "status": "SUCCEEDED",
        "lines": [{"line_id": "L01", "amount_cents": 39800}],
        "amount_cents": 39800,
        "simulated": True,
        "receipt_confirmed": True,
        "updated_at": progress["updated_at"],
    }
    assert env.client.get(url, headers=headers(OTHER)).status_code == 404
    with Session(env.db) as db, db.begin():
        row = db.get_one(Execution, execution_id)
        valid_receipt = row.receipt
        row.receipt = {**valid_receipt, "amount_cents": 1}
    assert detail()["execution_progress"]["status"] == "UNKNOWN"
    assert not detail()["execution_progress"]["receipt_confirmed"]
    with Session(env.db) as db, db.begin():
        row = db.get_one(Execution, execution_id)
        row.status, row.receipt = "SUBMITTING", None
    assert detail()["execution_progress"]["status"] == "SUBMITTING"
    with Session(env.db) as db, db.begin():
        row = db.get_one(Execution, execution_id)
        row.status, row.receipt = "FAILED", valid_receipt
    assert detail()["execution_progress"]["status"] == "FAILED"
    with Session(env.db) as db, db.begin():
        db.get_one(Conversation, url.rsplit("/", 1)[-1]).analysis_issue_id = str(uuid4())
    assert detail()["execution_progress"] is None
    env.client.app.state.conversations.decisions_enabled = False
    assert "execution_progress" not in detail()


def test_review_reassessment_progress_is_visible_when_previous_offer_is_stale(configured):
    from datetime import timedelta

    from aftercare.conversations import investigation_delivery as investigation
    from aftercare.conversations.analysis import process as analyze
    from aftercare.conversations.tables import InvestigationRequest

    env = configured
    url, case_id = _link(env)
    old_offer = _offer(env, url, case_id)
    assert old_offer
    settings = env.service.settings
    settings.chat_analysis_enabled = True
    settings.chat_investigation_enabled = True
    settings.chat_analysis_max_calls = 2
    settings.chat_analysis_max_microusd = 20_000
    settings.chat_vision_enabled = False
    stamp = env.service.clock()
    env.service.clock = lambda: stamp
    chat = ConversationService(env.service)
    env.client.app.state.conversations = chat
    version = env.service.context(case_id, SUPPORT)["case"]["version"]
    env.service.upload(
        case_id,
        SUPPORT,
        kind="ITEM",
        content=_photo("purple"),
        expected_version=version,
        idempotency_key="changed-material-while-offer-open",
    )
    version = env.service.context(case_id, SUPPORT)["case"]["version"]
    env.service.review_materials(
        case_id,
        SUPPORT,
        expected_version=version,
        idempotency_key="new-material-review",
        decision="APPROVED",
        notes="人工重新核对当前材料版本。",
    )
    stamp += timedelta(seconds=4)
    (turn,) = chat.analysis.due()
    analyze(chat, turn, settings, None)
    assert investigation._stage(env.service, turn) is not None
    current = env.client.get(url, headers=headers()).json()
    assert current["investigation_progress"] == {"state": "QUEUED"}
    agent = AgentService(env.db, settings, env.service, InMemorySaver())
    investigation.process(env.service, agent, turn)
    with Session(env.db) as db:
        request = db.get_one(InvestigationRequest, turn)
        assert request.state == "BLOCKED" and request.reason == "CASE_RUN_ACTIVE"
        assert request.run_id is None
    assert env.client.get(url, headers=headers()).json()["investigation_progress"] == {
        "state": "NEEDS_SUPPORT",
    }


def test_accepted_chat_offer_handoff_is_scoped_and_submission_pins_exact_ref(configured):
    env = configured
    url, case_id = _link(env)
    path = f"/api/cases/{case_id}"

    def preview():
        response = env.client.get(path, headers=headers(SUPPORT))
        assert response.status_code == 200, response.text
        return response.json()["chat_proposal"]

    assert preview()["status"] == "NO_OFFER"
    offer = _offer(env, url, case_id)
    assert preview()["status"] == "WAITING_CUSTOMER"
    assert preview()["offer"] is None
    _accept(env, url, offer)
    ready = preview()
    assert ready["status"] == "READY" and ready["case_id"] == case_id
    assert _ref(ready["offer"]) == _ref(offer)
    assert ready["offer"]["lines"] == offer["lines"]
    assert "chat_proposal" not in env.client.get(path, headers=headers()).json()
    assert "chat_proposal" not in env.client.get(path, headers=_approval_headers()).json()
    payload = {
        "expected_version": env.service.context(case_id, SUPPORT)["case"]["version"],
        "action": offer["action"],
        "lines": offer["lines"],
        "policy_ids": offer["policy_ids"],
        "notes": "CONTROL: submit exact accepted offer",
        "expected_offer": _ref(offer),
    }
    wrong = {**payload, "expected_offer": {**_ref(offer), "id": str(uuid4())}}
    denied = env.client.post(path + "/proposals", headers=headers(SUPPORT), json=wrong)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "DECISION_STALE"
    actor_headers = headers(SUPPORT)
    result = env.client.post(path + "/proposals", headers=actor_headers, json=payload)
    assert result.status_code == 201, result.text
    assert (
        env.client.post(path + "/proposals", headers=actor_headers, json=payload).json()
        == result.json()
    )
    assert (
        env.client.post(path + "/proposals", headers=actor_headers, json=wrong).status_code == 409
    )
    assert _execute(env, case_id, result.json()).status_code == 409


@pytest.mark.parametrize("change", ["withdraw", "text", "expiry"])
def test_chat_proposal_preview_hides_no_longer_accepted_offer(configured, change):
    from datetime import timedelta

    from aftercare.decisions.tables import Offer

    env = configured
    url, case_id = _link(env)
    offer = _offer(env, url, case_id)
    _accept(env, url, offer)
    if change == "withdraw":
        answer = env.client.post(
            url + "/decisions/offer/respond",
            headers=headers(),
            json={
                "ref": _ref(offer),
                "response": {"action": "REQUEST_CHANGE", "reason": "要换货"},
            },
        )
        assert answer.status_code == 200
    elif change == "text":
        assert send(env.client, url, "我不要退款，要换货").status_code == 201
    else:
        with Session(env.db) as db, db.begin():
            db.get_one(Offer, offer["id"]).expires_at = env.service.clock() - timedelta(seconds=1)
    data = env.client.get(f"/api/cases/{case_id}", headers=headers(SUPPORT)).json()["chat_proposal"]
    assert data["status"] == ("WITHDRAWN" if change == "withdraw" else "STALE")
    assert data["offer"] is None


@pytest.mark.parametrize("action", ["ACCEPT", "MODIFY"])
def test_adopting_custom_candidate_gaps_publishes_exact_items(configured, action):
    from aftercare.conversations.decisions import ConversationDecisions
    from aftercare.decisions.contracts import Candidate

    env = configured
    url, case_id = _link(env)
    _evidence(env.service, case_id)
    current = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()
    decisions = ConversationDecisions(env.service)
    gaps = ["拍清受损杯子的杯口", "补拍能区分两只杯子的商品标签"]
    option = {
        "id": "custom-gap",
        "action": "REQUEST_EVIDENCE",
        "target_line_ids": ["L01"],
        "missing_evidence": gaps,
        "rationale": "仅核对商品与损坏情况",
    }
    with Session(env.db) as db, db.begin():
        scope = decisions.resolve(db, SUPPORT, url.rsplit("/", 1)[-1], current["issue_id"])
        candidate = Candidate(
            run_id=str(uuid4()),
            graph_version="controlled",
            route_version="controlled",
            model_binding_sha256="0" * 64,
            options=[option],
        )
        decisions.records.record_candidate(db, SUPPORT, scope, candidate)
    original = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"]
    review_url = url + "/decisions/review"
    base = {"ref": _ref(original), "expected_review_id": None}
    for empty in ([], [""], ["   "]):
        rejected = env.client.post(
            review_url,
            headers=headers(SUPPORT),
            json={
                **base,
                "review": {
                    "action": "MODIFY",
                    "reason": "补证",
                    "replacement": {**option, "missing_evidence": empty},
                },
            },
        )
        assert rejected.status_code == 422
    body = {
        **base,
        "review": {
            "action": action,
            "reason": "内部理由",
            **({"option_id": option["id"]} if action == "ACCEPT" else {"replacement": option}),
        },
    }
    key = headers(SUPPORT)
    accepted = env.client.post(review_url, headers=key, json=body)
    assert accepted.status_code == 200, accepted.text
    assert env.client.post(review_url, headers=key, json=body).json() == accepted.json()
    messages = env.client.get(url, headers=headers()).json()["messages"]
    assert sum(all(gap in row["text"] for gap in gaps) for row in messages) == 1
    assert "内部理由" not in messages[-1]["text"]
    assert (
        env.client.get(
            url + "/decisions/history/" + original["id"], headers=headers(SUPPORT)
        ).json()
        == original
    )


def test_correction_blocks_inflight_candidate_and_recovers_same_case(configured):
    from aftercare.conversations.decisions import ConversationDecisions

    env = configured
    url, case_id = _link(env)
    original = send(
        env.client, url, "我想申请退款，补充原来那张照片", photos=[("original.png", _photo("blue"))]
    ).json()["messages"][0]
    attachment = original["attachments"][0]
    started = env.client.post(url + "/decisions/run", headers=headers(SUPPORT), json={})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    env.agent.process(run_id)
    assert pending(env.db) == [run_id]

    corrected = send(env.client, url, "更正：破损位置是左上角").json()
    source = corrected["messages"][0]
    later = send(env.client, url, "左上角外壳还有一道裂痕").json()
    assert later["memory"]["state"] == "CORRECTION_NEEDS_SUPPORT"
    assert later["memory"]["correction"]["message_id"] == source["id"]
    assert later["memory"]["latest_update"]["text"] == "左上角外壳还有一道裂痕"
    assert later["memory"]["confirmed_context"]["case_id"] == case_id
    assert later["memory"]["recent_attachments"][0]["attachment_id"] == attachment["id"]

    project(env.service, run_id)
    with Session(env.db) as db:
        stale = db.get(DecisionProjection, run_id)
        assert (stale.state, stale.reason, stale.snapshot_id) == ("BLOCKED", "DECISION_STALE", None)
    assert env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"] is None
    assert env.client.get(url, headers=headers(OTHER)).status_code == 404
    assert env.client.get(url, headers=headers()).json()["memory"] == later["memory"]

    confirm_url = url + "/correction/confirm"
    values = {
        "message_id": source["id"],
        "expected_revision": later["conversation"]["revision"],
        "retained_attachment_ids": [attachment["id"]],
        "evidence_reason": "更正前原图也能看到左上角的裂痕",
    }
    assert env.client.post(confirm_url, headers=headers(), json=values).status_code == 403
    rejected = env.client.post(
        confirm_url,
        headers=headers(SUPPORT),
        json={**values, "retained_attachment_ids": [str(uuid4())]},
    )
    assert rejected.status_code == 422
    confirmed = env.client.post(confirm_url, headers=headers(SUPPORT), json=values)
    assert confirmed.status_code == 200, confirmed.text
    memory = confirmed.json()["memory"]
    assert memory["state"] == "CURRENT"
    assert memory["confirmed_context"]["case_id"] == case_id
    assert memory["confirmed_correction"]["source_message_id"] == source["id"]
    assert (
        memory["confirmed_correction"]["retained_attachments"][0]["sha256"] == attachment["sha256"]
    )

    env.client.app.state.conversations = ConversationService(env.service)
    refreshed = env.client.get(url, headers=headers()).json()
    refreshed_memory = refreshed["memory"]
    assert "evidence_reason" not in refreshed_memory["confirmed_correction"]
    assert env.client.get(url, headers=headers(SUPPORT)).json()["memory"] == memory
    assert original in refreshed["messages"]
    from aftercare.investigation.input import build_input

    with Session(env.db) as db, db.begin():
        scope = ConversationDecisions(env.service).resolve(
            db, SUPPORT, refreshed["conversation"]["id"], memory["issue_id"]
        )
    frozen = scope.frozen_context
    assert [item["id"] for item in frozen["messages"]] == [source["id"], later["messages"][0]["id"]]
    assert frozen["case"]["description"] == source["text"]
    assert frozen["attachments"][0]["id"] == attachment["id"]
    role_input = build_input(scope).role_view("fact")
    assert role_input["primary_customer_statement"] == source["id"]
    assert [item["ref"]["source_id"] for item in role_input["customer_statements"]] == [
        source["id"],
        later["messages"][0]["id"],
    ]
    assert role_input["attachments"][0]["ref"]["source_id"] == attachment["id"]
    assert role_input["requested_customer_action"] == refreshed_memory["requested_action"]
    assert role_input["requested_customer_action"]["action"] == "REFUND"
    assert role_input["requested_customer_action"]["message_id"] == original["id"]


@pytest.mark.parametrize(
    "statement, action",
    [
        ("不要退款了，换货", "REPLACE"),
        ("不用退了", "WITHDRAWN"),
        ("我不想退款了，只想了解退款政策", "WITHDRAWN"),
        ("撤销退款申请", "WITHDRAWN"),
        ("取消退款申请", "WITHDRAWN"),
        ("我想退款或者换货", "NEEDS_CLARIFICATION"),
        ("我想退货退款", "REFUND"),
        ("我想退货或退款", "NEEDS_CLARIFICATION"),
        ("我想退货退款或者换货", "NEEDS_CLARIFICATION"),
        ("请问退款政策是什么？", None),
        ("补充照片，谢谢", None),
    ],
)
def test_memory_distinguishes_current_request_from_negation_and_supplement(statement, action):
    from aftercare.conversations.memory import requested_action

    assert requested_action(statement) == action


def test_withdrawal_blocks_current_offer_until_customer_clarifies(configured):
    env = configured
    url, case_id = _link(env)
    requested = send(env.client, url, "我想退款").json()
    assert requested["memory"]["requested_action"]["action"] == "REFUND"
    offer = _offer(env, url, case_id)
    withdrawn = send(env.client, url, "不用退了").json()
    assert withdrawn["memory"]["requested_action"]["action"] == "WITHDRAWN"
    assert withdrawn["memory"]["state"] == "CORRECTION_NEEDS_SUPPORT"
    assert env.client.get(url + "/decisions/offer", headers=headers()).json()["offer"] is None
    attempted = env.client.post(
        url + "/decisions/offer/respond",
        headers=headers(),
        json={"ref": _ref(offer), "response": {"action": "ACCEPT"}},
    )
    assert attempted.status_code == 409

    def confirm(data):
        return env.client.post(
            url + "/correction/confirm",
            headers=headers(SUPPORT),
            json={
                "message_id": data["memory"]["correction"]["message_id"],
                "expected_revision": data["conversation"]["revision"],
            },
        )

    held = confirm(withdrawn)
    assert held.status_code == 409
    assert held.json()["detail"]["code"] == "CORRECTION_REQUEST_UNCLEAR"
    clarified = send(env.client, url, "我想换货").json()
    response = confirm(clarified)
    assert response.status_code == 200, response.text
    assert response.json()["memory"]["requested_action"]["action"] == "REPLACE"
    assert response.json()["conversation"]["active_case_id"] == case_id
    assert env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"] is None


def test_confirmed_correction_fences_upload_staged_on_prior_basis(configured, monkeypatch):
    env = configured
    url, _ = _link(env)
    chat = env.client.app.state.conversations
    published = send(env.client, url, "", photos=[("published.png", _photo("green"))]).json()
    original = published["messages"][0]["attachments"][0]
    corrected = send(env.client, url, "更正：破损位置是左上角").json()
    original_find = chat._find
    conversation_id = corrected["conversation"]["id"]
    upload_id = uuid4()
    key = uuid4().hex
    locks = 0

    def interleave(db, ident, user, *, lock=False):
        nonlocal locks
        if lock and user.user_id == OWNER.user_id:
            locks += 1
            if locks == 2:
                with Session(env.db) as check:
                    staged = chat._message(check, ident, str(upload_id))
                    assert staged is not None and staged.sequence is None
                chat.confirm_correction(
                    conversation_id,
                    SUPPORT,
                    message_id=corrected["memory"]["correction"]["message_id"],
                    expected_revision=corrected["conversation"]["revision"],
                    retained_attachment_ids=[original["id"]],
                    evidence_reason="原图对应当前商品",
                    key="confirm-while-staged",
                )
        return original_find(db, ident, user, lock=lock)

    monkeypatch.setattr(chat, "_find", interleave)
    image = ("unpublished.png", _photo("red"))
    for retry_key in (key, key, uuid4().hex):
        response = send(env.client, url, "", photos=[image], client_id=upload_id, key=retry_key)
        assert response.status_code == 410, response.text
    page = env.client.get(url, headers=headers()).json()
    assert [item["attachment_id"] for item in page["memory"]["recent_attachments"]] == [
        original["id"]
    ]
    assert env.client.get(original["content_path"], headers=headers()).content == _photo("green")
