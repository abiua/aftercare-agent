"""P4 mechanism over real persisted chat/business fixtures and an injected model."""

import json
from functools import wraps
from threading import Lock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, TESTER, _change, _evidence, _photo
from test_chat_decisions_integration import _link
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.business.rules import digest
from aftercare.conversations.decisions import ConversationDecisions
from aftercare.conversations.tables import Conversation, Message
from aftercare.decisions.contracts import DecisionScope
from aftercare.intelligence.api import router
from aftercare.intelligence.decision_candidate import project_candidate
from aftercare.intelligence.models import ModelRunner, Registry
from aftercare.intelligence.service import AgentService, RunAudit
from aftercare.intelligence.tables import RunEvent
from aftercare.investigation.contracts import ORDER_OWNERSHIP_REF
from aftercare.investigation.graph import CHAT_GRAPH_PROFILE
from aftercare.investigation.input import InputUnavailable, build_input
from aftercare.investigation.tools import InvestigationTools

client = _client
system = _system
configured = _configured


def _reply(value):
    usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}
    if isinstance(value, list):
        return AIMessage(content="", tool_calls=value, usage_metadata=usage)
    return AIMessage(content=json.dumps(value), usage_metadata=usage)


def _call(tool, args):
    return _reply([{"name": tool, "id": tool, "args": args}])


def _claim(ident, ref, value):
    return {
        "id": ident,
        "subject": ident,
        "predicate": "value",
        "state": "SUPPORTED",
        "supporting_refs": [ref],
        "value": value,
    }


@pytest.mark.parametrize("arm", ["strong_single", "single_staged", "multi", "kernel"])
def test_actual_frozen_business_candidate_across_checkpointed_arms(configured, arm, monkeypatch):
    # SQLite does not implement SELECT FOR UPDATE. Serialize only its audit
    # transactions here; independent LangGraph fan-out has its own barrier test.
    audit_lock = Lock()

    def serialized(method):
        @wraps(method)
        def invoke(*args, **kwargs):
            with audit_lock:
                return method(*args, **kwargs)

        return invoke

    for method in ("event", "begin_attempt", "finish_attempt"):
        monkeypatch.setattr(RunAudit, method, serialized(getattr(RunAudit, method)))

    url, case_id = _link(configured)
    original_chat = send(
        configured.client,
        url,
        "两张合成照片分别拍了商品和包裹外观。",
        photos=(
            ("synthetic-item.png", _photo("green")),
            ("synthetic-parcel.png", _photo("orange")),
        ),
    )
    assert original_chat.status_code == 201
    _evidence(configured.service, case_id)
    configured.service.settings.chat_investigation_enabled = True
    decisions = ConversationDecisions(configured.service)
    with Session(configured.db) as db, db.begin():
        conversation_id = url.split("/")[-1]
        row = db.get_one(Conversation, conversation_id)
        scope = decisions.resolve(db, SUPPORT, row.id, row.analysis_issue_id)
    value = build_input(scope)
    context = configured.service.context(case_id, SUPPORT)
    allowed = context["eligibility"]["policy_ids_by_action"]["REFUND"]
    policy = next(
        item
        for item in value.evidence
        if item.ref.source_kind == "POLICY" and item.data["clause"]["id"] in allowed
    )
    policy_id, policy_version = policy.data["clause"]["id"], policy.data["clause"]["version"]
    line_id, policy_ref = scope.line_ids[0], policy.ref.source_id
    amount = context["eligibility"]["lines"][0]["available_cents"]
    calls = []
    role_tools = {}
    initial_statement_refs = set()
    rechecks = []
    reviews = []

    def transport(_spec, messages, tools, _timeout, _output):
        assert not tracing_is_enabled()
        if "独立审查者" in messages[0].content:
            calls.append("reviewer")
            reviews.append(json.loads(messages[1].content))
            assert not tools
            return _reply({"verdict": "allow"})
        role = messages[0].content.split("。", 1)[0].split()[-1]
        sent = json.loads(messages[1].content)
        user_view = sent["frozen_input"]
        if role == "strong_single":
            initial_statement_refs.update(
                row["ref"][1]
                for key in ("customer_statements", "attachments", "unverified_observations")
                for row in user_view.get(key, ())
            )
        calls.append(role)
        role_tools.setdefault(role, set()).update(tool["function"]["name"] for tool in tools)
        proposed = {
            "id": "refund",
            "action": "REFUND",
            "policy_ids": [policy_id],
            "lines": [{"line_id": line_id, "amount_cents": amount}],
            "evidence_ids": [line_id, policy_ref],
            "rationale": "按适用条款及实际可退余额提案。",
        }
        if role == "strong_single":
            if not isinstance(messages[-1], ToolMessage):
                return _reply(
                    [
                        {"name": "read_order_lines", "id": "line", "args": {"line_ids": [line_id]}},
                        {
                            "name": "read_material_evidence",
                            "id": "material",
                            "args": {
                                "evidence_ids": [
                                    row.ref.source_id
                                    for row in value.evidence
                                    if row.ref.source_kind == "MATERIAL"
                                ]
                            },
                        },
                        {
                            "name": "search_applicable_policy",
                            "id": "policy",
                            "args": {
                                "query": policy.data["clause"]["text"][:100],
                                "condition_keys": [
                                    "order_status",
                                    "payment_status",
                                    "paid_at",
                                    "shipped_at",
                                    "delivered_at",
                                    "cancelled_at",
                                ],
                            },
                        },
                    ]
                )
            by_name = {
                msg.name: json.loads(msg.content)["data"]["records"]
                for msg in messages
                if isinstance(msg, ToolMessage)
            }
            line, ownership = by_name["read_order_lines"]
            assert line["ref"][1] == line_id
            assert ownership["ref"][1] == ORDER_OWNERSHIP_REF
            assert ownership["data"]["identity_scope"] == "ACCOUNT_ONLY"
            materials = by_name["read_material_evidence"]
            assert len(materials) == 3
            assert materials[-1]["data"]["decision"] == "APPROVED"
            assert len(by_name["search_applicable_policy"]) == 11
            assert any(
                row["data"].get("clause", {}).get("text") == policy.data["clause"]["text"]
                for row in by_name["search_applicable_policy"]
            )
            assert any("两张合成照片" in row["text"] for row in user_view["customer_statements"])
            return _reply(
                {
                    "claims": [
                        _claim("amount", line_id, str(amount)),
                        _claim("clause", policy_ref, "适用"),
                    ],
                    "options": [proposed],
                }
            )
        if role == "fact":
            if "check_receipt" in user_view:
                rechecks.append(user_view["specific_claims"])
                assert (
                    user_view["check_receipt"]["data"]["records"][0]["data"]["available_cents"]
                    == amount
                )
                return _reply({"claims": [_claim("amount", line_id, str(amount))]})
            if not isinstance(messages[-1], ToolMessage):
                assert user_view["customer_statements"]
                return _call("read_order_lines", {"line_ids": [line_id]})
            return _reply(
                {
                    "claims": [
                        _claim("amount", line_id, str(amount - 1 if arm == "multi" else amount))
                    ]
                }
            )
        if role == "policy":
            assert user_view["customer_statements"]
            assert "eligibility" not in user_view
            assert bool(sent["upstream_claims"]) == (arm == "single_staged")
            if not isinstance(messages[-1], ToolMessage):
                return _call(
                    "read_policy_clause",
                    {
                        "policy_id": policy_id,
                        "version": policy_version,
                        "product_id": value.policy_context["products"][0]["id"],
                    },
                )
            assert policy.data["clause"]["text"] in messages[-1].content
            return _reply({"claims": [_claim("clause", policy_ref, "适用")]})
        assert role == "verifier"
        rows = user_view["original_frozen_evidence"]
        assert any(
            row["ref"][1] == line_id and row["data"]["available_cents"] == amount for row in rows
        )
        assert any(row["ref"][1] == policy_ref and row["data"]["clause"]["text"] for row in rows)
        fact = next(item for item in sent["upstream_claims"] if item["id"] == "fact/amount")
        mismatch = fact["value"] != str(amount)
        assessments = [
            {
                "claim_id": "fact/amount",
                "contradicted_field": "value",
                "verdict": "CONTRADICTED" if mismatch else "CONFIRMED",
                "evidence_refs": [line_id],
            },
            {
                "claim_id": "policy/clause",
                "contradicted_field": "value",
                "verdict": "CONFIRMED",
                "evidence_refs": [policy_ref],
            },
        ]
        if mismatch:
            return _reply(
                {
                    "assessments": assessments,
                    "recheck": {
                        "target_role": "fact",
                        "claim_ids": ["fact/amount"],
                        "tool_name": "read_order_lines",
                        "arguments": {"line_ids": [line_id]},
                        "reason": "订单剩余额度不一致。",
                    },
                }
            )
        return _reply({"assessments": assessments, "options": [proposed]})

    registry = Registry.model_validate(
        {
            "version": "chat-controlled",
            "allow_unverified": True,
            "max_usd": "0.10",
            "models": [
                {
                    "alias": "economy",
                    "provider": "fixture",
                    "model": "controlled",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "input_usd_per_million": "1",
                    "price_date": "2026-11-11",
                    "source": "controlled fixture",
                    "output_usd_per_million": "1",
                    "context_tokens": 32000,
                    "data_scopes": ["customer_conversation"],
                    "output_tokens": 1800,
                }
            ],
        }
    )
    agent = AgentService(
        configured.db,
        configured.service.settings,
        configured.service,
        InMemorySaver(),
        model=ModelRunner(registry, transport=transport),
        dispatch=lambda _: None,
    )
    run = agent.start(
        case_id,
        SUPPORT,
        expected_version=scope.business_versions["case"],
        idempotency_key=f"controlled-{arm}",
        mode="single",
        strategy="economy",
        message=scope.frozen_context["legacy_agent_input"]["message"],
        decision_scope=scope,
        graph_profile=CHAT_GRAPH_PROFILE,
        investigation_arm=arm,
    )
    with tracing_context(enabled=True):
        agent.process(run["id"])
    completed = agent.get(case_id, run["id"], SUPPORT)
    assert completed["status"] == "COMPLETED", (completed["reason"], calls)
    with Session(configured.db) as db, db.begin():
        ref = project_candidate(db, decisions.records, SUPPORT, run["id"])
        snapshot = decisions.records.snapshot(
            db, SUPPORT, scope.conversation_id, scope.issue_id, ref.id
        )
    assert snapshot["checks"]["refund"]["status"] == "PASSED"
    assert snapshot["candidate"]["options"][0]["lines"][0]["amount_cents"] == amount
    assert snapshot["candidate"]["options"][0]["evidence_ids"] == [line_id, policy_ref]
    assert len(rechecks) == (1 if arm == "multi" else 0)
    assert len(calls) == {"strong_single": 2, "single_staged": 5, "multi": 7, "kernel": 3}[arm]
    if arm == "kernel":
        # Executor, then one independent review of the checked refund proposal, then publish.
        assert calls == ["strong_single", "strong_single", "reviewer"]
        assert snapshot["candidate"]["route_version"] == "kernel"
        assert completed["output"]["kernel"] == {
            "version": "aftercare-kernel-v2",
            "review": completed["output"]["kernel"]["review"],
            "revised": False,
        }
        assert completed["output"]["kernel"]["review"]["verdict"] == "allow"
        (review,) = reviews
        assert review["proposal"]["calls"][0]["name"] == "propose_refund"
        assert review["guard_confirmed_facts"]["business_rule_checks"] == "passed"
        assert "executor_context" not in review and "claims" not in json.dumps(review)
    assert (
        role_tools["strong_single"]
        if arm in {"strong_single", "kernel"}
        else role_tools["fact"] | role_tools["policy"]
    ) == {
        "read_order_lines",
        "read_material_evidence",
        "read_refund_execution",
        "search_applicable_policy",
        "read_policy_clause",
    }
    configured.client.app.state.agent = agent
    configured.client.app.include_router(router, prefix="/api")
    event_path = f"/api/cases/{case_id}/runs/{run['id']}/events"
    staff = configured.client.get(event_path, headers=headers(SUPPORT))
    owner = configured.client.get(event_path, headers=headers(OWNER))
    tester = configured.client.get(event_path, headers=headers(TESTER))
    assert staff.status_code == owner.status_code == tester.status_code == 200
    with Session(configured.db) as db:
        stored_receipts = db.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run["id"], RunEvent.kind == "investigation_receipt"
            )
        ).all()
        assert stored_receipts
        assert all(
            row.payload["receipt_sha256"] == digest(row.payload["receipt"])
            for row in stored_receipts
        )
    for response in (staff, owner, tester):
        redacted = [row for row in response.json() if row["kind"] == "investigation_receipt"]
        assert len(redacted) == len(stored_receipts)
        assert all(row["payload"] == {} for row in redacted)
    default = configured.client.get(event_path.removesuffix("?after=0"))
    assert default.status_code == 401
    visible_run = configured.client.get(event_path.removesuffix("/events"), headers=headers(OWNER))
    assert visible_run.status_code == 200
    assert not {"action", "lines", "policy_ids", "investigation_options"}.intersection(
        visible_run.json()["output"]
    )
    assert any(
        row["payload"].get("claims")
        for row in staff.json()
        if row["kind"] == "investigation_claims"
    )
    assert all(
        not row["payload"] for row in owner.json() if row["kind"].startswith("investigation_")
    )
    assert all(not row["payload"] for row in staff.json() if row["kind"] == "decision_input")
    budgets = [
        item["payload"]["budget"]
        for item in agent.events(case_id, run["id"], SUPPORT, 0)
        if item["kind"] == "route"
    ]
    assert budgets and {budget["run_attempts_limit"] for budget in budgets} == {8}
    assert {budget["run_cost_limit_usd"] for budget in budgets} == {"0.10"}
    if arm == "strong_single":
        rounds = [
            row["payload"] for row in staff.json() if row["kind"] == "investigation_prompt_budget"
        ]
        assert len(rounds) == 2
        assert rounds[1]["preflight"] == "FIT"
        assert rounds[1]["estimated_with_tools"] <= rounds[1]["limit"]
        assert rounds[1]["allowed_tool_batch"] > 0
        assert rounds[1]["evidence_count"] == len(
            initial_statement_refs
            | {
                ref["source_id"]
                for item in stored_receipts
                for ref in item.payload["receipt"]["refs"]
            }
        )
        assert rounds[1]["estimated_with_tools"] < rounds[1]["limit"] == 24_576


def test_full_policy_history_is_blindly_searchable_but_never_supports_current_action(configured):
    from datetime import UTC, datetime

    from aftercare.business.errors import BusinessError
    from aftercare.business.models import Policy
    from aftercare.decisions.contracts import Option
    from aftercare.investigation.catalogue import CATALOGUE_VERSION
    from aftercare.investigation.routing import route_input
    from aftercare.investigation.tools import InvestigationTools, ToolRejected

    url, case_id = _link(configured)
    _evidence(configured.service, case_id)
    configured.service.settings.chat_investigation_enabled = True
    decisions = ConversationDecisions(configured.service)
    expired = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2090, 1, 1, tzinfo=UTC)
    with Session(configured.db) as db, db.begin():
        original = db.get_one(Policy, ("POL01", "1.0")).data
        old = {
            **original,
            "version": "history",
            "text": "旧版特别条款 HISTORYUNIQUE",
            "effective_at": expired.isoformat(),
            "expires_at": datetime(2021, 1, 1, tzinfo=UTC).isoformat(),
            "product_ids": ["DIFFERENT-PRODUCT"],
        }
        later = {
            **original,
            "version": "future",
            "text": "未来特别条款 FUTUREUNIQUE",
            "effective_at": future.isoformat(),
        }
        db.add_all(
            [
                Policy(id="POL01", version="history", effective_at=expired, data=old),
                Policy(id="POL01", version="future", effective_at=future, data=later),
            ]
        )
    with Session(configured.db) as db, db.begin():
        row = db.get_one(Conversation, url.split("/")[-1])
        scope = decisions.resolve(db, SUPPORT, row.id, row.analysis_issue_id)
    value = build_input(scope)
    assert value.policy_catalogue_version == CATALOGUE_VERSION
    assert route_input(value).path == "SINGLE"
    role = value.role_view("policy")
    assert (
        "policy_context" in role
        and "eligibility" not in role
        and "selected_policy_keys" not in role
    )
    assert "selected_for" not in str(role)
    assert role["policy_context"]["timestamps"]["decision_at"]
    assert {"POL01:1.0", "POL01:history", "POL01:future"} <= {
        ref["source_id"] for ref in role["available_evidence"]
    }
    tools = InvestigationTools(value)
    product = role["policy_context"]["products"][0]["id"]
    historical = tools.call(
        "policy",
        "read_policy_clause",
        {
            "policy_id": "POL01",
            "version": "history",
            "product_id": product,
        },
    )
    assert historical.data["records"][0]["data"]["clause"] == old
    assert historical.data["applicability"] == "NOT_ASSESSED"
    with pytest.raises(ToolRejected, match="EVIDENCE_NOT_AVAILABLE"):
        tools.call(
            "policy",
            "read_policy_clause",
            {"policy_id": "POL01", "version": "history", "product_id": "DIFFERENT-PRODUCT"},
        )
    search = tools.call(
        "policy", "search_applicable_policy", {"query": "HISTORYUNIQUE", "condition_keys": []}
    )
    assert search.data["retrieval"]["candidate_scope"] == "FROZEN_RAW_CATALOGUE"
    assert search.data["retrieval"]["applicability"] == "NOT_ASSESSED"
    assert search.refs[0].source_id == "POL01:history"

    line = scope.line_ids[0]
    option = Option(
        id="invalid-historical",
        action="REFUND",
        lines=[{"line_id": line, "amount_cents": 1}],
        evidence_ids=[line, "POL01:history"],
        policy_ids=["POL01"],
        rationale="测试旧版候选不通过。",
    )
    with Session(configured.db) as db, db.begin(), pytest.raises(BusinessError) as denied:
        decisions.validate(db, scope, option)
    assert denied.value.code == "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE"

    mixed = option.model_copy(update={"evidence_ids": (line, "POL01:history", "POL01:1.0")})
    with Session(configured.db) as db, db.begin(), pytest.raises(BusinessError) as denied:
        decisions.validate(db, scope, mixed)
    assert denied.value.code == "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE"

    current = option.model_copy(update={"evidence_ids": (line, "POL01:1.0")})
    configured.service.clock = lambda: datetime(2091, 1, 1, tzinfo=UTC)
    with Session(configured.db) as db, db.begin(), pytest.raises(BusinessError) as denied:
        decisions.validate(db, scope, current)
    assert denied.value.code == "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE"


def test_account_ownership_and_related_review_are_frozen_source_evidence(configured):
    url, case_id = _link(configured)
    _evidence(configured.service, case_id)
    configured.service.settings.chat_investigation_enabled = True
    decisions = ConversationDecisions(configured.service)

    def freeze():
        with Session(configured.db) as db, db.begin():
            conversation = db.get_one(Conversation, url.split("/")[-1])
            return decisions.resolve(db, SUPPORT, conversation.id, conversation.analysis_issue_id)

    scope = freeze()
    value = build_input(scope)
    tools = InvestigationTools(value)
    order_args = {"line_ids": list(scope.line_ids)}
    receipt = tools.call("fact", "read_order_lines", order_args)
    assert [row.source_id for row in receipt.refs] == [*scope.line_ids, ORDER_OWNERSHIP_REF]
    ownership = receipt.data["records"][-1]["data"]
    assert ownership == {
        "version": "account-order-ownership-v1",
        "order_id": scope.order_id,
        "value": "AUTHENTICATED_ACCOUNT_ORDER_OWNER",
        "identity_scope": "ACCOUNT_ONLY",
    }
    assert scope.customer_id not in ownership.values()
    policy_receipt = tools.call(
        "policy",
        "search_applicable_policy",
        {"query": "本人订单", "condition_keys": [ORDER_OWNERSHIP_REF]},
    )
    assert policy_receipt.refs[0].source_id == ORDER_OWNERSHIP_REF
    assert policy_receipt.data["records"][0]["data"] == ownership

    material = next(row for row in value.evidence if row.ref.source_kind == "MATERIAL")
    review = next(row for row in value.evidence if row.ref.source_kind == "REVIEW")
    for refs in ([material.ref.source_id], [material.ref.source_id, review.ref.source_id]):
        related = tools.call("fact", "read_material_evidence", {"evidence_ids": refs})
        assert [row.source_id for row in related.refs] == [
            material.ref.source_id,
            review.ref.source_id,
        ]
        assert related.data["records"][-1]["data"]["decision"] == "APPROVED"

    tampered = scope.model_dump(mode="json")
    raw = tampered["frozen_context"]
    raw["authenticated_order_ownership"]["identity_scope"] = "KYC"
    tampered["input_sha256"] = digest([tampered["input_refs"], raw])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_OWNERSHIP_BINDING_INVALID"):
        build_input(DecisionScope.model_validate(tampered))
    raw["authenticated_order_ownership"]["identity_scope"] = "ACCOUNT_ONLY"
    binding = next(row for row in tampered["input_refs"] if row["id"] == ORDER_OWNERSHIP_REF)
    binding["sha256"] = "0" * 64
    tampered["input_sha256"] = digest([tampered["input_refs"], raw])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_REFERENCE_MISMATCH"):
        build_input(DecisionScope.model_validate(tampered))

    # A legacy input without the ownership field remains readable. New freezes of
    # messages lacking authenticated publication provenance do not acquire it.
    legacy = scope.model_dump(mode="json")
    legacy["frozen_context"].pop("authenticated_order_ownership")
    legacy["input_refs"] = [row for row in legacy["input_refs"] if row["id"] != ORDER_OWNERSHIP_REF]
    legacy["input_sha256"] = digest([legacy["input_refs"], legacy["frozen_context"]])
    with pytest.raises(InputUnavailable, match="INVESTIGATION_OWNERSHIP_BINDING_INVALID"):
        build_input(DecisionScope.model_validate(legacy))
    legacy["evidence_ids"].remove(ORDER_OWNERSHIP_REF)
    legacy_value = build_input(DecisionScope.model_validate(legacy))
    assert ORDER_OWNERSHIP_REF not in {
        row.source_id
        for row in InvestigationTools(legacy_value)
        .call("fact", "read_order_lines", order_args)
        .refs
    }
    with Session(configured.db) as db, db.begin():
        first_message = scope.frozen_context["messages"][0]["id"]
        db.get_one(Message, first_message).actor_id = None
    changed = freeze()
    assert changed.dependency_sha256 != scope.dependency_sha256
    assert "authenticated_order_ownership" not in changed.frozen_context

    _change(
        configured.service,
        case_id,
        OWNER,
        configured.service.upload,
        "new-photo",
        kind="ITEM",
        content=_photo("blue"),
    )
    current = freeze()
    related = InvestigationTools(build_input(current)).call(
        "fact", "read_material_evidence", {"evidence_ids": [material.ref.source_id]}
    )
    assert [row.source_id for row in related.refs] == [material.ref.source_id]
