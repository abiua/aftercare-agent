"""Customer-safe progress from an actual offline automatic investigation."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session
from test_business import OTHER, SUPPORT
from test_chat_investigation_delivery import _published
from test_chat_investigation_delivery import enabled as _enabled
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.conversations import investigation_delivery as delivery
from aftercare.conversations.decision_delivery import project
from aftercare.conversations.tables import InvestigationRequest
from aftercare.intelligence.service import AgentService

client = _client
system = _system
enabled = _enabled


def test_progress_reads_bound_request_run_and_current_published_head(enabled):
    url, business, turn_id = _published(enabled)

    def progress():
        response = enabled.client.get(url, headers=headers())
        assert response.status_code == 200
        return response.json()["investigation_progress"]

    assert progress() is None
    assert delivery._stage(business, turn_id) is not None
    assert progress() == {"state": "QUEUED"}
    enabled.settings.model_enabled = False
    agent = AgentService(
        enabled.engine, enabled.settings, business, InMemorySaver(), dispatch=lambda _run: None
    )
    delivery.process(business, agent, turn_id)
    with Session(enabled.engine) as db:
        run_id = db.get_one(InvestigationRequest, turn_id).run_id
    assert run_id
    assert progress() == {"state": "QUEUED"}
    agent.process(run_id)
    assert progress() == {"state": "CHECKING"}
    project(business, run_id)
    assert progress() == {"state": "PENDING_SUPPORT"}
    assert "investigation_progress" not in enabled.client.get(url, headers=headers(SUPPORT)).json()
    assert enabled.client.get(url, headers=headers(OTHER)).status_code == 404
    assert send(enabled.client, url, "补充：位置在边缘").status_code == 201
    assert progress() is None


def test_customer_progress_ignores_unbound_request_and_flag_off(enabled):
    url, business, turn_id = _published(enabled)
    assert delivery._stage(business, turn_id) is not None
    with Session(enabled.engine) as db, db.begin():
        request = db.get_one(InvestigationRequest, turn_id)
        scope = request.scope
        request.scope = {**scope, "line_ids": ["unrelated-line"]}
    assert enabled.client.get(url, headers=headers()).json()["investigation_progress"] is None
    enabled.settings.chat_investigation_enabled = False
    assert "investigation_progress" not in enabled.client.get(url, headers=headers()).json()


@pytest.mark.parametrize("photo_state", [None, "PENDING", "RUNNING"])
def test_material_review_queues_new_native_candidate_without_customer_message(enabled, photo_state):
    from sqlalchemy import func, select
    from test_business import _photo
    from test_conversation_analysis_integration import advance, controlled_model, work

    from aftercare.business.models import Approval, Case, Execution
    from aftercare.conversation_analysis.tables import (
        ChatAnalysisTurn,
        ChatModelAttempt,
        Observation,
    )
    from aftercare.conversations.tables import Message
    from aftercare.decisions.tables import DecisionSnapshot

    url, business, old_turn = _published(enabled)
    conversation_id = url.split("/")[-1]
    enabled.settings.model_enabled = False
    agent = AgentService(
        enabled.engine, enabled.settings, business, InMemorySaver(), dispatch=lambda _: None
    )

    def begin(turn):
        delivery.process(business, agent, turn)
        with Session(enabled.engine) as db:
            return db.get_one(InvestigationRequest, turn).run_id

    def progress():
        return enabled.client.get(url, headers=headers()).json()["investigation_progress"]

    old_run = begin(old_turn)
    assert old_run
    agent.process(old_run)
    project(business, old_run)
    with Session(enabled.engine) as db:
        original = db.scalar(select(DecisionSnapshot).where(DecisionSnapshot.run_id == old_run))
        original_id, original_input = original.id, original.input
        case_id = original_input["case_id"]
        case_version = db.get_one(Case, case_id).version
        messages = db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "customer",
            )
        )

    for kind, color in (("ITEM", "green"), ("PARCEL", "blue")):
        business.upload(
            case_id,
            SUPPORT,
            kind=kind,
            content=_photo(color),
            expected_version=case_version,
            idempotency_key=f"material-trigger-{kind}",
        )
        with Session(enabled.engine) as db:
            case_version = db.get_one(Case, case_id).version
    assert delivery.pending(business) == []
    pending_turn = None
    photo_id = None
    if photo_state:
        response = send(enabled.client, url, "", photos=(("latest.png", _photo("red")),))
        assert response.status_code == 201
        photo_id = next(
            row["attachments"][0]["id"]
            for row in response.json()["messages"]
            if row["role"] == "customer"
        )
        messages += 1
        with Session(enabled.engine) as db:
            pending_turn = db.scalar(
                select(ChatAnalysisTurn.id).where(
                    ChatAnalysisTurn.conversation_id == conversation_id,
                    ChatAnalysisTurn.state == "PENDING",
                )
            )
        assert pending_turn and pending_turn != old_turn
    request = {
        "decision": "APPROVED",
        "notes": "已人工确认当前版材料。",
        "expected_version": case_version,
        "idempotency_key": "material-trigger-review",
    }
    reviews = []
    transports = []

    def review():
        reviews.append(business.review_materials(case_id, SUPPORT, **request))

    if photo_state == "RUNNING":
        advance(enabled)
        work(enabled, controlled_model(transports, while_sending=review))
    else:
        review()
    (reviewed,) = reviews
    assert business.review_materials(case_id, SUPPORT, **request) == reviewed
    with Session(enabled.engine) as db:
        turns = list(
            db.scalars(
                select(ChatAnalysisTurn)
                .where(
                    ChatAnalysisTurn.conversation_id == conversation_id,
                )
                .order_by(ChatAnalysisTurn.generation)
            )
        )
        new_turn = turns[-1].id
        assert turns[-1].state == ("PUBLISHED" if photo_state == "RUNNING" else "PENDING")
        if pending_turn:
            assert new_turn == pending_turn
            assert photo_id in {ref["id"] for ref in turns[-1].attachments}
        else:
            assert turns[-1].reason == "MATERIAL_REVIEW_CHANGED"
            assert turns[-1].generation == turns[-2].generation + 1
            assert turns[-2].id == old_turn
        assert db.get_one(DecisionSnapshot, original_id).input == original_input
    assert progress() is None
    if photo_state != "RUNNING":
        advance(enabled)
        work(enabled, controlled_model(transports) if photo_state else None)
    assert delivery.pending(business) == [new_turn]
    assert delivery._stage(business, old_turn) is None
    new_run = begin(new_turn)
    assert new_run and new_run != old_run
    assert progress() == {"state": "QUEUED"}
    agent.process(new_run)
    assert progress() == {"state": "CHECKING"}
    project(business, new_run)
    assert progress() == {"state": "PENDING_SUPPORT"}
    delivered = enabled.client.get(url, headers=headers()).json()
    assert "已转客服" in delivered["messages"][-1]["text"]
    assert delivered["messages"][-1]["cards"][0]["data"]["code"] == "PENDING_SUPPORT"
    project(business, new_run)
    assert enabled.client.get(url, headers=headers()).json() == delivered
    support_view = enabled.client.get(url + "/decisions", headers=headers(SUPPORT))
    assert support_view.status_code == 200
    support_snapshot = support_view.json()["snapshot"]
    assert support_snapshot["original_run_id"] == new_run
    with Session(enabled.engine) as db:
        replacement = db.scalar(select(DecisionSnapshot).where(DecisionSnapshot.run_id == new_run))
        assert replacement.input["dependency_sha256"] != original_input["dependency_sha256"]
        assert any(
            row["id"] == reviewed["id"] for row in replacement.input["frozen_context"]["reviews"]
        )
        assert db.get_one(DecisionSnapshot, original_id).input == original_input
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "customer",
                )
            )
            == messages
        )
        attempts = list(db.scalars(select(ChatModelAttempt)))
        observations = list(db.scalars(select(Observation)))
        if photo_state:
            assert len(transports) == len(attempts) == len(observations) == 1
            assert attempts[0].turn_id == new_turn and observations[0].attachment_id == photo_id
        else:
            assert not (transports or attempts or observations)
        assert db.scalar(select(Approval.id)) is None
        assert db.scalar(select(Execution.id)) is None
    # The notice must not merely display a stale card: support can actually review it.
    response = enabled.client.post(
        url + "/decisions/review", headers=headers(SUPPORT),
        json={"ref": {key: support_snapshot[key] for key in ("id", "version", "sha256")},
              "review": {"action": "REJECT", "reason": "自动调查未完成，由客服继续核对"},
              "expected_review_id": None},
    )
    assert response.status_code == 200, response.text


def test_material_review_preserves_pending_customer_text(enabled):
    from sqlalchemy import select
    from test_business import _photo
    from test_conversation_analysis_integration import _linked, advance, work

    from aftercare.business.models import Case
    from aftercare.conversation_analysis.tables import ChatAnalysisTurn
    from aftercare.conversations.service import ConversationService

    enabled.settings.chat_decisions_enabled = True
    enabled.settings.chat_investigation_enabled = True
    enabled.chat = ConversationService(enabled.chat.business)
    enabled.client.app.state.conversations = enabled.chat
    url = _linked(enabled)
    response = send(enabled.client, url, "暂时无法提供另一张照片。")
    assert response.status_code == 201
    ident = url.split("/")[-1]
    case_id = response.json()["conversation"]["active_case_id"]
    with Session(enabled.engine) as db:
        turn = db.scalar(
            select(ChatAnalysisTurn).where(
                ChatAnalysisTurn.conversation_id == ident, ChatAnalysisTurn.state == "PENDING"
            )
        )
        turn_id, generation = turn.id, turn.generation
        version = db.get_one(Case, case_id).version
    business = enabled.chat.business
    business.upload(
        case_id,
        SUPPORT,
        kind="ITEM",
        content=_photo("green"),
        expected_version=version,
        idempotency_key="pending-text-material",
    )
    with Session(enabled.engine) as db:
        version = db.get_one(Case, case_id).version
    reviewed = business.review_materials(
        case_id,
        SUPPORT,
        decision="NEEDS_MORE",
        notes="已查看现有照片。",
        expected_version=version,
        idempotency_key="pending-text-review",
    )
    with Session(enabled.engine) as db:
        turn = enabled.chat.analysis._latest(db, ident)
        assert (turn.id, turn.generation, turn.state, turn.reason) == (
            turn_id,
            generation,
            "PENDING",
            None,
        )
    advance(enabled)
    work(enabled)
    actor, raw, _ = delivery._stage(business, turn_id)
    assert actor.role == "customer"
    assert any("暂时无法提供" in row["text"] for row in raw["frozen_context"]["messages"])
    assert any(row["id"] == reviewed["id"] for row in raw["frozen_context"]["reviews"])


def test_offline_review_reassessment_does_not_repeat_photo_unavailable_notice(enabled):
    from test_business import _photo
    from test_conversation_analysis_integration import advance, work

    url, business, _ = _published(enabled)
    enabled.settings.chat_vision_enabled = False

    def notices():
        detail = enabled.client.get(url, headers=headers()).json()
        return sum(
            card["data"].get("code") == "PHOTO_ANALYSIS_UNAVAILABLE"
            for row in detail["messages"]
            for card in row["cards"]
        )

    uploaded = send(enabled.client, url, "", photos=(("original.png", _photo("red")),))
    assert uploaded.status_code == 201
    case_id = uploaded.json()["conversation"]["active_case_id"]
    advance(enabled)
    work(enabled)
    assert notices() == 1
    version = business.context(case_id, SUPPORT)["case"]["version"]
    business.upload(
        case_id,
        SUPPORT,
        kind="ITEM",
        content=_photo("red"),
        expected_version=version,
        idempotency_key="confirmed-offline-image",
    )
    version = business.context(case_id, SUPPORT)["case"]["version"]
    business.review_materials(
        case_id,
        SUPPORT,
        decision="NEEDS_MORE",
        notes="已核对已有材料。",
        expected_version=version,
        idempotency_key="review-offline-image",
    )
    advance(enabled)
    work(enabled)
    (turn,) = delivery.pending(business)
    assert delivery._stage(business, turn) is not None
    assert notices() == 1

    assert (
        send(enabled.client, url, "", photos=(("followup.png", _photo("blue")),)).status_code == 201
    )
    advance(enabled)
    work(enabled)
    assert notices() == 2
