"""P4 mechanism over real persisted chat/business fixtures and an injected model."""

import json
from functools import wraps
from threading import Lock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, _evidence
from test_chat_decisions_integration import _link
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers
from test_conversations import system as _system

from aftercare.conversations.decisions import ConversationDecisions
from aftercare.conversations.tables import Conversation
from aftercare.intelligence.api import router
from aftercare.intelligence.decision_candidate import project_candidate
from aftercare.intelligence.models import ModelRunner, Registry
from aftercare.intelligence.service import AgentService, RunAudit
from aftercare.investigation.graph import CHAT_GRAPH_PROFILE
from aftercare.investigation.input import build_input

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


@pytest.mark.parametrize("arm", ["strong_single", "single_staged", "multi"])
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
    rechecks = []

    def transport(_spec, messages, tools, _timeout, _output):
        assert not tracing_is_enabled()
        role = messages[0].content.split("。", 1)[0].split()[-1]
        sent = json.loads(messages[1].content)
        user_view = sent["frozen_input"]
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
                            "name": "read_policy_clause",
                            "id": "policy",
                            "args": {
                                "policy_id": policy_id,
                                "version": policy_version,
                                "product_id": policy.data["selected_for"][0],
                            },
                        },
                    ]
                )
            assert policy.data["clause"]["text"] in messages[-1].content
            assert str(amount) in messages[-2].content
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
            assert "customer_statements" not in user_view
            assert bool(sent["upstream_claims"]) == (arm == "single_staged")
            if not isinstance(messages[-1], ToolMessage):
                return _call(
                    "read_policy_clause",
                    {
                        "policy_id": policy_id,
                        "version": policy_version,
                        "product_id": policy.data["selected_for"][0],
                    },
                )
            assert policy.data["clause"]["text"] in messages[-1].content
            return _reply({"claims": [_claim("clause", policy_ref, "适用")]})
        assert role == "verifier"
        rows = user_view["original_frozen_evidence"]
        assert any(
            row["ref"]["source_id"] == line_id and row["data"]["available_cents"] == amount
            for row in rows
        )
        assert any(
            row["ref"]["source_id"] == policy_ref and row["data"]["clause"]["text"] for row in rows
        )
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
    assert len(calls) == {"strong_single": 2, "single_staged": 5, "multi": 7}[arm]
    assert (
        role_tools["strong_single"]
        if arm == "strong_single"
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
    assert staff.status_code == owner.status_code == 200
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
