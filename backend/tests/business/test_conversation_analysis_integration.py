"""HTTP submission through the actual chat outbox, worker and controlled vision adapter."""

from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import _photo
from test_conversations import cards, change, create, headers, send
from test_conversations import client as _client
from test_conversations import system as _system

from aftercare.business.errors import BusinessError
from aftercare.business.models import Material
from aftercare.business.rules import now
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, ChatModelAttempt, Observation
from aftercare.conversations.analysis import process
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import Message
from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry

client = _client
system = _system


@pytest.fixture
def enabled(client, system):
    settings = SimpleNamespace(
        profile="test",
        chat_analysis_enabled=True,
        chat_vision_enabled=True,
        chat_vision_alias="economy",
        chat_analysis_max_calls=2,
        chat_analysis_max_microusd=20_000,
    )
    clock = SimpleNamespace(stamp=now())
    system.service.settings = settings
    system.service.clock = lambda: clock.stamp
    chat = ConversationService(system.service)
    client.app.state.conversations = chat
    return SimpleNamespace(
        client=client, engine=system.engine, chat=chat, settings=settings, clock=clock
    )


def advance(system, seconds=4):
    system.clock.stamp += timedelta(seconds=seconds)


def work(system, model=None):
    for turn in system.chat.analysis.due():
        process(system.chat, turn, system.settings, model)


def _linked(system, text="订单 ORDER 的商品开裂了"):
    url = create(system.client)
    message = send(system.client, url, text)
    assert message.status_code == 201, message.text
    result = change(system.client, url, message.json()["conversation"]["revision"], lines=("L01",))
    assert result.status_code == 200, result.text
    assert result.json()["conversation"]["active_case_id"]
    return url


def _read(system, url):
    return system.client.get(url, headers=headers()).json()


def test_deterministic_refund_never_enters_outbox(enabled):
    url = create(enabled.client)
    response = send(enabled.client, url, "订单 ORDER 的退款进度").json()
    assert any(c["data"].get("code") == "REFUND_NOT_FOUND" for c in cards(response))
    assert "analysis" not in response
    with Session(enabled.engine) as db:
        assert db.scalar(select(ChatAnalysisTurn.id)) is None


def test_quiet_text_then_targeted_photo_and_cannot_provide(enabled):
    url = _linked(enabled, "订单 ORDER 的商品发错了")
    before = _read(enabled, url)
    assert before["analysis"]["state"] == "COLLECTING"
    assert not [c for c in cards(before) if c["type"] == "evidence_request"]
    work(enabled)
    assert _read(enabled, url) == before
    advance(enabled)
    work(enabled)
    after = _read(enabled, url)
    prompt = next(c for c in cards(after) if c["type"] == "evidence_request")
    assert "包裹外侧标签" in prompt["data"]["prompt"]
    assert after["analysis"]["state"] == "WAITING_CUSTOMER"
    assert after["conversation"]["revision"] == before["conversation"]["revision"]
    declined = send(enabled.client, url, "暂时无法提供").json()
    assert declined["conversation"]["status"] == "HUMAN_HANDOFF"
    assert declined["analysis"]["state"] == "HUMAN_HANDOFF"
    with Session(enabled.engine) as db:
        assert db.scalar(select(ChatModelAttempt.id)) is None


def controlled_model(transports, while_sending=None, *, findings=("POSSIBLE_CRACK",), usage=True):
    import json

    def transport(_spec, messages, tools, *_args):
        assert not tools
        images = [
            item["image_url"]["url"] for item in messages[-1].content if item["type"] == "image_url"
        ]
        assert images and all(item.startswith("data:image/jpeg;base64,") for item in images)
        transports.append(images)
        if while_sending:
            while_sending()
        return AIMessage(
            content=json.dumps(
                {
                    "photos": [
                        {
                            "image_index": index + 1,
                            "candidate_kind": "ITEM",
                            "uncertainty": "CLEAR",
                            "findings": list(findings),
                        }
                        for index in range(len(images))
                    ]
                }
            ),
            usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
            if usage
            else None,
        )

    stamp = date(2026, 11, 11)
    registry = Registry(
        version="controlled",
        models=[
            ModelSpec(
                alias="economy",
                provider="test",
                model="controlled",
                endpoint="https://example.invalid",
                api_key_env="TEST_ONLY",
                compatibility="VERIFIED",
                compatibility_version="1",
                source="fixture",
                checked_at=stamp,
                price_date=stamp,
                vision=True,
                vision_checked_at=stamp,
                vision_compatibility_version="1",
                vision_input_tokens_per_image=3000,
                data_scopes=["customer_attachment"],
                input_usd_per_million=1,
                output_usd_per_million=1,
            )
        ],
    )
    return ModelRunner(registry, transport=transport)


def test_split_photo_batches_keep_observations_and_charge_only_new_photos(enabled):
    url = _linked(enabled)
    transports = []
    model = controlled_model(transports)
    first = send(enabled.client, url, "", photos=(("first.png", _photo("red")),)).json()
    assert first["analysis"]["state"] == "COLLECTING"
    advance(enabled)
    work(enabled, model)
    first_after = _read(enabled, url)
    assert first_after["analysis"]["state"] == "COMPLETE"
    first_card = next(c for c in cards(first_after) if c["type"] == "photo_observation")
    assert "疑似裂纹" in first_card["data"]["summary"]
    assert set(first_card["data"]) == {
        "issue_id",
        "generation",
        "attachment_refs",
        "summary",
        "certainty",
    }
    assert [len(images) for images in transports] == [1]
    send(enabled.client, url, "我再补充一下现场情况")
    advance(enabled)
    work(enabled, model)
    assert [len(images) for images in transports] == [1]
    assert not [c for c in cards(_read(enabled, url)) if c["type"] == "evidence_request"]
    send(enabled.client, url, "", photos=(("second.png", _photo("blue")),))
    advance(enabled)
    work(enabled, model)
    assert [len(images) for images in transports] == [1, 1]
    assert transports[0] != transports[1]
    history = [c for c in cards(_read(enabled, url)) if c["type"] == "photo_observation"]
    assert len(history) == 2 and history[0] == first_card
    with Session(enabled.engine) as db:
        assert len(list(db.scalars(select(Observation)))) == 2
        assert len(list(db.scalars(select(ChatModelAttempt)))) == 2
        assert db.scalar(select(Material.id)) is None


def test_late_vision_after_support_has_no_public_observation(enabled):
    from test_business import SUPPORT

    url = _linked(enabled)
    sent = []
    send(enabled.client, url, "", photos=(("first.png", _photo("red")),))
    advance(enabled)

    def takeover():
        reply = send(enabled.client, url, "客服已接管并核对原图。", actor=SUPPORT)
        assert reply.json()["conversation"]["status"] == "HUMAN_HANDOFF"

    work(enabled, controlled_model(sent, takeover))
    detail = _read(enabled, url)
    assert detail["analysis"]["state"] == "HUMAN_HANDOFF"
    assert len(sent) == 1 and not [c for c in cards(detail) if c["type"] == "photo_observation"]
    with Session(enabled.engine) as db:
        attempt = db.scalars(select(ChatModelAttempt)).one()
        assert attempt.state == "SUCCEEDED" and attempt.estimated_cost_microusd == 1100
        assert db.scalar(select(Observation.id)) is None


def test_large_combined_batch_is_not_partially_sent(enabled):
    url = _linked(enabled)
    for batch, color in enumerate(("red", "blue")):
        files = tuple((f"{batch}-{i}.png", _photo(color)) for i in range(4))
        assert send(enabled.client, url, "", photos=files).status_code == 201
    sent = []
    advance(enabled)
    work(enabled, controlled_model(sent))
    detail = _read(enabled, url)
    assert detail["analysis"]["state"] == "HUMAN_HANDOFF"
    assert sum(len(message["attachments"]) for message in detail["messages"]) == 8
    assert any(card["data"].get("code") == "PHOTO_BATCH_UNANALYZED" for card in cards(detail))
    assert sent == []
    with Session(enabled.engine) as db:
        assert db.scalar(select(ChatModelAttempt.id)) is None


@pytest.mark.parametrize(
    ("description", "finding", "required", "excluded"),
    [
        ("订单 ORDER 的商品发错了", "ITEM_VISIBLE", "只需再发一张包裹外侧标签", "收到的商品照片"),
        ("订单 ORDER 的商品开裂了", "NO_VISIBLE_DAMAGE", "局部照片", "商品完好"),
    ],
)
def test_photo_observations_prompt_only_specific_gap_once(
    enabled, description, finding, required, excluded
):
    url = _linked(enabled, description)
    sent = []
    model = controlled_model(sent, findings=(finding,))
    send(enabled.client, url, "", photos=(("first.png", _photo("red")),))
    advance(enabled)
    work(enabled, model)
    detail = _read(enabled, url)
    requests = [
        card["data"]["prompt"] for card in cards(detail) if card["type"] == "evidence_request"
    ]
    assert len(requests) == 1 and required in requests[0] and excluded not in requests[0]
    send(enabled.client, url, "", photos=(("second.png", _photo("blue")),))
    advance(enabled)
    work(enabled, model)
    detail = _read(enabled, url)
    assert detail["analysis"]["state"] == "HUMAN_HANDOFF"
    assert [
        card["data"]["prompt"] for card in cards(detail) if card["type"] == "evidence_request"
    ] == requests
    assert len(sent) == 2


def test_celery_sweep_and_worker_use_persisted_turn(enabled, monkeypatch):
    from contextlib import contextmanager

    from aftercare import queue
    from aftercare.conversations import analysis as integration

    enabled.settings.redis_url = "memory://"
    monkeypatch.setattr(queue, "load_settings", lambda _: enabled.settings)

    @contextmanager
    def runtime(_profile):
        yield enabled.settings, enabled.engine, enabled.chat.business, SimpleNamespace(model=None)

    scheduler = integration.scheduler
    monkeypatch.setattr(
        integration,
        "scheduler",
        lambda engine, settings, **_kw: scheduler(
            engine, settings, clock=lambda: enabled.clock.stamp
        ),
    )
    monkeypatch.setattr("aftercare.db.guarded_engine", lambda _: enabled.engine)
    monkeypatch.setattr("aftercare.runtime.open_runtime", runtime)
    app = queue.make_queue("test")
    dispatched = []
    monkeypatch.setattr(app, "send_task", lambda name, args: dispatched.append((name, args)))
    assert app.conf.beat_schedule["scan-chat-analysis"]["task"] == "aftercare.chat_analysis_sweep"
    url = _linked(enabled)
    app.tasks["aftercare.chat_analysis_sweep"].run()
    assert dispatched == []
    advance(enabled)
    app.tasks["aftercare.chat_analysis_sweep"].run()
    assert len(dispatched) == 1
    name, args = dispatched[0]
    assert name == "aftercare.chat_analysis"
    app.tasks[name].run(*args)
    app.tasks[name].run(*args)
    assert len([c for c in cards(_read(enabled, url)) if c["type"] == "evidence_request"]) == 1


def test_confirmed_new_issue_keeps_only_its_own_photos_in_input(enabled):
    url = _linked(enabled)
    first = send(enabled.client, url, "", photos=(("old.png", _photo("red")),)).json()
    old_attachment = first["messages"][0]["attachments"][0]["id"]
    previous_issue = first["analysis"]["issue_id"]
    new_message = send(
        enabled.client, url, "同一订单另一件商品发错了", photos=(("new.png", _photo("blue")),)
    ).json()
    new_attachment = new_message["messages"][0]["attachments"][0]["id"]
    ordinary = send(enabled.client, url, "", photos=(("ordinary.png", _photo("green")),)).json()
    ordinary_attachment = ordinary["messages"][0]["attachments"][0]["id"]
    with Session(enabled.engine) as db:
        current = db.scalar(select(ChatAnalysisTurn).order_by(ChatAnalysisTurn.generation.desc()))
        # Pending new-issue confirmation cancels analysis instead of feeding ambiguous
        # follow-up images into the old issue. The original input stays immutable.
        assert current.state == "STALE"
        assert {ref["id"] for ref in current.attachments} == {old_attachment}
    advance(enabled)
    premature = []
    work(enabled, controlled_model(premature))
    assert premature == []  # No model may consume either issue while scope is unresolved.
    confirmed = change(
        enabled.client, url, ordinary["conversation"]["revision"], lines=("L01",), new=True
    ).json()
    assert confirmed["analysis"]["issue_id"] != previous_issue
    advance(enabled)
    sent = []
    work(enabled, controlled_model(sent))
    assert [len(images) for images in sent] == [1]
    observations = [c for c in cards(_read(enabled, url)) if c["type"] == "photo_observation"]
    assert [c["data"]["attachment_refs"] for c in observations] == [[new_attachment]]
    with Session(enabled.engine) as db:
        latest = db.scalar(select(ChatAnalysisTurn).order_by(ChatAnalysisTurn.generation.desc()))
        assert [item["id"] for item in latest.attachments] == [new_attachment]
    assert {old_attachment, ordinary_attachment} <= {
        item["id"] for message in _read(enabled, url)["messages"] for item in message["attachments"]
    }


@pytest.mark.parametrize("changed", [False, True])
def test_staged_photo_keeps_original_issue_when_text_or_context_changes(
    enabled, monkeypatch, changed
):
    url = _linked(enabled) if changed else create(enabled.client)
    photo = ("staged.png", _photo("red"))
    ident = uuid4()
    put = enabled.chat.storage.put

    def interrupted(*_args):
        raise BusinessError("OBJECT_STORE_UNAVAILABLE", "fixture interruption", 503)

    monkeypatch.setattr(enabled.chat.storage, "put", interrupted)
    assert send(enabled.client, url, "", photos=(photo,), client_id=ident).status_code == 503
    follow = send(enabled.client, url, "同一订单另一件商品发错了").json()
    if changed:
        changed_context = change(
            enabled.client, url, follow["conversation"]["revision"], lines=("L01",), new=True
        )
        assert changed_context.status_code == 200
    monkeypatch.setattr(enabled.chat.storage, "put", put)
    retry = send(enabled.client, url, "", photos=(photo,), client_id=ident)
    assert retry.status_code == (409 if changed else 201), retry.text
    with Session(enabled.engine) as db:
        row = db.scalar(select(Message).where(Message.client_message_id == str(ident)))
        assert row.analysis_issue_id
        assert (row.sequence is None) is changed


@pytest.mark.parametrize("failure", ["budget", "unknown", "missing_usage"])
def test_failed_vision_hands_off_in_current_generation_and_preserves_audit(enabled, failure):
    url = _linked(enabled)
    sent = []
    if failure == "budget":
        enabled.settings.chat_analysis_max_microusd = 1

    def timeout():
        if failure == "unknown":
            raise TimeoutError

    model = controlled_model(sent, timeout, usage=failure != "missing_usage")
    assert send(enabled.client, url, "", photos=(("one.png", _photo("red")),)).status_code == 201
    advance(enabled)
    work(enabled, model)
    detail = _read(enabled, url)
    assert detail["analysis"]["state"] == "HUMAN_HANDOFF"
    assert any(card["data"].get("code") == "PHOTO_ANALYSIS_HANDOFF" for card in cards(detail))
    assert not any(card["type"] == "photo_observation" for card in cards(detail))
    with Session(enabled.engine) as db:
        assert db.scalar(select(Observation.id)) is None
        attempt = db.scalar(select(ChatModelAttempt))
        if failure == "budget":
            assert attempt is None and sent == []
        else:
            assert len(sent) == 1
            assert attempt.state == ("UNKNOWN" if failure == "unknown" else "SUCCEEDED")
            assert attempt.estimated_cost_microusd is None and attempt.reserved_microusd > 0


def test_manual_new_issue_does_not_rebind_an_already_observed_photo(enabled):
    url = _linked(enabled)
    old = send(
        enabled.client, url, "补充：之前那件商品损坏", photos=(("original.png", _photo("red")),)
    ).json()
    attachment_id = old["messages"][0]["attachments"][0]["id"]
    issue_id = old["analysis"]["issue_id"]
    advance(enabled)
    sent = []
    model = controlled_model(sent)
    work(enabled, model)
    current = _read(enabled, url)
    manual = change(
        enabled.client, url, current["conversation"]["revision"], lines=("L02",), new=True
    ).json()
    assert manual["analysis"]["issue_id"] != issue_id
    advance(enabled)
    work(enabled, model)
    assert len(sent) == 1
    with Session(enabled.engine) as db:
        observed = db.scalar(select(Observation))
        assert observed.attachment_id == attachment_id
        original = db.get_one(Message, old["messages"][0]["id"])
        assert original.analysis_issue_id == issue_id
