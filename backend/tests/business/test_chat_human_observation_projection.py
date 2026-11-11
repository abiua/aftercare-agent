"""Check versioned observation freeze and tool/QA projections without a DB or API."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.conversations.decisions import ConversationDecisions
from aftercare.decisions.contracts import DecisionScope
from aftercare.evaluation.chat.service import _frozen_public
from aftercare.investigation.input import InputUnavailable, build_input
from aftercare.investigation.material_observations import HUMAN_MATERIAL_PROJECTION_V1 as PROJECTION
from aftercare.investigation.tools import InvestigationTools, ToolRejected

STAMP = datetime(2026, 11, 11, tzinfo=UTC)
ITEM_SHA = "a" * 64
PARCEL_SHA = "b" * 64


def _freeze(*, projection=PROJECTION, original_issue="issue", parcel_uncertain=False):
    row = SimpleNamespace(id="chat", analysis_issue_id="issue", customer_id="customer", sequence=1)
    case = SimpleNamespace(
        id="case",
        customer_id="customer",
        line_ids=["L01"],
        facts={},
        description="商品有损坏",
        materials_version=2,
    )
    order = SimpleNamespace(id="order", customer_id="customer")
    material = [
        {"id": ident, "kind": kind, "sha256": sha, "size": 40, "content_type": "image/png"}
        for ident, kind, sha in (("item", "ITEM", ITEM_SHA), ("parcel", "PARCEL", PARCEL_SHA))
    ]
    reviews = [
        {
            "id": "previous-review",
            "materials_version": 1,
            "effective": False,
            "material_ids": ["item"],
            "decision": "NEEDS_MORE",
            "notes": "旧备注提到三件，不生成结构化记录。",
            "reviewer_id": "private-identity",
            "created_at": STAMP.isoformat(),
            "observations": [
                {"source": "HUMAN_MATERIAL_REVIEW", "material_id": "item", "finding": "UNCERTAIN"}
            ],
        },
        {
            "id": "active-review",
            "materials_version": 2,
            "effective": True,
            "material_ids": ["item", "parcel"],
            "decision": "APPROVED",
            "notes": "这段备注不进入模型或盲审。",
            "reviewer_id": "private-identity",
            "created_at": STAMP.isoformat(),
            "observations": [
                {
                    "source": "HUMAN_MATERIAL_REVIEW",
                    "material_id": ident,
                    "finding": "VISIBLE_DAMAGE",
                    "line_id": "L01",
                    "affected_quantity": 2,
                }
                for ident in ("item", "parcel")
            ],
        },
    ]
    if parcel_uncertain:
        observation = reviews[1]["observations"][1]
        observation["finding"] = "UNCERTAIN"
        observation.pop("affected_quantity")
    clause = {"id": "POL14", "version": "1.0", "text": "损坏材料审核后可提交方案。"}
    context = {
        "case": {
            "id": "case",
            "customer_id": "customer",
            "order_id": "order",
            "line_ids": ["L01"],
            "materials_version": 2,
            "intent": "damaged",
        },
        "order": {
            "id": "order",
            "version": 1,
            "status": "DELIVERED",
            "payment_status": "CONFIRMED",
            "items": [{"line_id": "L01", "product_id": "mug", "quantity": 3}],
            "products": [{"id": "mug", "category": "家居"}],
        },
        "materials": material,
        "eligibility": {
            "lines": [{"line_id": "L01", "available_cents": 6000}],
            "status": "ELIGIBLE",
        },
        "ledger": [],
        "versions": {"case": 3, "policies": "c" * 64},
        "policy_resolution": {"documents": [clause], "blockers": [], "snapshot": "c" * 64},
    }

    def business_context(*_, include_review_observations=False, **__):
        visible = deepcopy(reviews)
        if not include_review_observations:
            for review in visible:
                review.pop("observations")
        return {**context, "reviews": visible}

    entities = {
        "Message": [
            SimpleNamespace(
                id="message",
                sequence=1,
                text="请核对杯口损坏。",
                actor_id="customer",
                client_message_id="posted",
                request_hash="d" * 64,
                discarded_at=None,
            )
        ],
        "Attachment": [
            SimpleNamespace(
                id="original-item",
                message_id="message",
                sha256=ITEM_SHA,
                size=40,
                content_type="image/png",
            )
        ],
        "Observation": [],
        "ChatMaterialSource": [
            SimpleNamespace(
                material_id="item",
                attachment_id="original-item",
                case_id="case",
                conversation_id="chat",
                issue_id=original_issue,
                message_id="message",
                sha256=ITEM_SHA,
                kind="ITEM",
            )
        ],
    }

    def scalars(statement):
        return entities[statement.column_descriptions[0]["entity"].__name__]

    db = SimpleNamespace(scalars=scalars)
    business = SimpleNamespace(
        engine=None,
        clock=lambda: STAMP,
        settings=SimpleNamespace(chat_investigation_enabled=True),
        _context=business_context,
        _ledger=lambda *_: [],
        _all_policy_versions=lambda _: [clause],
        validate_draft=lambda *_: pytest.fail(
            "Evidence freeze must not validate a financial action"
        ),
    )
    return ConversationDecisions(business, evidence_projection=projection).freeze(
        db, row, case, order
    )


def test_human_review_evidence_is_opt_in_frozen_and_read_by_authorized_roles():
    legacy = _freeze(projection=None)
    new = _freeze()
    assert "evidence_projection" not in legacy.frozen_context
    assert all("observations" not in review for review in legacy.frozen_context["reviews"])
    assert legacy.input_sha256 != new.input_sha256
    assert legacy.dependency_sha256 != new.dependency_sha256
    assert new.frozen_context["evidence_projection"] == PROJECTION
    assert "observations" not in new.frozen_context["reviews"][0]
    with pytest.raises(InputUnavailable, match="INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH"):
        build_input(new)
    with pytest.raises(InputUnavailable, match="INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH"):
        build_input(legacy, evidence_projection=PROJECTION)
    old, value = build_input(legacy), build_input(new, evidence_projection=PROJECTION)
    assert old.eligibility == value.eligibility
    assert old.relevant_input_sha256 != value.relevant_input_sha256
    review = new.frozen_context["reviews"][1]
    reference = next(ref for ref in new.input_refs if ref.id == review["id"])
    assert reference.sha256 == digest(review)
    item, parcel = review["observations"]
    assert item["original_attachments"] == [
        {"attachment_id": "original-item", "message_id": "message", "sha256": ITEM_SHA}
    ]
    assert item["material_sha256"] == ITEM_SHA
    assert parcel["material_sha256"] == PARCEL_SHA and parcel["original_attachments"] == []
    assert item["affected_quantity"] == parcel["affected_quantity"] == 2

    tools = InvestigationTools(value)
    for role in ("strong_single", "fact"):
        result = tools.call(role, "read_material_evidence", {"evidence_ids": ["item"]})
        (observed,) = [
            record for record in result.data["records"] if record["ref"]["source_kind"] == "REVIEW"
        ]
        assert observed["data"]["observations"] == [item, parcel]
        assert observed["data"]["materials_version"] == 2
        assert observed["data"]["decision"] == "APPROVED"
        assert "notes" not in observed["data"] and "reviewer_id" not in observed["data"]
    with pytest.raises(ToolRejected, match="TOOL_NOT_ALLOWED"):
        tools.call("policy", "read_material_evidence", {"evidence_ids": ["item"]})

    blind = _frozen_public(new)
    assert _frozen_public(legacy).schema_version == "chat-blind-input-v1"
    assert blind.schema_version == "chat-blind-input-v2"
    facts = {row.id: row.data for row in blind.facts}
    assert facts["REVIEW:active-review"]["observations"] == [item, parcel]
    assert "observations" not in facts["REVIEW:previous-review"]
    assert "private-identity" not in blind.model_dump_json()
    assert "这段备注" not in blind.model_dump_json()


def test_uncertain_observation_does_not_fill_quantity_or_change_eligibility():
    scope = _freeze(parcel_uncertain=True)
    frozen = _frozen_public(scope)
    active = next(row for row in frozen.facts if row.id == "REVIEW:active-review")
    _, parcel = active.data["observations"]
    assert parcel["finding"] == "UNCERTAIN"
    assert "affected_quantity" not in parcel
    assert (
        build_input(scope, evidence_projection=PROJECTION).eligibility
        == build_input(_freeze(projection=None, parcel_uncertain=True)).eligibility
    )


def test_freeze_rejects_original_mapping_from_another_issue():
    with pytest.raises(BusinessError) as error:
        _freeze(original_issue="foreign")
    assert error.value.code == "DECISION_HUMAN_MATERIAL_OBSERVATIONS_INVALID"


@pytest.mark.parametrize(
    "change",
    [
        {"affected_quantity": 3},
        {"line_id": "foreign"},
        {"material_sha256": "c" * 64},
        {
            "original_attachments": [
                {"attachment_id": "fake", "message_id": "message", "sha256": ITEM_SHA}
            ]
        },
    ],
)
def test_projector_rejects_self_consistent_but_conflicting_observation_payload(change):
    payload = _freeze().model_dump(mode="json")
    review = payload["frozen_context"]["reviews"][1]
    review["observations"][0].update(change)
    next(ref for ref in payload["input_refs"] if ref["id"] == review["id"])["sha256"] = digest(
        review
    )
    payload["input_sha256"] = digest([payload["input_refs"], payload["frozen_context"]])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_HUMAN_MATERIAL_OBSERVATIONS_INVALID"):
        build_input(DecisionScope.model_validate(payload), evidence_projection=PROJECTION)


def test_projector_rejects_observation_for_other_item_in_same_order():
    payload = _freeze().model_dump(mode="json")
    frozen = payload["frozen_context"]
    frozen["order"]["items"].append({"line_id": "L02", "product_id": "mug", "quantity": 3})
    review = frozen["reviews"][1]
    review["observations"][0]["line_id"] = "L02"
    next(ref for ref in payload["input_refs"] if ref["id"] == review["id"])["sha256"] = digest(
        review
    )
    payload["input_sha256"] = digest([payload["input_refs"], frozen])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_HUMAN_MATERIAL_OBSERVATIONS_INVALID"):
        build_input(DecisionScope.model_validate(payload), evidence_projection=PROJECTION)
