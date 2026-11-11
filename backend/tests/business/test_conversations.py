"""P1 route and transaction tests using a local database and controlled private storage."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_business import OTHER, OWNER, SUPPORT, TESTER, _photo
from test_business import system as _system_fixture

from aftercare.auth import token_hash
from aftercare.business.errors import BusinessError
from aftercare.business.models import Case, Execution, Material, Order, OrderLine
from aftercare.business.rules import now
from aftercare.conversations.api import router
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import Attachment, CaseLink, Message
from aftercare.db import User

system = _system_fixture

ACTORS = (OWNER, OTHER, SUPPORT, TESTER)
TOKENS = {actor.user_id: f"test-token-{actor.user_id}".ljust(40, "-") for actor in ACTORS}


@pytest.fixture
def client(system):
    with Session(system.engine) as db, db.begin():
        for actor in ACTORS:
            db.add(
                User(
                    id=actor.user_id,
                    role=actor.role,
                    customer_id=actor.customer_id,
                    display_name=actor.user_id,
                    token_hash=token_hash(TOKENS[actor.user_id]),
                    enabled=True,
                )
            )
    app = FastAPI()
    app.state.engine = system.engine
    app.state.conversations = ConversationService(system.service)
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def headers(actor=OWNER, key=None):
    return {
        "Authorization": f"Bearer {TOKENS[actor.user_id]}",
        "Idempotency-Key": key or uuid4().hex,
    }


def create(client):
    response = client.post("/api/conversations", json={}, headers=headers())
    assert response.status_code == 201, response.text
    return "/api/conversations/" + response.json()["id"]


def send(client, url, text, *, photos=(), actor=OWNER, client_id=None, key=None):
    multipart = [
        ("text", (None, text)),
        ("client_message_id", (None, str(client_id or uuid4()))),
        *(("files", (name, data, "image/png")) for name, data in photos),
    ]
    return client.post(url + "/messages", files=multipart, headers=headers(actor, key))


def change(client, url, revision, *, order="ORDER", lines=(), new=False, key=None):
    return client.post(
        url + "/context",
        headers=headers(key=key),
        json={
            "order_id": order,
            "line_ids": list(lines),
            "new_issue": new,
            "expected_revision": revision,
        },
    )


def cards(data):
    return [card for message in data["messages"] for card in message["cards"]]


def test_refund_text_with_explicit_order_does_not_ask_for_product(client, system):
    url = create(client)
    response = send(client, url, "  请查订单 ORDER 的退款进度  ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["conversation"]["revision"] == 2
    assert body["conversation"]["active_order_id"] == "ORDER"
    assert body["conversation"]["active_case_id"] is None
    assert body["messages"][0]["text"] == "  请查订单 ORDER 的退款进度  "
    assert body["messages"][1]["text"] == "目前没查到这笔订单的退款处理记录。"
    assert cards(body) == [
        {"type": "status", "data": {"code": "REFUND_NOT_FOUND", "refunds": []}},
        {"type": "status", "data": {"code": "ORDER_CONFIRMED", "order_id": "ORDER"}},
    ]
    with Session(system.engine) as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 0
        assert db.scalar(select(func.count()).select_from(Execution)) == 0


def test_multi_photos_paging_owner_and_public_support_handoff(client, system):
    url = create(client)
    photos = (("one.png", _photo("red")), ("two.png", _photo("blue")))
    client_id, key = uuid4(), uuid4().hex
    first = send(client, url, "照片补充", photos=photos, client_id=client_id, key=key)
    assert first.status_code == 201, first.text
    photo_message = first.json()["messages"][0]
    assert len(photo_message["attachments"]) == 2
    assert [item["filename"] for item in photo_message["attachments"]] == ["one.png", "two.png"]
    assert (
        send(client, url, "照片补充", photos=photos, client_id=client_id, key=key).json()
        == first.json()
    )
    assert (
        send(client, url, "照片补充", photos=photos, client_id=client_id).json()["messages"][0]
        == photo_message
    )
    assert send(client, url, "照片补充 ", photos=photos, client_id=client_id).status_code == 409
    assert (
        send(client, url, "照片补充", photos=photos[::-1], client_id=client_id).status_code == 409
    )
    assert client.get(url, headers=headers(OTHER)).status_code == 404
    assert client.get(url, headers=headers(TESTER)).status_code == 403
    assert client.get("/api/conversations", headers=headers(OTHER)).json() == {"items": []}
    for attachment, (_, content) in zip(photo_message["attachments"], photos, strict=True):
        path = attachment["content_path"]
        owned = client.get(path, headers=headers())
        assert owned.status_code == 200 and owned.content == content
        assert owned.headers["cache-control"] == "private, no-store"
        assert client.get(path, headers=headers(OTHER)).status_code == 404
        assert client.get(path, headers=headers(SUPPORT)).content == content
    reply = send(client, url, "我是客服，已经收到照片。", actor=SUPPORT).json()
    assert reply["conversation"]["status"] == "HUMAN_HANDOFF"
    assert [item["role"] for item in reply["messages"]] == ["support"]
    continued = send(client, url, "补充的第三条消息").json()
    assert [item["role"] for item in continued["messages"]] == ["customer"]
    after, observed = 0, []
    while True:
        page = client.get(url, headers=headers(), params={"after": after, "limit": 1}).json()
        observed.extend(page["messages"])
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert [item["sequence"] for item in observed] == [1, 2, 3, 4]
    assert observed[2]["text"] == "我是客服，已经收到照片。"
    with Session(system.engine) as db:
        assert db.scalar(select(func.count()).select_from(Attachment)) == 2
        assert db.scalar(select(func.count()).select_from(Material)) == 0


def test_partial_upload_hidden_and_resumable_with_both_idempotency_keys(
    client, system, monkeypatch
):
    url = create(client)
    photos = (("one.png", _photo("red")), ("two.png", _photo("blue")))
    client_id, key = uuid4(), uuid4().hex
    real_put = system.objects.put

    def fail_second(object_key, *args):
        if "/1-" in object_key:
            raise BusinessError("OBJECT_STORE_UNAVAILABLE", "fixture write failed", 503)
        real_put(object_key, *args)

    monkeypatch.setattr(system.objects, "put", fail_second)
    assert send(client, url, "", photos=photos, client_id=client_id, key=key).status_code == 503
    assert client.get(url, headers=headers()).json()["messages"] == []
    with Session(system.engine) as db:
        staged = db.scalar(select(Message))
        assert staged.sequence is None
        image_id = db.scalar(select(Attachment.id).where(Attachment.position == 0))
    assert client.get(f"{url}/attachments/{image_id}/content", headers=headers()).status_code == 404
    assert send(client, url, "另一条消息", key=key).status_code == 409
    monkeypatch.setattr(system.objects, "put", real_put)
    recovered = send(client, url, "", photos=photos, client_id=client_id, key=key)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["messages"][0]["attachments"][0]["id"] == image_id
    assert len(system.objects.objects) == 2


def test_context_requires_range_and_keeps_case_and_attachments_separate(client, system):
    url = create(client)
    initial = send(
        client, url, "收到的商品已经损坏", photos=(("evidence.png", _photo("red")),)
    ).json()
    assert cards(initial)[0]["type"] == "order_candidates"
    assert cards(initial)[0]["data"]["orders"][0]["id"] == "ORDER"
    no_scope = change(client, url, initial["conversation"]["revision"]).json()
    assert no_scope["conversation"]["active_case_id"] is None
    assert cards(no_scope)[0]["type"] == "order_candidates"
    revision, key = no_scope["conversation"]["revision"], uuid4().hex
    linked = change(client, url, revision, lines=("L01",), key=key)
    assert linked.status_code == 200, linked.text
    case_id = linked.json()["conversation"]["active_case_id"]
    assert (
        case_id and change(client, url, revision, lines=("L01",), key=key).json() == linked.json()
    )
    assert change(client, url, revision, lines=("L02",)).status_code == 409
    revision = linked.json()["conversation"]["revision"]
    assert change(client, url, revision, lines=("L02",)).status_code == 409
    with Session(system.engine) as db, db.begin():
        db.get_one(Case, case_id).status = "RESOLVED"
    issue = send(client, url, "同一件商品还是损坏").json()
    restarted = change(client, url, issue["conversation"]["revision"], lines=("L01",), new=True)
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["conversation"]["status"] == "HUMAN_HANDOFF"
    assert restarted.json()["conversation"]["active_case_id"] is None
    assert cards(restarted.json())[0]["data"]["code"] == "CASE_SCOPE_EXISTS"
    with Session(system.engine) as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 1
        assert db.scalar(select(func.count()).select_from(CaseLink)) == 1
        assert db.scalar(select(func.count()).select_from(Material)) == 0


def _extra_order(db, ident, *, customer=OWNER.customer_id):
    original = db.get_one(Order, "ORDER")
    original_line = db.get_one(OrderLine, ("ORDER", "L01"))
    db.add(
        Order(
            id=ident,
            customer_id=customer,
            version=1,
            data={**original.data, "id": ident, "customer_id": customer},
        )
    )
    db.flush()
    db.add(
        OrderLine(
            order_id=ident,
            line_id="L01",
            product_id=original_line.product_id,
            data=original_line.data,
            quantity=original_line.quantity,
            paid_cents=original_line.paid_cents,
            refunded_cents=0,
        )
    )


def test_explicit_order_beyond_candidate_limit_and_switch_does_not_reuse_old_intent(client, system):
    with Session(system.engine) as db, db.begin():
        for i in range(22):
            _extra_order(db, f"A{i:04}")
        _extra_order(db, "Z9999")
        _extra_order(db, "O9999", customer=OTHER.customer_id)
    url = create(client)
    linked = send(client, url, "订单 Z9999 的商品开裂了").json()
    assert linked["conversation"]["active_order_id"] == "Z9999"
    assert linked["conversation"]["active_case_id"]
    old_intent = send(client, url, "商品已经损坏，还需要客服帮忙").json()
    switched = change(client, url, old_intent["conversation"]["revision"], order="ORDER", new=True)
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert body["conversation"]["active_case_id"] is None
    assert (
        change(client, url, body["conversation"]["revision"], lines=["L01"]).json()["conversation"][
            "active_case_id"
        ]
        is None
    )
    foreign = send(client, create(client), "请查 O9999 的退款进度").json()
    assert foreign["conversation"]["active_order_id"] is None
    assert "O9999" not in [row["id"] for row in cards(foreign)[0]["data"]["orders"]]


def test_refund_without_receipt_does_not_report_success_and_submitting_is_distinct(
    client, system, monkeypatch
):
    stamp = now()
    records = [
        SimpleNamespace(
            id=str(uuid4()),
            action="REFUND",
            status=status,
            amount_cents=1234,
            created_at=stamp,
            receipt=receipt,
        )
        for status, receipt in [
            ("SUCCEEDED", None),
            ("SUBMITTING", None),
            ("SUCCEEDED", {"id": "receipt"}),
        ]
    ]
    monkeypatch.setattr(system.service, "_ledger", lambda *_: records)
    data = send(client, create(client), "查 ORDER 的退款状态").json()
    ledger = next(card["data"] for card in cards(data) if card["data"]["code"] == "REFUND_LEDGER")
    assert [item["status"] for item in ledger["refunds"]] == ["UNKNOWN", "SUBMITTING", "SUCCEEDED"]
    assert [item["receipt_confirmed"] for item in ledger["refunds"]] == [False, False, True]
    assert "实际到账需另行核对" in data["messages"][-1]["text"]


def test_expired_partial_upload_cleanup_keeps_public_photo_and_tombstone(
    client, system, monkeypatch
):
    url = create(client)
    photo = ("one.png", _photo("red"))
    assert send(client, url, "", photos=(photo,)).status_code == 201
    ident, key = uuid4(), uuid4().hex
    real_put = system.objects.put

    def interrupted(key, *args):
        real_put(key, *args)
        raise BusinessError("OBJECT_STORE_UNAVAILABLE", "fixture write failed", 503)

    monkeypatch.setattr(system.objects, "put", interrupted)
    assert send(client, url, "", photos=(photo,), client_id=ident, key=key).status_code == 503
    service = ConversationService(system.service)
    assert service.cleanup_staged(execute=True) == {"eligible": 0, "cleaned": 0}
    with Session(system.engine) as db, db.begin():
        db.scalar(select(Message).where(Message.client_message_id == str(ident))).created_at = (
            now() - timedelta(hours=25)
        )
    monkeypatch.setattr(
        system.objects, "delete", lambda key: system.objects.objects.pop(key, None), raising=False
    )
    assert service.cleanup_staged() == {"eligible": 1, "cleaned": 0}
    assert service.cleanup_staged(execute=True) == {"eligible": 1, "cleaned": 1}
    assert service.cleanup_staged(execute=True) == {"eligible": 0, "cleaned": 0}
    monkeypatch.setattr(system.objects, "put", real_put)
    assert send(client, url, "", photos=(photo,), client_id=ident, key=key).status_code == 410
    assert send(client, url, "", photos=(photo,), client_id=ident).status_code == 410
    assert len(system.objects.objects) == 1
    detail = client.get(url, headers=headers()).json()
    assert len(detail["messages"]) == 2 and len(detail["messages"][0]["attachments"]) == 1


def test_same_order_new_intent_waits_for_confirmation_and_keeps_old_photos(client, system):
    url = create(client)
    damage = send(
        client, url, "订单 ORDER 的商品开裂了", photos=(("old.png", _photo("red")),)
    ).json()
    old_attachment = damage["messages"][0]["attachments"][0]
    original = change(client, url, damage["conversation"]["revision"], lines=("L01",)).json()
    old_case = original["conversation"]["active_case_id"]
    assert old_case
    corrected = send(client, url, "我之前说错了，是发错商品").json()
    assert corrected["conversation"]["active_case_id"] == old_case
    assert cards(corrected)[0]["data"]["code"] == "CORRECTION_NEEDS_SUPPORT"
    prompt = send(client, url, "此外这笔订单另一件商品发错了").json()
    assert prompt["conversation"]["active_case_id"] == old_case
    assert cards(prompt)[0]["type"] == "order_candidates"
    assert cards(prompt)[0]["data"]["new_issue"] is True
    assert [order["id"] for order in cards(prompt)[0]["data"]["orders"]] == ["ORDER"]
    with Session(system.engine) as db:
        assert db.scalar(select(func.count()).select_from(CaseLink)) == 1
    revision = prompt["conversation"]["revision"]
    retained = change(client, url, revision, lines=("L01",)).json()
    assert retained["conversation"]["active_case_id"] == old_case
    confirmed = change(client, url, retained["conversation"]["revision"], lines=("L01",), new=True)
    assert confirmed.status_code == 200, confirmed.text
    new_case = confirmed.json()["conversation"]["active_case_id"]
    assert new_case and new_case != old_case
    with Session(system.engine) as db:
        assert {case.intent for case in db.scalars(select(Case))} == {"damaged", "wrong_item"}
        assert set(db.scalars(select(CaseLink.case_id))) == {old_case, new_case}
        assert db.scalar(select(func.count()).select_from(Material)) == 0
        assert db.scalar(select(func.count()).select_from(Attachment)) == 1
    detail = client.get(url, headers=headers()).json()
    assert detail["messages"][0]["attachments"] == [old_attachment]
