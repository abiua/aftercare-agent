"""Check versioned observation freeze and tool/QA projections without a DB or API."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.conversations.decisions import ConversationDecisions
from aftercare.conversations.investigation_delivery import _covered
from aftercare.decisions.contracts import DecisionScope
from aftercare.evaluation.chat.service import _frozen_public
from aftercare.investigation.input import InputUnavailable, build_input
from aftercare.investigation.material_observations import HUMAN_MATERIAL_PROJECTION_V1 as PROJECTION
from aftercare.investigation.material_observations import ORDER_PAYMENT_REF
from aftercare.investigation.protocol import HUMAN_PAYMENT_V7
from aftercare.investigation.tools import InvestigationTools, ToolRejected

STAMP = datetime(2026, 11, 11, tzinfo=UTC)
ITEM_SHA = "a" * 64
PARCEL_SHA = "b" * 64


def _freeze(
    *,
    projection=PROJECTION,
    original_issue="issue",
    parcel_uncertain=False,
    observations=True,
    protocol=None,
    paid_at=None,
):
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

    if paid_at is not None:
        context["order"]["paid_at"] = paid_at

    def business_context(*_, include_review_observations=False, **__):
        visible = deepcopy(reviews)
        if not include_review_observations or not observations:
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
    if protocol is not None:
        business.settings.chat_investigation_protocol = protocol
    options = {} if protocol is not None else {"evidence_projection": projection}
    return ConversationDecisions(business, **options).freeze(db, row, case, order)


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


def test_new_projection_without_observations_does_not_reuse_legacy_candidate():
    legacy = _freeze(projection=None, observations=False)
    current = _freeze(observations=False)
    assert all("observations" not in item for item in current.frozen_context["reviews"])
    assert {
        key: value for key, value in current.frozen_context.items() if key != "evidence_projection"
    } == legacy.frozen_context
    assert current.input_refs == legacy.input_refs
    assert (
        legacy.dependency_sha256
        == "6a996f32a9b4e3997d386786e5a7b2ede73abb63c753d09f32f150293c415b16"
    )
    assert current.dependency_sha256 != legacy.dependency_sha256

    previous = SimpleNamespace(
        expires_at=STAMP + timedelta(hours=1),
        input=legacy.model_dump(mode="json"),
        candidate={"unresolved": []},
        checks={"refund": {"status": "PASSED"}},
    )
    assert _covered(previous, legacy, STAMP)
    assert not _covered(previous, current, STAMP)


@pytest.mark.parametrize("paid_at", [None, STAMP.isoformat()])
def test_v7_freezes_order_payment_as_a_citable_raw_record(paid_at):
    scope = _freeze(protocol="compact-v7", paid_at=paid_at)
    projection = HUMAN_PAYMENT_V7.evidence_projection
    assert scope.frozen_context["evidence_projection"] == projection
    assert scope.frozen_context["reviews"][1]["observations"]
    value = build_input(scope, evidence_projection=projection)
    payment = {
        "order_id": scope.order_id,
        "order_version": 1,
        "payment_status": "CONFIRMED",
        "paid_at": paid_at,
    }
    ref = next(ref for ref in scope.input_refs if ref.id == ORDER_PAYMENT_REF)
    assert ref.kind == "FACT" and ref.sha256 == digest(payment)
    assert ORDER_PAYMENT_REF in scope.evidence_ids
    tools = InvestigationTools(value)
    for role in ("fact", "strong_single"):
        receipt = tools.call(role, "read_order_lines", {"line_ids": ["L01"]})
        row = next(
            row for row in receipt.data["records"] if row["ref"]["source_id"] == ORDER_PAYMENT_REF
        )
        assert row["data"] == payment
        assert row["ref"]["source_kind"] == "CONDITION"
        assert row["ref"]["version_or_sha256"] == ref.sha256
    with pytest.raises(ToolRejected, match="TOOL_NOT_ALLOWED"):
        tools.call("policy", "read_order_lines", {"line_ids": ["L01"]})
    fact = next(
        row for row in _frozen_public(scope).facts if row.id == f"CONDITION:{ORDER_PAYMENT_REF}"
    )
    assert fact.data == payment
    legacy = _freeze(protocol="compact-v6", paid_at=paid_at)
    assert value.eligibility == build_input(legacy).eligibility
    assert ORDER_PAYMENT_REF not in legacy.evidence_ids
    assert not any(row.ref.source_id == ORDER_PAYMENT_REF for row in build_input(legacy).evidence)


@pytest.mark.parametrize("changed_field", ["payment_status", "paid_at", "version"])
def test_v7_rejects_order_payment_tampering_even_if_input_digest_is_recomputed(changed_field):
    scope = _freeze(protocol="compact-v7").model_dump(mode="json")
    scope["frozen_context"]["order"][changed_field] = {
        "payment_status": "UNKNOWN",
        "paid_at": STAMP.isoformat(),
        "version": 2,
    }[changed_field]
    scope["input_sha256"] = digest([scope["input_refs"], scope["frozen_context"]])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_REFERENCE_MISMATCH"):
        build_input(
            DecisionScope.model_validate(scope),
            evidence_projection=HUMAN_PAYMENT_V7.evidence_projection,
        )


def test_v7_responses_wire_tools_expose_exact_scoped_argument_values():
    from langchain_core.messages import HumanMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langchain_openai.chat_models.base import _construct_responses_api_payload

    scope = _freeze(protocol="compact-v7", paid_at=STAMP.isoformat())
    value = build_input(scope, evidence_projection=HUMAN_PAYMENT_V7.evidence_projection)
    tools = InvestigationTools(value)
    legacy = {item["function"]["name"]: item["function"] for item in tools.schemas("strong_single")}
    assert "strict" not in legacy["read_order_lines"]
    assert "read_refund_execution" in legacy

    payload = _construct_responses_api_payload(
        [HumanMessage(content="controlled fixture")],
        {
            "tools": [
                convert_to_openai_tool(item)
                for item in tools.schemas("strong_single", scoped_arguments=True)
            ]
        },
    )
    wire = {item["name"]: item for item in payload["tools"]}
    assert "read_refund_execution" not in wire
    assert all(item["strict"] is True for item in wire.values())
    assert wire["read_order_lines"]["parameters"]["properties"]["line_ids"]["items"]["enum"] == [
        "L01"
    ]
    search = wire["search_applicable_policy"]["parameters"]
    assert set(search["required"]) == {"query", "condition_keys"}
    assert search["properties"]["condition_keys"]["items"]["enum"] == sorted(
        row.ref.source_id for row in value.evidence if row.ref.source_kind == "CONDITION"
    )
    clause = wire["read_policy_clause"]["parameters"]["properties"]
    assert clause["product_id"]["enum"] == ["mug"]
    assert clause["version"]["enum"] == ["1.0"]
    assert clause["policy_id"]["enum"] == ["POL14"]
    (verifier,) = tools.schemas("verifier", scoped_arguments=True)
    assert (
        "enum" not in verifier["function"]["parameters"]["properties"]["conflicting_refs"]["items"]
    )
    original = next(row for row in value.evidence if row.ref.source_kind == "POLICY")
    other = original.model_copy(
        update={
            "ref": original.ref.model_copy(update={"source_id": "POL15:2.0"}),
            "data": {
                **original.data,
                "clause": {**original.data["clause"], "id": "POL15", "version": "2.0"},
            },
        }
    )
    pair_tools = InvestigationTools(value.model_copy(update={"evidence": (*value.evidence, other)}))
    with pytest.raises(ToolRejected, match="EVIDENCE_NOT_AVAILABLE"):
        pair_tools.validate_call(
            "policy",
            "read_policy_clause",
            {
                "policy_id": "POL14",
                "version": "2.0",
                "product_id": "mug",
            },
        )

    assert tools.validate_call(
        "policy", "search_applicable_policy", {"query": "损坏", "condition_keys": []}
    )
    with pytest.raises(ToolRejected, match="EVIDENCE_NOT_AVAILABLE"):
        tools.validate_call(
            "policy",
            "read_policy_clause",
            {"policy_id": "POL14", "version": "2.0", "product_id": "mug"},
        )


@pytest.mark.parametrize(
    ("args", "code", "field"),
    [
        ({"line_ids": "private-value"}, "ARRAY_REQUIRED", "line_ids"),
        ({"line_ids": ["outside-order"]}, "EVIDENCE_NOT_AVAILABLE", "line_ids"),
        ({"line_ids": ["L01", "L01"]}, "DUPLICATE_EVIDENCE_REFERENCE", "line_ids"),
        ({"line_ids": ["L01"], "private-name": "secret"}, "UNEXPECTED_FIELD", None),
    ],
)
def test_v7_validation_diagnostic_contains_no_model_argument_values(args, code, field):
    from aftercare.intelligence.models import InvalidOutput, _validation_fields
    from aftercare.investigation.orchestration import InvestigationContext, run_phase

    scope = _freeze(protocol="compact-v7")
    value = build_input(scope, evidence_projection=HUMAN_PAYMENT_V7.evidence_projection)

    def model_call(_audit, _logical, **kwargs):
        return kwargs["validate"](
            [{"name": "read_order_lines", "id": "test-tool-call", "args": args}], ""
        )

    context = InvestigationContext(
        value,
        None,
        SimpleNamespace(call=model_call),
        SimpleNamespace(event=lambda *_: None),
        InvestigationTools(value),
        None,
        "analysis",
        protocol=HUMAN_PAYMENT_V7,
    )
    with pytest.raises(InvalidOutput) as failure:
        run_phase(context, "fact", "fact", prompt=value.role_view("fact"))
    path = "tool_calls.arguments.read_order_lines"
    assert _validation_fields(failure.value) == [
        {"code": code, "field": f"{path}.{field}" if field else path}
    ]
