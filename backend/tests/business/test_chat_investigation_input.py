"""Exercise P4 projection and tool access on actual P3 persisted decisions."""

import pytest
from test_business import SUPPORT
from test_chat_decisions_integration import _link, _offer
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.business.rules import digest
from aftercare.decisions.contracts import DecisionScope
from aftercare.investigation.input import InputUnavailable, build_input
from aftercare.investigation.routing import route_input, route_unavailable
from aftercare.investigation.tools import InvestigationTools, ToolRejected

client = _client
system = _system
configured = _configured


@pytest.fixture
def scope(configured):
    url, case_id = _link(configured)
    send(configured.client, url, "第一张图拍了裂纹，联系地址 example@example.org。")
    _offer(configured, url, case_id)
    response = configured.client.get(url + "/decisions", headers=headers(SUPPORT))
    return DecisionScope.model_validate(response.json()["snapshot"]["input"])


def test_projection_from_persisted_scope_and_role_bound_tools(scope):
    value = build_input(scope)
    view = value.role_view("strong_single")
    assert [item["sequence"] for item in view["customer_statements"]] == sorted(
        item.sequence for item in value.messages
    )
    assert any(
        "第一张图" in item["text"] and "example@" not in item["text"]
        for item in view["customer_statements"]
    )
    assert "customer_statements" not in value.role_view("policy")
    tools = InvestigationTools(value)
    receipt = tools.call("fact", "read_order_lines", {"line_ids": [scope.line_ids[0]]})
    assert receipt.data["records"][0]["data"]["paid_cents"] == 39800
    assert receipt.refs[0].scope == (scope.conversation_id, scope.issue_id, scope.case_id)
    assert receipt.input_digest == value.relevant_input_sha256
    with pytest.raises(ToolRejected, match="TOOL_NOT_ALLOWED"):
        tools.call("policy", "read_order_lines", {"line_ids": [scope.line_ids[0]]})
    with pytest.raises(ToolRejected, match="EVIDENCE_NOT_AVAILABLE"):
        tools.validate_call("fact", "read_order_lines", {"line_ids": ["OTHER-ORDER-LINE"]})
    with pytest.raises(ToolRejected, match="INVALID_TOOL_ARGUMENTS"):
        tools.validate_call("fact", "read_order_lines", {"line_ids": [5]})
    with pytest.raises(ToolRejected, match="TOOL_NOT_ALLOWED"):
        tools.call("verifier", "read_order_lines", {"line_ids": [scope.line_ids[0]]})
    request = tools.call(
        "verifier",
        "request_targeted_check",
        {
            "claim_id": "claim-1",
            "conflicting_refs": [scope.line_ids[0]],
            "target_tool": "read_order_lines",
            "reason": "核对可用金额",
        },
    )
    assert request.data["status"] == "REQUESTED"
    assert request.refs == receipt.refs
    policy = next(item for item in value.evidence if item.ref.source_kind == "POLICY")
    clause = policy.data["clause"]
    proof = tools.call(
        "policy",
        "read_policy_clause",
        {
            "policy_id": clause["id"],
            "version": clause["version"],
            "product_id": policy.data["selected_for"][0],
        },
    )
    assert proof.data["records"][0]["data"]["clause"]["text"] == clause["text"]
    ranking = tools.call(
        "policy",
        "search_applicable_policy",
        {
            "query": clause["text"][:100],
            "condition_keys": ["order_status"],
        },
    )
    assert ranking.data["retrieval"]["mode"] == "BM25"
    assert ranking.refs[0].source_kind == "CONDITION"
    assert any(ref.source_kind == "POLICY" for ref in ranking.refs)


def _updated(scope, edit):
    payload = scope.model_dump(mode="json")
    edit(payload)
    payload["input_sha256"] = digest([payload["input_refs"], payload["frozen_context"]])
    return DecisionScope.model_validate(payload)


def test_ack_does_not_change_relevant_digest_and_overflow_routes_to_human(scope, monkeypatch):
    value = build_input(scope)

    def ack(payload):
        payload["input_through_seq"] += 1
        seq = payload["input_through_seq"]
        payload["frozen_context"]["messages"].append({"id": "ack", "sequence": seq, "text": "谢谢"})
        payload["input_refs"].append(
            {"kind": "MESSAGE", "id": "ack", "sha256": digest([seq, "谢谢"])}
        )

    followup = build_input(_updated(scope, ack))
    assert value.relevant_input_sha256 == followup.relevant_input_sha256
    assert value.source_snapshot_sha256 != followup.source_snapshot_sha256
    monkeypatch.setattr("aftercare.investigation.input.MAX_TEXT_BYTES", 1)
    with pytest.raises(InputUnavailable, match="INVESTIGATION_TEXT_LIMIT") as error:
        build_input(scope)
    assert route_unavailable(error.value).output["status"] == "NEEDS_REVIEW"


def test_route_program_first_and_financial_risk_independent(scope):
    value = build_input(scope)
    eligible = value.model_copy(
        update={"eligibility": {"status": "ELIGIBLE", "available_cents": 1000000}}
    )
    route = route_input(eligible)
    assert route.path == "SINGLE"
    assert route.financial_risk["maximum_refundable_cents"] == 1000000
    line = next(row for row in value.evidence if row.ref.source_kind == "ORDER_LINE")
    extra_line = line.model_copy(update={"ref": line.ref.model_copy(update={"source_id": "L02"})})
    complex_case = eligible.model_copy(update={"evidence": (*eligible.evidence, extra_line)})
    assert route_input(complex_case).path == "MULTI"
    missing = complex_case.model_copy(
        update={
            "eligibility": {"status": "WAITING_INPUT", "missing_materials": ["DAMAGE_PHOTO"]},
        }
    )
    assert route_input(missing).output["missing_materials"] == ["DAMAGE_PHOTO"]
    refund = missing.model_copy(update={"intent": "refund_status"})
    assert route_input(refund).output["refund_lookup"]["status"] == "NO_RECORD"


def test_unverified_photo_references_cannot_be_spoofed_or_treated_as_business_review(scope):
    def evidence(payload):
        frozen = payload["frozen_context"]
        message = frozen["messages"][0]["id"]
        sha = "a" * 64
        frozen["attachments"].append(
            {
                "id": "photo",
                "message_id": message,
                "sha256": sha,
                "content_type": "image/png",
                "size": 25,
            }
        )
        frozen["observations"].append(
            {
                "id": "obs",
                "attachment_id": "photo",
                "original_sha256": sha,
                "sent_sha256": "b" * 64,
                "certainty": "UNCERTAIN",
                "findings": ["BLUR"],
            }
        )
        payload["input_refs"].extend(
            [
                {
                    "kind": "ATTACHMENT",
                    "id": "photo",
                    "sha256": digest([message, sha, 25, "image/png"]),
                },
                {
                    "kind": "OBSERVATION",
                    "id": "obs",
                    "sha256": digest(["photo", sha, "b" * 64, "UNCERTAIN", ["BLUR"]]),
                },
            ]
        )

    bound = _updated(scope, evidence)
    value = build_input(bound)
    observed = value.role_view("fact")["unverified_observations"]
    assert observed[0]["trust"] == "UNVERIFIED_OBSERVATION"
    assert observed[0]["data"]["sent_sha256"] == "b" * 64
    assert "unverified_observations" not in value.role_view("policy")
    value = value.model_copy(
        update={
            "eligibility": {"status": "WAITING_INPUT", "missing_materials": ["ITEM"]},
        }
    )
    assert route_input(value).path == "PROGRAM"
    assert route_input(value).reason == "EVIDENCE_REQUIRED"
    with_detail = [
        row.model_copy(update={"data": {**row.data, "findings": ["POSSIBLE_CRACK"]}})
        if row.ref.source_kind == "OBSERVATION"
        else row
        for row in value.evidence
    ]
    useful = route_input(value.model_copy(update={"evidence": tuple(with_detail)}))
    assert useful.path == "SINGLE"
    assert useful.output["material_review_required"] is True
    with pytest.raises(InputUnavailable, match="INVESTIGATION_OBSERVATION_SCOPE"):
        build_input(
            _updated(
                bound,
                lambda payload: payload["frozen_context"]["observations"][0].update(
                    {"original_sha256": "c" * 64}
                ),
            )
        )


def test_soft_policy_and_pending_material_review_do_not_grant_execution(scope):
    value = build_input(scope)
    pending_review = value.model_copy(
        update={
            "eligibility": {"status": "HUMAN_REVIEW", "reason_code": "MATERIAL_REVIEW_REQUIRED"}
        }
    )
    route = route_input(pending_review)
    assert route.path == "SINGLE"
    assert route.output["material_review_required"] is True
    assert route.output["business_status"] == "HUMAN_REVIEW"
    soft = pending_review.model_copy(
        update={"policy_blockers": ("CONFLICTING_POLICY_PRIORITY:POL01:P01",)}
    )
    assert route_input(soft).path == "MULTI"
    assert route_input(soft).output["policy_blockers"] == list(soft.policy_blockers)
    hard = soft.model_copy(
        update={"policy_blockers": (*soft.policy_blockers, "INVALID_POLICY_VERSION:POL02")}
    )
    assert route_input(hard).path == "PROGRAM"
    assert route_input(hard).output["status"] == "NEEDS_REVIEW"
