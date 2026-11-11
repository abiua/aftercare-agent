"""Exercise the frozen, staged P5 reviewer flow with an actual P3 snapshot."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, TESTER, _photo
from test_chat_decisions_integration import _link, _offer
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.auth import token_hash
from aftercare.db import User
from aftercare.evaluation.api import router
from aftercare.evaluation.chat.service import BlindReviewService
from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindLabels, BlindTask
from aftercare.investigation.contracts import ORDER_OWNERSHIP_REF

client = _client
system = _system
configured = _configured
BASE = "/api/evaluation/chat-blind-reviews"
OTHER_TESTER_TOKEN = "test-blind-other-tester-token".ljust(40, "-")


@pytest.fixture
def task(configured):
    env = configured
    env.client.app.include_router(router, prefix="/api")
    url, case_id = _link(env)
    image = _photo("green")
    assert (
        send(env.client, url, "原始证据文字", photos=(("original.png", image),)).status_code == 201
    )
    _offer(env, url, case_id)
    current = env.client.get(url + "/decisions", headers=headers(SUPPORT)).json()["snapshot"]
    snapshot = env.client.get(
        url + "/decisions/history/" + current["parent_id"], headers=headers(SUPPORT)
    ).json()
    with Session(env.db) as db, db.begin():
        db.add(
            User(
                id="other-tester",
                role="tester",
                display_name="other",
                token_hash=token_hash(OTHER_TESTER_TOKEN),
                enabled=True,
            )
        )
    service = BlindReviewService(env.service)
    assignment = {
        "reviewer_id": TESTER.user_id,
        "case_label": "P5-DEV-CASE-01",
        "input_snapshot_id": snapshot["id"],
        "candidate_snapshot_ids": [snapshot["id"]],
    }
    assigned = service.assign(**assignment)
    assert service.assign(**assignment) == assigned
    return env, assigned["id"], image, snapshot


def _initial(frozen):
    line = next(item for item in frozen["facts"] if item["kind"] == "ORDER_LINE")
    policy = frozen["policies"][0]
    return {
        "expected_version": 1,
        "judgment": "DECIDABLE",
        "acceptable_plans": [
            {
                "action": "REFUND",
                "lines": [
                    {
                        "line_id": line["data"]["line_id"],
                        "amount_min_cents": 1000,
                        "amount_max_cents": 3000,
                    }
                ],
            },
            {"action": "REQUEST_EVIDENCE", "required_evidence": ["补一张能看清破损位置的局部照片"]},
        ],
        "required_fact_ids": [line["id"]],
        "policy_assessments": [
            {"policy_id": policy["id"], "version": policy["version"], "status": "APPLICABLE"}
        ],
    }


def test_blind_input_then_immutable_initial_explicit_reveal_and_labels(task):
    env, task_id, original, snapshot = task
    endpoint = BASE + "/" + task_id
    response = env.client.get(endpoint, headers=headers(TESTER))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    state = response.json()
    assert state["status"] == "BLIND" and state["version"] == 1
    assert state["initial"] is state["exposure"] is state["labels"] is None
    assert any(row["text"] == "原始证据文字" for row in state["input"]["messages"])
    assert not any(
        key in response.text
        for key in (
            "original_run_id",
            "graph_version",
            "model_binding_sha256",
            "reviewer_id",
            "program_check",
            "object_key",
            "legacy_agent_input",
        )
    )
    assert snapshot["original_run_id"] not in response.text
    image_url = endpoint + "/images/" + state["input"]["images"][0]["id"]
    read = env.client.get(image_url, headers=headers(TESTER))
    assert read.content == original and read.headers["cache-control"] == "no-store"
    for actor_headers in (
        headers(OWNER),
        headers(SUPPORT),
        {"Authorization": f"Bearer {OTHER_TESTER_TOKEN}"},
    ):
        assert env.client.get(endpoint, headers=actor_headers).status_code == 404
        assert env.client.get(image_url, headers=actor_headers).status_code == 404
    foreign_list = env.client.get(BASE, headers={"Authorization": f"Bearer {OTHER_TESTER_TOKEN}"})
    assert foreign_list.json() == {"items": [], "next_after": None}
    assert (
        env.client.post(
            endpoint + "/reveal", json={"expected_version": 1}, headers=headers(TESTER)
        ).status_code
        == 409
    )
    with Session(env.db) as db:
        assert db.scalar(select(BlindExposure.task_id)) is None
    judgment = _initial(state["input"])
    invalid = {**judgment, "required_fact_ids": ["ORDER_LINE:OUTSIDE"]}
    assert (
        env.client.post(endpoint + "/initial", json=invalid, headers=headers(TESTER)).status_code
        == 422
    )
    initial_key = uuid4().hex
    sealed = env.client.post(
        endpoint + "/initial", json=judgment, headers=headers(TESTER, initial_key)
    )
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["status"] == "SEALED" and sealed.json()["exposure"] is None
    assert len(sealed.json()["initial"]["payload"]["acceptable_plans"]) == 2
    assert env.client.get(endpoint, headers=headers(TESTER)).json()["exposure"] is None
    assert (
        env.client.post(
            endpoint + "/labels",
            json={
                "expected_version": 2,
                "items": [{"candidate_id": "unseen", "decision": "ACCEPTABLE"}],
            },
            headers=headers(TESTER),
        ).status_code
        == 409
    )
    reveal_key = uuid4().hex
    reveal = env.client.post(
        endpoint + "/reveal", json={"expected_version": 2}, headers=headers(TESTER, reveal_key)
    )
    assert reveal.status_code == 200, reveal.text
    candidate = reveal.json()["exposure"]["candidates"][0]
    assert candidate["options"][0]["action"] == "REQUEST_EVIDENCE"
    assert snapshot["original_run_id"] not in reveal.text
    label_body = {
        "expected_version": 3,
        "items": [{"candidate_id": candidate["id"], "decision": "ACCEPTABLE"}],
    }
    labeled = env.client.post(endpoint + "/labels", json=label_body, headers=headers(TESTER))
    assert labeled.status_code == 200, labeled.text
    assert labeled.json()["status"] == "LABELED"
    assert labeled.json()["initial"] == sealed.json()["initial"]
    assert (
        env.client.post(
            endpoint + "/initial", json=judgment, headers=headers(TESTER, initial_key)
        ).json()
        == sealed.json()
    )
    assert (
        env.client.post(
            endpoint + "/reveal", json={"expected_version": 2}, headers=headers(TESTER, reveal_key)
        ).json()
        == reveal.json()
    )
    assert (
        env.client.post(
            endpoint + "/initial",
            json={**judgment, "notes": "改写"},
            headers=headers(TESTER, initial_key),
        ).status_code
        == 409
    )
    with Session(env.db) as db:
        initial = db.get_one(BlindInitial, task_id)
        exposure = db.get_one(BlindExposure, task_id)
        labels = db.get_one(BlindLabels, task_id)
        assert initial.created_at <= exposure.created_at <= labels.created_at
        assert initial.sha256 == exposure.initial_sha256
        assert exposure.sha256 == labels.exposure_sha256


def test_profile_and_revoked_reviewer_cannot_read_frozen_images(task):
    env, task_id, _, _ = task
    endpoint = BASE + "/" + task_id
    with Session(env.db) as db, db.begin():
        db.get_one(User, TESTER.user_id).enabled = False
        assert db.get_one(BlindTask, task_id).version == 1
    assert env.client.get(endpoint, headers=headers(TESTER)).status_code == 401
    env.service.settings.profile = "app"
    assert (
        env.client.get(BASE, headers={"Authorization": f"Bearer {OTHER_TESTER_TOKEN}"}).status_code
        == 404
    )


def test_blind_view_retains_material_review_facts_and_raw_policy_conditions(configured):
    from aftercare.conversations.decisions import ConversationDecisions
    from aftercare.conversations.tables import Conversation
    from aftercare.evaluation.chat.service import _frozen_public

    env = configured
    url, case_id = _link(env)
    _offer(env, url, case_id)
    with Session(env.db) as db, db.begin():
        conversation_id = url.rsplit("/", 1)[-1]
        row = db.get_one(Conversation, conversation_id)
        scope = ConversationDecisions(env.service).resolve(
            db, SUPPORT, row.id, row.analysis_issue_id
        )
        frozen = _frozen_public(scope).model_dump(mode="json")
    reviews = [row for row in frozen["facts"] if row["kind"] == "REVIEW"]
    assert len(reviews) == 1 and reviews[0]["trust"] == "VERIFIED_RECORD"
    review = reviews[0]["data"]
    assert review["decision"] == "APPROVED" and review["effective"] is True
    assert len(review["material_ids"]) == 2 and review["created_at"]
    assert set(review) == {
        "decision",
        "effective",
        "material_ids",
        "materials_version",
        "created_at",
    }
    assert frozen["policy_coverage"] == "SCOPED_ONLY"
    assert frozen["policy_context"] is None
    assert all("selected_for" not in policy for policy in frozen["policies"])
    assert all("effective_at" in policy["conditions"] for policy in frozen["policies"])
    assert "reviewer_id" not in str(frozen) and "proposal_id" not in str(frozen)


def test_revealed_projection_preserves_unsupported_references_and_original_reasoning(task):
    from copy import deepcopy
    from types import SimpleNamespace

    from aftercare.decisions.contracts import DecisionScope
    from aftercare.evaluation.chat.service import _candidate, _frozen_public

    _, _, _, source = task
    scope = DecisionScope.model_validate(source["input"])
    public = _frozen_public(scope)
    candidate = deepcopy(source["candidate"])
    policy = public.policies[0]
    candidate["unresolved"] = ["条款适用对象需要复查"]
    candidate["options"][0].update(
        evidence_ids=[public.images[0].id, "missing-evidence"],
        rationale="候选原始补证理由",
        alternatives=["用文字补充破损位置"],
    )
    candidate["options"].append(
        {
            "id": "unverified-financial-option",
            "action": "REFUND",
            "lines": [{"line_id": scope.line_ids[0], "amount_cents": 1200}],
            "evidence_ids": ["missing-fact"],
            "policy_ids": [policy.id, "UNSUPPORTED-POLICY"],
            "rationale": "原始退款理由",
        }
    )
    snapshot = SimpleNamespace(
        id=source["id"],
        origin="AGENT",
        run_id=candidate["run_id"],
        conversation_id=scope.conversation_id,
        issue_id=scope.issue_id,
        input=source["input"],
        candidate=candidate,
    )
    projected = _candidate(snapshot, scope, public)
    assert projected["unresolved"] == candidate["unresolved"]
    evidence_option, refund_option = projected["options"]
    assert evidence_option["rationale"] == "候选原始补证理由"
    assert evidence_option["alternatives"] == ["用文字补充破损位置"]
    assert evidence_option["evidence_ids"] == [public.images[0].id, "missing-evidence"]
    assert evidence_option["evidence_references"][-1] == {
        "id": "missing-evidence",
        "status": "UNRESOLVED",
        "resolved_ids": [],
    }
    assert refund_option["policy_ids"] == [policy.id, "UNSUPPORTED-POLICY"]
    assert [row["status"] for row in refund_option["policy_references"]] == [
        "RESOLVED",
        "UNRESOLVED",
    ]
    assert refund_option["policy_references"][0]["resolved_ids"] == [
        f"{policy.id}:{policy.version}"
    ]


def test_complete_catalogue_and_selected_bundle_reference_are_distinct(task):
    from copy import deepcopy
    from types import SimpleNamespace

    from aftercare.business.models import Policy
    from aftercare.conversations.decisions import ConversationDecisions
    from aftercare.evaluation.chat.service import _candidate, _frozen_public

    env, _, _, source = task
    env.service.settings.chat_investigation_enabled = True
    with Session(env.db) as db, db.begin():
        policy = db.scalar(select(Policy))
        db.add(
            Policy(
                id="OTHER-PRODUCT-POLICY",
                version=policy.version,
                effective_at=policy.effective_at,
                data={**policy.data, "id": "OTHER-PRODUCT-POLICY", "product_ids": ["other"]},
            )
        )
        db.flush()
        scope = ConversationDecisions(env.service).resolve(
            db, SUPPORT, source["input"]["conversation_id"], source["input"]["issue_id"]
        )
    public = _frozen_public(scope)
    assert public.policy_coverage == "COMPLETE_CATALOGUE"
    assert public.policy_context["products"]
    assert public.policy_context["timestamps"]["decision_at"]
    ownership_id = f"CONDITION:{ORDER_OWNERSHIP_REF}"
    ownership = next(row for row in public.facts if row.id == ownership_id)
    assert ownership.trust == "VERIFIED_RECORD"
    assert ownership.data == {
        "value": "AUTHENTICATED_ACCOUNT_ORDER_OWNER",
        "identity_scope": "ACCOUNT_ONLY",
        "version": "account-order-ownership-v1",
        "order_id": scope.order_id,
    }
    selected = {
        f"{row['id']}:{row['version']}" for row in scope.frozen_context["policies"]["documents"]
    }
    raw = {f"{row.id}:{row.version}" for row in public.policies}
    assert selected < raw
    assert "selected_for" not in str(public.model_dump())

    candidate = deepcopy(source["candidate"])
    other_clause = min(raw - selected)
    candidate["options"][0]["evidence_ids"] = [
        "effective-policy-snapshot",
        other_clause,
        ORDER_OWNERSHIP_REF,
    ]
    snapshot = SimpleNamespace(
        id=source["id"],
        origin="AGENT",
        run_id=candidate["run_id"],
        conversation_id=scope.conversation_id,
        issue_id=scope.issue_id,
        input=scope.model_dump(mode="json"),
        candidate=candidate,
    )
    references = _candidate(snapshot, scope, public)["options"][0]["evidence_references"]
    assert [row["resolved_ids"] for row in references] == [
        sorted(selected),
        [other_clause],
        [ownership_id],
    ]
    assert references[-1]["status"] == "RESOLVED"
