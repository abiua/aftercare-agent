"""Exercise typed chat originals through authenticated routes and existing reviews."""

from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, _photo
from test_chat_decisions_integration import _link
from test_chat_decisions_integration import configured as _configured
from test_conversations import change, create, headers, send
from test_conversations import client as _client
from test_conversations import system as _system

from aftercare.business.models import Material, MaterialReview
from aftercare.conversations.tables import Attachment, ChatMaterialSource

client = _client
system = _system
configured = _configured


def _originals(env):
    url, case_id = _link(env)
    photos = (_photo("green"), _photo("orange"))
    response = send(
        env.client,
        url,
        "两张照片",
        photos=(("item.png", photos[0]), ("parcel.png", photos[1])),
    )
    assert response.status_code == 201, response.text
    endpoint = url + "/decision-materials"
    candidates = env.client.get(endpoint, headers=headers(SUPPORT))
    assert candidates.status_code == 200, candidates.text
    assert candidates.headers["Cache-Control"] == "no-store"
    listing = candidates.json()
    assert [item["sha256"] for item in listing["items"]] == [
        sha256(photo).hexdigest() for photo in photos
    ]
    return url, case_id, endpoint, listing, photos


def _body(listing, index, kind, version=None):
    return {
        "attachment_id": listing["items"][index]["attachment_id"],
        "expected_issue_id": listing["issue_id"],
        "expected_case_id": listing["case_id"],
        "expected_materials_version": listing["materials_version"] if version is None else version,
        "kind": kind,
    }


def test_staff_classifies_existing_originals_then_separately_reviews_them(configured, system):
    env = configured
    _, case_id, endpoint, listing, photos = _originals(env)
    assert env.client.get(endpoint, headers=headers(OWNER)).status_code == 404
    request = _body(listing, 0, "ITEM")
    assert env.client.post(endpoint, json=request, headers=headers(OWNER)).status_code == 404
    key = uuid4().hex
    first = env.client.post(endpoint, json=request, headers=headers(SUPPORT, key))
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert (
        env.client.post(endpoint, json=request, headers=headers(SUPPORT, key)).json()
        == first.json()
    )
    assert first.json()["source"]["sha256"] == sha256(photos[0]).hexdigest()
    changed = env.client.post(
        endpoint, json=_body(listing, 0, "PARCEL", 1), headers=headers(SUPPORT)
    )
    assert (
        changed.status_code == 409
        and changed.json()["detail"]["code"] == "CHAT_MATERIAL_KIND_CONFIRMED"
    )
    stale = env.client.post(endpoint, json=_body(listing, 1, "PARCEL"), headers=headers(SUPPORT))
    assert (
        stale.status_code == 409
        and stale.json()["detail"]["code"] == "CHAT_MATERIAL_VERSION_CHANGED"
    )
    second = env.client.post(
        endpoint, json=_body(listing, 1, "PARCEL", 1), headers=headers(SUPPORT)
    )
    assert second.status_code == 200, second.text
    assert second.json()["materials_version"] == 2
    with Session(env.db) as db:
        provenance = db.scalars(select(ChatMaterialSource).order_by(ChatMaterialSource.kind)).all()
        assert [row.actor_id for row in provenance] == [SUPPORT.user_id, SUPPORT.user_id]
        assert db.scalar(select(MaterialReview.id)) is None
        for row, original_bytes in zip(provenance, photos, strict=True):
            original = db.get_one(Attachment, row.attachment_id)
            typed = db.get_one(Material, row.material_id)
            assert (
                system.objects.objects[original.object_key]
                == system.objects.objects[typed.object_key]
                == original_bytes
            )
            assert typed.sha256 == original.sha256 == row.sha256
    current = env.service.context(case_id, SUPPORT)
    assert current["eligibility"]["reason_code"] == "MATERIAL_REVIEW_REQUIRED"
    review = env.client.post(
        f"/api/cases/{case_id}/material-reviews",
        json={
            "expected_version": current["case"]["version"],
            "decision": "APPROVED",
            "notes": "人工核对原照片",
        },
        headers=headers(SUPPORT),
    )
    assert review.status_code in {200, 201}, review.text
    assert env.service.context(case_id, SUPPORT)["eligibility"]["status"] == "ELIGIBLE"
    updated = env.client.get(endpoint, headers=headers(SUPPORT)).json()
    assert [item["confirmed_kind"] for item in updated["items"]] == ["ITEM", "PARCEL"]


def test_other_issue_and_corrupt_bytes_are_rejected(configured, system):
    env = configured
    _, _, endpoint, listing, _ = _originals(env)
    other_url = create(env.client)
    other = send(env.client, other_url, "发错货了", photos=(("other.png", _photo("black")),))
    assert other.status_code == 201, other.text
    with Session(env.db) as db:
        original = db.get_one(Attachment, listing["items"][0]["attachment_id"])
        foreign = db.scalar(
            select(Attachment).where(
                ~Attachment.id.in_([item["attachment_id"] for item in listing["items"]])
            )
        )
        wrong = _body(listing, 0, "ITEM") | {"attachment_id": foreign.id}
        assert env.client.post(endpoint, json=wrong, headers=headers(SUPPORT)).status_code == 404
        system.objects.objects[original.object_key] = b"corrupt bytes"
    result = env.client.post(endpoint, json=_body(listing, 0, "ITEM"), headers=headers(SUPPORT))
    assert (
        result.status_code == 503 and result.json()["detail"]["code"] == "MATERIAL_INTEGRITY_ERROR"
    )
    with Session(env.db) as db:
        assert db.scalar(select(Material.id)) is None
        assert db.scalar(select(ChatMaterialSource.attachment_id)) is None


def test_issue_switch_during_copy_cannot_publish_to_old_or_new_case(
    configured, system, monkeypatch
):
    env = configured
    url, _, endpoint, listing, _ = _originals(env)
    put = system.objects.put
    switched = []

    def copy_while_issue_changes(key, data, content_type):
        put(key, data, content_type)
        if key.startswith("cases/"):
            candidate = send(env.client, url, "同一笔订单还发错了商品")
            assert candidate.status_code == 201, candidate.text
            selected = change(
                env.client,
                url,
                candidate.json()["conversation"]["revision"],
                lines=("L01",),
                new=True,
            )
            assert selected.status_code == 200, selected.text
            switched.append(selected.json()["conversation"]["active_case_id"])

    monkeypatch.setattr(system.objects, "put", copy_while_issue_changes)
    response = env.client.post(endpoint, json=_body(listing, 0, "ITEM"), headers=headers(SUPPORT))
    assert response.status_code == 409, response.text
    assert switched and switched[0] != listing["case_id"]
    with Session(env.db) as db:
        assert db.scalar(select(Material.id)) is None
        assert db.scalar(select(ChatMaterialSource.attachment_id)) is None


def test_private_copy_failure_retries_same_intent_without_duplicate(
    configured, system, monkeypatch
):
    from aftercare.business.errors import BusinessError

    env = configured
    _, _, endpoint, listing, _ = _originals(env)
    put = system.objects.put
    interrupted = []

    def lost_response(key, data, content_type):
        put(key, data, content_type)
        if key.startswith("cases/") and not interrupted:
            interrupted.append(key)
            raise BusinessError("CONTROLLED_COPY_INTERRUPTION", "受控对象写入响应中断", 503)

    monkeypatch.setattr(system.objects, "put", lost_response)
    payload = _body(listing, 0, "ITEM")
    key = uuid4().hex
    assert env.client.post(endpoint, json=payload, headers=headers(SUPPORT, key)).status_code == 503
    with Session(env.db) as db:
        assert db.scalar(select(Material.id)) is None
        assert db.scalar(select(ChatMaterialSource.attachment_id)) is None
    recovered = env.client.post(endpoint, json=payload, headers=headers(SUPPORT, key))
    assert recovered.status_code == 200, recovered.text
    assert (
        env.client.post(endpoint, json=payload, headers=headers(SUPPORT, key)).json()
        == recovered.json()
    )
    with Session(env.db) as db:
        assert len(db.scalars(select(Material.id)).all()) == 1
        assert len(db.scalars(select(ChatMaterialSource.attachment_id)).all()) == 1


def test_staff_reinvestigation_uses_p4_after_classification_without_customer_message(configured):
    from langgraph.checkpoint.memory import InMemorySaver

    from aftercare.intelligence.service import AgentService
    from aftercare.intelligence.tables import RunEvent

    env = configured
    url, _, endpoint, listing, _ = _originals(env)
    env.service.settings.chat_analysis_enabled = True
    env.service.settings.chat_investigation_enabled = True
    env.client.app.state.agent = AgentService(
        env.db, env.service.settings, env.service, InMemorySaver()
    )
    registered = env.client.post(endpoint, json=_body(listing, 0, "ITEM"), headers=headers(SUPPORT))
    assert registered.status_code == 200, registered.text
    key = uuid4().hex
    run = env.client.post(url + "/decisions/run", json={}, headers=headers(SUPPORT, key))
    assert run.status_code == 202, run.text
    assert (
        env.client.post(url + "/decisions/run", json={}, headers=headers(SUPPORT, key)).json()
        == run.json()
    )
    with Session(env.db) as db:
        event = db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run.json()["run_id"],
                RunEvent.kind == "decision_input",
            )
        )
        assert event.payload["graph_profile"] == "chat-investigation-v1"
        assert event.payload["investigation_arm"] == "auto"
        assert event.payload["decision_turn_id"] is None
        frozen = event.payload["scope"]["frozen_context"]
        assert "两张照片" in [row["text"] for row in frozen["messages"]]
        assert frozen["materials"][0]["id"] == registered.json()["material"]["id"]
