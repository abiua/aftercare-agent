"""Ordinary Agent recovery with real SQLite-backed business facts and a model double."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.models import Policy
from aftercare.business.payment import PaymentResult
from aftercare.business.service import BusinessService, initialize
from aftercare.db import Base, User
from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry
from aftercare.intelligence.service import AgentService, QueueFenceLost
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

ROOT = Path(__file__).resolve().parents[3]
STAMP = datetime(2026, 11, 10, 10, tzinfo=UTC)
SUPPORT = UserContext(user_id="support", role="support")
APPROVER = UserContext(user_id="approver", role="approver")
ORDER_ID = "RECOVERY-ORDER"
PAID = 39800


class NoMaterials:
    def put(self, *_):
        raise AssertionError("Recovery fixture has no material uploads")

    def get(self, *_):
        raise AssertionError("Recovery fixture has no material reads")

    def presign(self, *_):
        raise AssertionError("Recovery fixture has no signed material links")


class UnknownThenFailedPayment:
    def submit(self, *_):
        raise TimeoutError

    def lookup(self, key, payload_hash, amount_cents):
        return PaymentResult(
            "FAILED", {"id": f"fixture-{key}", "mode": "SIMULATED", "amount_cents": amount_cents}
        )


@pytest.fixture
def recovery_system(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.sqlite'}")
    Base.metadata.create_all(engine)
    seed = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    seed["shipments"] = []
    seed["orders"] = [
        {
            "id": ORDER_ID,
            "customer_id": "C001",
            "status": "DELIVERED",
            "payment_status": "CONFIRMED",
            "created_at": (STAMP - timedelta(days=4)).isoformat(),
            "paid_at": (STAMP - timedelta(days=3)).isoformat(),
            "delivered_at": (STAMP - timedelta(days=1)).isoformat(),
            "subtotal_cents": PAID,
            "discount_cents": 0,
            "payable_cents": PAID,
            "paid_cents": PAID,
            "refunded_cents": 0,
            "items": [
                {
                    "line_id": "L01",
                    "product_id": "P001",
                    "quantity": 1,
                    "unit_price_cents": PAID,
                    "discount_cents": 0,
                    "payable_cents": PAID,
                    "paid_cents": PAID,
                    "refunded_cents": 0,
                }
            ],
        }
    ]
    initialize(engine, seed)
    with Session(engine) as db, db.begin():
        for actor in (SUPPORT, APPROVER):
            db.add(
                User(
                    id=actor.user_id,
                    role=actor.role,
                    enabled=True,
                    token_hash=actor.user_id,
                    customer_id=None,
                    display_name=actor.user_id,
                )
            )
    settings = SimpleNamespace(profile="test", model_enabled=False)
    business = BusinessService(engine, settings, storage=NoMaterials(), clock=lambda: STAMP)
    try:
        yield SimpleNamespace(engine=engine, settings=settings, business=business)
    finally:
        engine.dispose()


def create_case(business, intent):
    return business.create_case(
        SUPPORT,
        order_id=ORDER_ID,
        intent=intent,
        description="隔离恢复验收。请核对当前订单的处理进度。",
        expected_version=business.order(ORDER_ID, SUPPORT)["version"],
        idempotency_key=str(uuid4()),
        line_ids=["L01"],
    )["id"]


def mutate(business, case_id, actor, method, **fields):
    return method(
        case_id,
        actor,
        expected_version=business.context(case_id, actor)["case"]["version"],
        idempotency_key=str(uuid4()),
        **fields,
    )


def approved_sibling(business):
    case_id = create_case(business, "return")
    mutate(
        business,
        case_id,
        SUPPORT,
        business.record_facts,
        facts={"return_receipt_status": "RECEIVED", "inspection_status": "PASSED"},
        line_ids=["L01"],
        notes="已核验退件。",
    )
    plan = mutate(
        business,
        case_id,
        SUPPORT,
        business.propose,
        action="REFUND",
        lines=[{"line_id": "L01", "amount_cents": PAID}],
        notes="隔离模拟退款。",
        policy_ids=[],
    )
    mutate(
        business,
        case_id,
        APPROVER,
        business.approve,
        proposal_id=plan["id"],
        decision="APPROVED",
        notes="批准模拟处置。",
    )
    return case_id, plan["id"]


def execute_sibling(business, sibling):
    case_id, proposal_id = sibling
    return mutate(business, case_id, SUPPORT, business.execute, proposal_id=proposal_id)


def reply(*, quote=None):
    result = {"intent": "refund_status", "notes": "仅查询已记录的退款处理。"}
    if quote is not None:
        result["citations"] = [{"id": "POL12", "version": "1.0", "quote": quote}]
    return AIMessage(
        content=json.dumps(result, ensure_ascii=False),
        usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
    )


def worker(system, transport, owned):
    registry = Registry(
        version="offline-recovery-test",
        allow_unverified=True,
        models=[
            ModelSpec(
                alias="economy",
                provider="fixture",
                model="local-only",
                endpoint="http://127.0.0.1:9",
                api_key_env="UNUSED_RECOVERY_TEST_KEY",
            )
        ],
    )
    return AgentService(
        system.engine,
        system.settings,
        system.business,
        InMemorySaver(),
        dispatch=lambda _: None,
        model=ModelRunner(registry, transport=transport) if transport is not None else None,
        queue_admission=lambda _: owned[0],
    )


def start(agent, business, case_id):
    return agent.start(
        case_id,
        SUPPORT,
        expected_version=business.context(case_id, SUPPORT)["case"]["version"],
        idempotency_key=str(uuid4()),
        mode="single",
        strategy="economy",
        message="请查询当前订单的退款执行记录。",
    )


def attempts(engine, run_id):
    with Session(engine) as db:
        return [
            (row.logical_id, row.ordinal, row.status)
            for row in db.scalars(
                select(ModelAttempt)
                .where(ModelAttempt.run_id == run_id)
                .order_by(ModelAttempt.logical_id, ModelAttempt.ordinal)
            )
        ]


def bindings(agent, case_id, run_id):
    return [
        row["payload"]
        for row in agent.events(case_id, run_id, SUPPORT, 0)
        if row["kind"] == "context_binding"
    ]


def lose_before_publication(agent, owned, monkeypatch):
    original_finish = agent._finish

    def fenced_finish(*args):
        owned[0] = False
        return original_finish(*args)

    monkeypatch.setattr(agent, "_finish", fenced_finish)
    return original_finish


def assert_refused(agent, case_id, run_id, reason):
    result = agent.get(case_id, run_id, SUPPORT)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"] == reason
    assert result["output"] is None


def test_terminal_checkpoint_cannot_publish_old_ledger_after_sibling_refund(
    recovery_system, monkeypatch
):
    system = recovery_system
    case_id = create_case(system.business, "refund_status")
    sibling = approved_sibling(system.business)
    owned, calls = [True], []

    def transport(*_):
        calls.append(1)
        return reply()

    agent = worker(system, transport, owned)
    original_finish = lose_before_publication(agent, owned, monkeypatch)
    run = start(agent, system.business, case_id)
    old = system.business.context(case_id, SUPPORT)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"])
    checkpoint = agent.graphs["single"].get_state({"configurable": {"thread_id": run["thread_id"]}})
    assert checkpoint.next == () and checkpoint.values["status"] == "COMPLETED"
    assert checkpoint.values["output"]["ledger"] == []
    assert checkpoint.values["output"]["historical_refunded_cents"] == 0
    unpublished = agent.get(case_id, run["id"], SUPPORT)
    assert unpublished["status"] == "RUNNING" and unpublished["output"] is None

    assert execute_sibling(system.business, sibling)["status"] == "SUCCEEDED"
    fresh = system.business.context(case_id, SUPPORT)
    assert fresh["versions"]["case"] == old["versions"]["case"]
    assert fresh["versions"]["order"] == old["versions"]["order"] + 1
    assert [entry["status"] for entry in fresh["ledger"]] == ["SUCCEEDED"]
    assert fresh["order"]["refunded_cents"] == PAID
    owned[0] = True
    monkeypatch.setattr(agent, "_finish", original_finish)
    agent.process(run["id"], expected_cycle=0)
    assert_refused(agent, case_id, run["id"], "RECOVERY_CONTEXT_CHANGED")
    assert calls == [1]
    assert attempts(system.engine, run["id"]) == [("single:0:0", 1, "SUCCEEDED")]


@pytest.mark.parametrize(
    "change", ["sibling_refund", "ledger_status", "policy_body", "policy_version"]
)
def test_recovery_refuses_saved_attempt_when_its_context_changes(recovery_system, change):
    system = recovery_system
    business = system.business
    case_id = create_case(business, "refund_status")
    sibling = approved_sibling(business) if change in {"sibling_refund", "ledger_status"} else None
    unknown = None
    if change == "ledger_status":
        business.payments = UnknownThenFailedPayment()
        unknown = execute_sibling(business, sibling)
        assert unknown["status"] == "UNKNOWN"
    before = business.context(case_id, SUPPORT)
    old_policy = next(row for row in before["policies"] if row["id"] == "POL12")
    owned, calls = [True], []

    def transport(_spec, messages, *_):
        calls.append(1)
        if len(calls) == 1:
            return AIMessage(
                content="", tool_calls=[{"name": "read_case_facts", "args": {}, "id": "facts"}]
            )
        assert len(calls) == 2
        facts = json.loads(messages[-1].content)["facts"]
        assert [row["status"] for row in facts["ledger"]] == (
            ["UNKNOWN"] if change == "ledger_status" else []
        )
        owned[0] = False
        return reply(quote=old_policy["text"])

    agent = worker(system, transport, owned)
    run = start(agent, business, case_id)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"])
    assert attempts(system.engine, run["id"]) == [
        ("single:0:0", 1, "SUCCEEDED"),
        ("single:0:1", 1, "SUCCEEDED"),
    ]
    if change == "sibling_refund":
        assert execute_sibling(business, sibling)["status"] == "SUCCEEDED"
    elif change == "ledger_status":
        sibling_case_id, _ = sibling
        result = business.reconcile(
            sibling_case_id,
            unknown["id"],
            SUPPORT,
            expected_version=business.context(sibling_case_id, SUPPORT)["case"]["version"],
            idempotency_key=str(uuid4()),
        )
        assert result["status"] == "FAILED"
    else:
        modified = {**old_policy, "text": "变更后的模拟退款条款，需使用当前版本。"}
        with Session(system.engine) as db, db.begin():
            if change == "policy_body":
                db.get_one(Policy, ("POL12", "1.0")).data = modified
            else:
                modified.update(
                    version="2.0", effective_at=(STAMP - timedelta(hours=1)).isoformat()
                )
                db.add(
                    Policy(
                        id="POL12",
                        version="2.0",
                        effective_at=datetime.fromisoformat(modified["effective_at"]),
                        data=modified,
                    )
                )
    after = business.context(case_id, SUPPORT)
    assert after["versions"]["case"] == before["versions"]["case"]
    if change == "ledger_status":
        assert after["versions"] == before["versions"]
        assert [(row["id"], row["status"]) for row in after["ledger"]] == [
            (unknown["id"], "FAILED")
        ]
    elif change.startswith("policy_"):
        active_policy = next(row for row in after["policies"] if row["id"] == "POL12")
        assert active_policy == modified
    owned[0] = True
    agent.process(run["id"], expected_cycle=0)
    assert_refused(agent, case_id, run["id"], "RECOVERY_CONTEXT_CHANGED")
    assert calls == [1, 1]
    assert len(attempts(system.engine, run["id"])) == 2


def test_unchanged_context_replays_same_attempt_once(recovery_system):
    system = recovery_system
    case_id = create_case(system.business, "refund_status")
    owned, calls = [True], []

    def transport(*_):
        calls.append(1)
        owned[0] = False
        return reply()

    agent = worker(system, transport, owned)
    run = start(agent, system.business, case_id)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"])
    (old_binding,) = bindings(agent, case_id, run["id"])
    assert old_binding["cycle"] == 0 and old_binding["version"] == "agent-context-v1"
    assert len(old_binding["digest"]) == 64
    owned[0] = True
    agent.process(run["id"], expected_cycle=0)
    result = agent.get(case_id, run["id"], SUPPORT)
    assert result["status"] == "COMPLETED" and result["reason"] == "STATUS_ONLY"
    assert calls == [1]
    assert bindings(agent, case_id, run["id"]) == [old_binding]


def test_same_context_does_not_publish_a_cached_decision_that_fails_validation(recovery_system):
    system = recovery_system
    case_id = create_case(system.business, "refund_status")
    owned, calls = [True], []

    def transport(*_):
        calls.append(1)
        owned[0] = False
        return reply()

    agent = worker(system, transport, owned)
    run = start(agent, system.business, case_id)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"])
    with Session(system.engine) as db, db.begin():
        row = db.get_one(ModelAttempt, (run["id"], 0, "single:0:0", 1))
        row.result = {
            "result": {
                **row.result["result"],
                "citations": [
                    {"id": "POL12", "version": "1.0", "quote": "没有出现在适用条款中的文本"}
                ],
            }
        }
    owned[0] = True
    agent.process(run["id"], expected_cycle=0)
    assert_refused(agent, case_id, run["id"], "REPLAY_VALIDATION_FAILED")
    assert calls == [1]


@pytest.mark.parametrize("durable_work", ["attempt", "checkpoint"])
def test_legacy_active_work_without_context_binding_is_rejected(
    recovery_system, monkeypatch, durable_work
):
    system = recovery_system
    case_id = create_case(system.business, "refund_status")
    owned, calls = [True], []

    def transport(*_):
        calls.append(1)
        owned[0] = False
        return reply()

    agent = worker(system, transport if durable_work == "attempt" else None, owned)
    original_finish = (
        lose_before_publication(agent, owned, monkeypatch) if durable_work == "checkpoint" else None
    )
    run = start(agent, system.business, case_id)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"])
    assert bool(attempts(system.engine, run["id"])) == (durable_work == "attempt")
    with Session(system.engine) as db, db.begin():
        removed = db.execute(
            delete(RunEvent).where(RunEvent.run_id == run["id"], RunEvent.kind == "context_binding")
        )
        assert removed.rowcount == 1
    if original_finish is not None:
        monkeypatch.setattr(agent, "_finish", original_finish)
    owned[0] = True
    agent.process(run["id"], expected_cycle=0)
    assert_refused(agent, case_id, run["id"], "RECOVERY_CONTEXT_UNBOUND")
    assert calls == ([1] if durable_work == "attempt" else [])
    assert bindings(agent, case_id, run["id"]) == []


def test_legacy_queued_run_with_no_durable_work_can_bind_context(recovery_system):
    system = recovery_system
    case_id = create_case(system.business, "refund_status")
    agent = worker(system, None, [True])
    run = start(agent, system.business, case_id)
    assert bindings(agent, case_id, run["id"]) == []
    with Session(system.engine) as db:
        assert db.get_one(AgentRun, run["id"]).status == "QUEUED"
    agent.process(run["id"], expected_cycle=0)
    assert agent.get(case_id, run["id"], SUPPORT)["status"] == "COMPLETED"
    (binding,) = bindings(agent, case_id, run["id"])
    assert binding["cycle"] == 0 and len(binding["digest"]) == 64


def test_final_context_comparison_rejects_same_version_ledger_drift(recovery_system):
    system = recovery_system
    business = system.business
    case_id = create_case(business, "refund_status")
    sibling_case_id, proposal_id = approved_sibling(business)
    business.payments = UnknownThenFailedPayment()
    unknown = execute_sibling(business, (sibling_case_id, proposal_id))
    assert unknown["status"] == "UNKNOWN"
    before = business.context(case_id, SUPPORT)

    def transport(*_):
        reconciled = business.reconcile(
            sibling_case_id,
            unknown["id"],
            SUPPORT,
            expected_version=business.context(sibling_case_id, SUPPORT)["case"]["version"],
            idempotency_key=str(uuid4()),
        )
        assert reconciled["status"] == "FAILED"
        return reply()

    agent = worker(system, transport, [True])
    run = start(agent, business, case_id)
    agent.process(run["id"], expected_cycle=0)
    assert business.context(case_id, SUPPORT)["versions"] == before["versions"]
    assert_refused(agent, case_id, run["id"], "FACTS_CHANGED_DURING_RUN")


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_target_refund_is_bound_and_published_without_models(recovery_system, mode):
    system = recovery_system
    business = system.business
    case_id = create_case(business, "refund_status")
    recorded = execute_sibling(business, approved_sibling(business))
    agent = worker(system, lambda *_: pytest.fail("read-only lookup called a model"), [True])
    agent.intent_probe = lambda *_: pytest.fail("read-only lookup called intent probe")
    run = agent.start(
        case_id,
        SUPPORT,
        expected_version=business.context(case_id, SUPPORT)["case"]["version"],
        idempotency_key=str(uuid4()),
        mode=mode,
        strategy="economy",
        message=f"请帮我查询退款执行编号：{recorded['id']} 的状态。",
    )
    agent.process(run["id"], expected_cycle=0)
    result = agent.get(case_id, run["id"], SUPPORT)
    assert result["status"] == "COMPLETED" and result["reason"] == "STATUS_ONLY"
    output = result["output"]
    target = output["refund_lookup"]["execution"]
    assert output["refund_lookup"]["status"] == "FOUND"
    assert target["id"] == recorded["id"] and target["receipt_confirmed"]
    assert output["ledger"] == [{**target, "action": "REFUND"}]
    assert output["action"] is None and output["lines"] == []
    assert "实际到账需另行核对" in output["notes"]
    assert [item["id"] for item in output["citations"]] == ["POL08"]
    assert output["citation_assessments"][0]["scope"] == {
        "kind": "MATCHED_REFUND_EXECUTION",
        "execution_ids": [recorded["id"]],
    }
    assert output["citation_assessments"][0]["condition_status"] == "MET"
    assert attempts(system.engine, run["id"]) == []
    assert bindings(agent, case_id, run["id"])[0]["version"] == "agent-context-v2-refund"


def test_target_refund_terminal_checkpoint_rejects_old_selected_status(
    recovery_system, monkeypatch
):
    system = recovery_system
    business = system.business
    case_id = create_case(business, "refund_status")
    sibling_case_id, proposal_id = approved_sibling(business)
    business.payments = UnknownThenFailedPayment()
    original = execute_sibling(business, (sibling_case_id, proposal_id))
    before = business.context(case_id, SUPPORT)
    owned = [True]
    agent = worker(system, None, owned)
    run = agent.start(
        case_id,
        SUPPORT,
        expected_version=before["case"]["version"],
        idempotency_key=str(uuid4()),
        mode="single",
        strategy="economy",
        message=f"退款执行记录编号：{original['id']}",
    )
    finish = lose_before_publication(agent, owned, monkeypatch)
    with pytest.raises(QueueFenceLost):
        agent.process(run["id"], expected_cycle=0)
    checkpoint = agent.graphs["single"].get_state({"configurable": {"thread_id": run["thread_id"]}})
    assert checkpoint.values["output"]["refund_lookup"]["execution"]["status"] == "UNKNOWN"
    reconciled = mutate(
        business,
        sibling_case_id,
        SUPPORT,
        lambda case, actor, **values: business.reconcile(case, original["id"], actor, **values),
    )
    assert reconciled["status"] == "FAILED"
    assert business.context(case_id, SUPPORT)["versions"] == before["versions"]
    monkeypatch.setattr(agent, "_finish", finish)
    owned[0] = True
    agent.process(run["id"], expected_cycle=0)
    assert_refused(agent, case_id, run["id"], "RECOVERY_CONTEXT_CHANGED")
    assert attempts(system.engine, run["id"]) == []


def test_unmatched_target_does_not_publish_a_different_refund(recovery_system):
    system = recovery_system
    business = system.business
    case_id = create_case(business, "refund_status")
    other = execute_sibling(business, approved_sibling(business))
    missing_id = str(uuid4())
    agent = worker(system, lambda *_: pytest.fail("unmatched lookup called a model"), [True])
    run = agent.start(
        case_id,
        SUPPORT,
        expected_version=business.context(case_id, SUPPORT)["case"]["version"],
        idempotency_key=str(uuid4()),
        mode="single",
        strategy="economy",
        message=f"退款执行编号：{missing_id}",
    )
    agent.process(run["id"], expected_cycle=0)
    result = agent.get(case_id, run["id"], SUPPORT)
    assert result["status"] == "COMPLETED" and result["reason"] == "STATUS_ONLY"
    output = result["output"]
    assert output["refund_lookup"] == {"status": "NOT_FOUND", "execution": None}
    assert output["ledger"] == [] and output["citations"] == []
    assert missing_id not in str(output) and other["id"] not in str(output)
    assert attempts(system.engine, run["id"]) == []
