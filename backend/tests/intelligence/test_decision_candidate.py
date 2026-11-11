"""P3 projection from actual persisted runs using a controlled business context."""

import json
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
