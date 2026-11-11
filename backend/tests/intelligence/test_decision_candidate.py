"""P3 projection from actual persisted runs using a controlled business context."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import test_intelligence
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.decisions.contracts import DecisionScope
from aftercare.decisions.service import DecisionService
from aftercare.intelligence.decision_candidate import project_candidate
from aftercare.intelligence.models import ModelRunner
from aftercare.intelligence.tables import AgentRun, RunEvent

run_environment = test_intelligence.environment


@pytest.fixture
def system(run_environment):
    engine, business, actor, _ = run_environment
    original = business.context

    def context(case_id, user):
        facts = original(case_id, user)
        facts["case"].update(order_id="order", customer_id="owner", line_ids=["L1"])
        return facts

    business.context = context
    service = test_intelligence._service(run_environment, dispatch=lambda _: None)
    frozen = DecisionScope(
        conversation_id="chat",
        issue_id="issue",
        case_id="case-1",
        order_id="order",
        customer_id="owner",
        line_ids=("L1",),
        input_through_seq=2,
        input_sha256="a" * 64,
        input_refs=(
            {"kind": "MESSAGE", "id": "message", "sha256": "b" * 64},
            {"kind": "ATTACHMENT", "id": "photo", "sha256": "c" * 64},
            {"kind": "OBSERVATION", "id": "observation", "sha256": "d" * 64},
            {"kind": "FACT", "id": "fact", "sha256": "e" * 64},
            {"kind": "POLICY", "id": "policy", "sha256": "f" * 64},
        ),
        evidence_graph_sha256="1" * 64,
        evidence_ids=("fact", "policy", "observation"),
        policy_catalogue_sha256="2" * 64,
        dependency_sha256="3" * 64,
        business_versions={"case": 1, "policies": "p1"},
    )
    state = SimpleNamespace(
        engine=engine,
        service=service,
        actor=actor,
        frozen=frozen,
        current=frozen,
        business=business,
    )
    state.decisions = DecisionService(
        lambda *_: state.current,
        lambda *_: pytest.fail("nonexecuting suggestion entered financial validation"),
    )
    return state


def _start(system, **kwargs):
    return test_intelligence._start(
        system.service, system.actor, decision_scope=system.frozen, **kwargs
    )


def _project(system, run, *, actor=None):
    with Session(system.engine) as db, db.begin():
        return project_candidate(db, system.decisions, actor or system.actor, run["id"])


def _error(operation, code):
    with pytest.raises(BusinessError) as denied:
        operation()
    assert denied.value.code == code


def test_real_graph_missing_materials_projects_original_frozen_scope(system):
    run = _start(system)
    system.service.process(run["id"])
    ref = _project(system, run)
    assert _project(system, run) == ref
    with Session(system.engine) as db, db.begin():
        decision = system.decisions.snapshot(db, system.actor, "chat", "issue", ref.id)
        assert (
            decision["input"]["input_refs"] == system.frozen.model_dump(mode="json")["input_refs"]
        )
        assert decision["origin"] == "AGENT"
        candidate = decision["candidate"]
        assert candidate["run_id"] == run["id"] and candidate["route_version"] == "rules-only"
        (option,) = candidate["options"]
        assert option["action"] == "REQUEST_EVIDENCE"
        assert option["target_line_ids"] == ["L1"]
        assert option["missing_evidence"] == ["商品照片", "包裹外观照片"]
        assert "observation" not in option["evidence_ids"]
        assert len(list(db.scalars(select(RunEvent).where(RunEvent.kind == "decision_input")))) == 1
    events = system.service.events("case-1", run["id"], system.actor, 0)
    assert all(
        event["payload"] == {}
        for event in events
        if event["kind"] in {"decision_input", "decision_ready"}
    )


def test_old_scope_cannot_be_projected_after_dependency_change(system):
    run = _start(system)
    system.service.process(run["id"])
    system.current = system.frozen.model_copy(update={"dependency_sha256": "4" * 64})
    _error(lambda: _project(system, run), "DECISION_STALE")


def test_unrelated_later_message_does_not_replace_frozen_input(system):
    run = _start(system)
    system.service.process(run["id"])
    system.current = system.frozen.model_copy(
        update={"input_through_seq": 3, "input_sha256": "8" * 64}
    )
    assert _project(system, run).version == 1


def test_run_changed_before_graph_cannot_project(system):
    run = _start(system)
    system.business.refunded_cents = 100
    system.service.process(run["id"])
    assert (
        system.service.get("case-1", run["id"], system.actor)["reason"]
        == "DECISION_RUN_CONTEXT_CHANGED"
    )
    _error(lambda: _project(system, run), "DECISION_RUN_CONTEXT_CHANGED")


def test_start_retry_keeps_original_binding_even_after_later_business_change(system):
    run = _start(system)
    system.business.refunded_cents = 100
    assert _start(system)["id"] == run["id"]


def test_durable_result_changed_after_graph_cannot_project(system):
    run = _start(system)
    system.service.process(run["id"])
    with Session(system.engine) as db, db.begin():
        row = db.get_one(AgentRun, run["id"])
        row.output = {"missing_materials": ["ITEM"]}
    _error(lambda: _project(system, run), "DECISION_RUN_NOT_READY")


def test_ordinary_run_cannot_be_claimed_as_chat_candidate(system):
    run = test_intelligence._start(system.service, system.actor)
    system.service.process(run["id"])
    _error(lambda: _project(system, run), "DECISION_RUN_NOT_READY")


def test_eligible_legacy_graph_preserves_exact_money_and_trusted_refs(system):
    system.business.status = "ELIGIBLE"
    policy = test_intelligence.POLICY

    def transport(_spec, _messages, tools, _timeout, _output):
        if tools:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_case_facts",
                        "args": {},
                        "id": "fixture-facts",
                    }
                ],
            )
        return AIMessage(
            content=json.dumps(
                {
                    "intent": "damaged",
                    "notes": "Fixture",
                    "action": "REFUND",
                    "lines": [{"line_id": "L1", "amount_cents": 1300}],
                    "citations": [
                        {"id": policy["id"], "version": policy["version"], "quote": policy["text"]}
                    ],
                }
            )
        )

    system.service.model = ModelRunner(test_intelligence.registry(), transport=transport)
    system.decisions.validate = lambda *_: {"policy_ids": ["P1"]}
    run = _start(system, mode="single")
    system.service.process(run["id"])
    ref = _project(system, run)
    with Session(system.engine) as db, db.begin():
        candidate = system.decisions.snapshot(db, system.actor, "chat", "issue", ref.id)
    (option,) = candidate["candidate"]["options"]
    assert option["action"] == "REFUND"
    assert option["lines"][0]["amount_cents"] == 1300
    assert option["evidence_ids"] == ["fact", "policy"]
    assert candidate["checks"]["graph-option"]["status"] == "PASSED"


@pytest.mark.parametrize("amount,expected", [(1300, "PASSED"), (1400, "BLOCKED")])
def test_p4_model_uses_actual_tools_and_preserves_immutable_amount(
    system, monkeypatch, amount, expected
):
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import InMemorySaver

    from aftercare.intelligence.service import AgentService
    from aftercare.investigation.contracts import Evidence, EvidenceRef, InvestigationInput
    from aftercare.investigation.graph import CHAT_GRAPH_PROFILE

    def evidence(kind, ident, data, trust):
        return Evidence(
            ref=EvidenceRef(
                source_kind=kind,
                source_id=ident,
                scope=("chat", "issue", "case-1"),
                version_or_sha256="1",
            ),
            data=data,
            trust=trust,
        )

    source = InvestigationInput(
        conversation_id="chat",
        issue_id="issue",
        case_id="case-1",
        order_id="order",
        input_through_seq=2,
        source_snapshot_sha256="a" * 64,
        relevant_input_sha256="b" * 64,
        messages=(),
        evidence=(
            evidence("ORDER_LINE", "L1", {"available_cents": 1300}, "VERIFIED_RECORD"),
            evidence(
                "POLICY",
                "P1:1.0",
                {"clause": test_intelligence.POLICY, "selected_for": ["product"]},
                "POLICY",
            ),
        ),
        eligibility={"status": "ELIGIBLE"},
        policy_blockers=(),
        business_versions={},
        intent="damaged",
    )
    monkeypatch.setattr("aftercare.intelligence.service.build_input", lambda scope: source)
    system.frozen = system.frozen.model_copy(
        update={
            "input_refs": tuple(
                ref.model_copy(update={"id": mapping})
                if (mapping := {"fact": "L1", "policy": "P1:1.0"}.get(ref.id))
                else ref
                for ref in system.frozen.input_refs
            ),
            "evidence_ids": ("L1", "P1:1.0", "observation"),
        }
    )
    system.current = system.frozen
    system.business.status = "ELIGIBLE"
    system.decisions = DecisionService(
        lambda *_: system.current,
        lambda _db, _scope, option: system.business.validate_draft(
            "case-1",
            system.actor,
            expected_version=1,
            action=option.action,
            lines=option.business_lines(),
            policy_ids=option.policy_ids,
        ),
    )
    registry = test_intelligence.registry(max_usd="0.1")
    for spec in registry.models:
        spec.data_scopes = ["customer_conversation"]
        spec.input_usd_per_million = Decimal(1)
        spec.output_usd_per_million = Decimal(1)
    transcripts = []

    def transport(_spec, messages, _tools, *_):
        transcripts.append(messages)
        usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}
        if len(transcripts) == 1:
            return AIMessage(
                content="",
                usage_metadata=usage,
                tool_calls=[
                    {"name": "read_order_lines", "args": {"line_ids": ["L1"]}, "id": "line"},
                    {
                        "name": "read_policy_clause",
                        "args": {"policy_id": "P1", "version": "1.0", "product_id": "product"},
                        "id": "policy",
                    },
                ],
            )
        assert "1300" in messages[-2].content and "商品损坏" in messages[-1].content
        return AIMessage(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "id": "c",
                            "subject": "L1",
                            "predicate": "eligible",
                            "value": "材料已通过现有规则审查",
                            "state": "SUPPORTED",
                            "supporting_refs": ["L1", "P1:1.0"],
                        }
                    ],
                    "options": [
                        {
                            "id": "refund",
                            "action": "REFUND",
                            "lines": [{"line_id": "L1", "amount_cents": amount}],
                            "policy_ids": ["P1"],
                            "evidence_ids": ["L1", "P1:1.0"],
                            "rationale": "按剩余额度和适用条款提出。",
                        }
                    ],
                }
            ),
            usage_metadata=usage,
        )

    system.service = AgentService(
        system.engine,
        SimpleNamespace(model_enabled=False, chat_investigation_enabled=True),
        system.business,
        InMemorySaver(),
        model=ModelRunner(registry, transport=transport),
        dispatch=lambda _: None,
    )
    run = _start(system, graph_profile=CHAT_GRAPH_PROFILE)
    system.service.process(run["id"])
    ref = _project(system, run)
    with Session(system.engine) as db, db.begin():
        decision = system.decisions.snapshot(db, system.actor, "chat", "issue", ref.id)
        candidate = decision["candidate"]
        assert candidate["graph_version"] == CHAT_GRAPH_PROFILE
        option = candidate["options"][0]
        assert option["lines"][0]["amount_cents"] == amount
        assert option["evidence_ids"] == ["L1", "P1:1.0"]
        assert decision["checks"]["refund"]["status"] == expected
        assert bool(candidate["unresolved"]) == (expected == "BLOCKED")
    assert len(transcripts) == 2


def test_p4_refuses_unapproved_data_scope_before_model_transport(system, monkeypatch):
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import InMemorySaver

    from aftercare.intelligence.service import AgentService
    from aftercare.investigation.contracts import InvestigationInput
    from aftercare.investigation.graph import CHAT_GRAPH_PROFILE

    value = InvestigationInput(
        conversation_id="chat",
        issue_id="issue",
        case_id="case-1",
        order_id="order",
        input_through_seq=2,
        source_snapshot_sha256="a" * 64,
        relevant_input_sha256="b" * 64,
        messages=(),
        evidence=(),
        eligibility={"status": "ELIGIBLE"},
        policy_blockers=(),
        business_versions={},
        intent="damaged",
    )
    monkeypatch.setattr("aftercare.intelligence.service.build_input", lambda _: value)
    runner = ModelRunner(
        test_intelligence.registry(max_usd="0.10"),
        transport=lambda *_: pytest.fail("private chat reached disallowed provider"),
    )
    system.service = AgentService(
        system.engine,
        SimpleNamespace(model_enabled=False, chat_investigation_enabled=True),
        system.business,
        InMemorySaver(),
        model=runner,
        dispatch=lambda _: None,
    )
    run = _start(system, graph_profile=CHAT_GRAPH_PROFILE)
    system.service.process(run["id"])
    actual = system.service.get("case-1", run["id"], system.actor)
    assert actual["reason"] == "NO_COMPATIBLE_MODEL"
    rows = system.service.events("case-1", run["id"], system.actor, 0)
    route = next(row["payload"] for row in rows if row["kind"] == "route")
    assert set(route["excluded"].values()) == {"DATA_SCOPE_RESTRICTED"}
