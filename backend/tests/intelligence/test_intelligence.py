import asyncio
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.config import ROOT
from aftercare.db import Base, User
from aftercare.intelligence.api import router
from aftercare.intelligence.models import (
    ModelRunner,
    ModelSpec,
    Registry,
    TaskSpec,
    _estimated_price,
    _failure,
    _reserve_price,
    eligible_models,
)
from aftercare.intelligence.policies import applicable, bm25, rrf
from aftercare.intelligence.service import AgentService, QueueFenceLost, RunError
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

POLICY = {
    "id": "P1",
    "title": "损坏材料",
    "version": "1.0",
    "text": "商品损坏需要商品损坏照和包裹照。",
    "effective_at": "2025-01-01T00:00:00+08:00",
    "intents": ["damaged"],
}


class Business:
    def __init__(self):
        self.version = 1
        self.intent = "damaged"
        self.status = "WAITING_INPUT"
        self.reason_code = None
        self.missing_materials = None
        self.materials_version = 0
        self.reviews = []
        self.ledger = []
        self.refunded_cents = 0

    def validate_draft(self, case_id, user, *, expected_version, action, lines, policy_ids):
        assert self.version == expected_version
        assert self.context(case_id, user)
        if action != "REFUND" or any(line["amount_cents"] > 1300 for line in lines):
            raise BusinessError("REFUND_CAP_EXCEEDED", "模拟业务规则拒绝")
        return {"policy_ids": policy_ids}

    def context(self, case_id, user):
        assert case_id == "case-1"
        assert user.user_id == "support"
        return {
            "case": {
                "id": case_id,
                "intent": self.intent,
                "version": self.version,
                "materials_version": self.materials_version,
                "created_at": "2026-01-01T12:00:00+08:00",
                "description": "商品损坏",
            },
            "order": {"refunded_cents": self.refunded_cents},
            "policies": [POLICY],
            "reviews": self.reviews,
            "materials": [],
            "ledger": self.ledger,
            "versions": {"case": self.version, "policies": "p1"},
            "eligibility": {
                "status": self.status,
                "reason_code": self.reason_code,
                "policy_ids": ["P1"],
                "missing_materials": self.missing_materials
                if self.missing_materials is not None
                else ["ITEM", "PARCEL"]
                if self.status == "WAITING_INPUT"
                else [],
                "actions": ["REFUND"] if self.status == "ELIGIBLE" else [],
                "available_cents": 1300,
                "lines": [{"line_id": "L1", "available_cents": 1300}],
            },
        }


def registry(**overrides):
    values = {
        "version": "controlled",
        "allow_unverified": True,
        "models": [
            {
                "alias": alias,
                "provider": "fixture",
                "model": alias,
                "endpoint": "http://127.0.0.1:1/v1",
                "api_key_env": "TEST_KEY",
            }
            for alias in ("economy", "analysis")
        ],
    }
    return Registry.model_validate({**values, **overrides})


@pytest.fixture
def environment():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            User(
                id="support",
                role="support",
                enabled=True,
                token_hash="0" * 64,
                customer_id=None,
                display_name="Test Support",
            )
        )
    return engine, Business(), UserContext(user_id="support", role="support"), InMemorySaver()


def _service(environment, *, model=None, dispatch=None, attempt_admission=None):
    engine, business, _, saver = environment
    settings = SimpleNamespace(model_enabled=False)
    return AgentService(
        engine,
        settings,
        business,
        saver,
        model=model,
        dispatch=dispatch,
        attempt_admission=attempt_admission,
    )


def _start(service, user, **overrides):
    return service.start(
        "case-1",
        user,
        **{
            "expected_version": 1,
            "idempotency_key": "start",
            "mode": "multi",
            "strategy": "routed",
            "message": "商品破损",
            **overrides,
        },
    )


@pytest.fixture
def run_http(environment):
    engine, _, actor, _ = environment
    service = _service(environment)
    run = _start(service, actor)
    token = "controlled-run-test-token".ljust(32, "-")
    with Session(engine) as session, session.begin():
        session.get(User, actor.user_id).token_hash = token_hash(token)
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    app.state.agent = service
    path = f"/cases/case-1/runs/{run['id']}"
    return app, path, {"Authorization": f"Bearer {token}"}, run["id"]


def test_run_event_pagination_reconnect_and_cursor_errors(run_http):
    app, path, headers, run_id = run_http
    with Session(app.state.engine) as session, session.begin():
        run = session.get(AgentRun, run_id)
        for sequence in range(run.event_seq + 1, 204):
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=sequence,
                    kind="test",
                    payload={},
                    created_at=run.created_at,
                )
            )
        run.event_seq = 203

    with TestClient(app, headers=headers) as client:
        first = client.get(f"{path}/events")
        assert first.status_code == 200
        assert [row["seq"] for row in first.json()] == list(range(1, 201))
        next_page = client.get(f"{path}/events?after=200")
        assert [row["seq"] for row in next_page.json()] == [201, 202, 203]
        replay = client.get(f"{path}/stream?after=2")
        assert replay.status_code == 200
        assert [int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")] == (
            list(range(3, 204))
        )
        resumed = client.get(f"{path}/stream?after=1", headers={"Last-Event-ID": "200"})
        assert resumed.status_code == 200
        assert resumed.headers["content-type"].startswith("text/event-stream")
        assert [line for line in resumed.text.splitlines() if line.startswith("id:")] == [
            "id: 201",
            "id: 202",
            "id: 203",
        ]
        assert "event: idle" in resumed.text
        assert client.get(f"{path}/events?after=203").json() == []
        empty = client.get(f"{path}/stream?after=203")
        assert empty.status_code == 200 and "id:" not in empty.text

        for route, query, request_headers in [
            ("events", "?after=204", {}),
            ("stream", "?after=204", {}),
            ("stream", "?after=1", {"Last-Event-ID": "204"}),
        ]:
            response = client.get(f"{path}/{route}{query}", headers=request_headers)
            assert response.status_code == 409
            assert response.headers["content-type"] == "application/json"
            assert response.json()["detail"]["code"] == "INVALID_EVENT_CURSOR"
        for header in ("-1", "+1", "1.0", "NaN", ""):
            response = client.get(f"{path}/stream", headers={"Last-Event-ID": header})
            assert response.status_code == 400
            assert response.json()["detail"]["code"] == "INVALID_CURSOR"
        for route in ("events", "stream"):
            assert client.get(f"{path}/{route}?after=-1").status_code == 422


def test_run_stream_stops_buffered_replay_after_token_revocation(run_http):
    app, path, headers, run_id = run_http
    with Session(app.state.engine) as session:
        assert session.get(AgentRun, run_id).event_seq > 1
    messages = []

    async def receive():
        await asyncio.Future()

    async def send(message):
        messages.append(message)
        if message.get("body", b"").startswith(b"id: 1\n"):
            with Session(app.state.engine) as session, session.begin():
                session.get(User, "support").enabled = False

    async def request():
        await asyncio.wait_for(
            app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "method": "GET",
                    "path": f"{path}/stream",
                    "query_string": b"",
                    "headers": [
                        (key.lower().encode(), value.encode()) for key, value in headers.items()
                    ],
                },
                receive,
                send,
            ),
            timeout=3,
        )

    asyncio.run(request())
    assert messages[0]["status"] == 200
    frames = [message["body"] for message in messages[1:] if message.get("body")]
    assert len(frames) == 1 and frames[0].startswith(b"id: 1\n")
    assert messages[-1]["more_body"] is False
    with TestClient(app, headers=headers) as client:
        assert client.get(f"{path}/stream", headers={"Last-Event-ID": "1"}).status_code == 401


@pytest.mark.parametrize(
    ("mode", "roles"),
    [
        ("multi", {"coordinator", "policy", "verifier"}),
        ("single", {"single"}),
    ],
)
def test_same_thread_pause_then_resume(environment, mode, roles):
    _, business, actor, _ = environment
    service = _service(environment)
    first = _start(service, actor, mode=mode)
    assert first["status"] == "WAITING_INPUT"
    assert first["output"]["missing_materials"] == ["ITEM", "PARCEL"]
    assert _start(service, actor, mode=mode)["id"] == first["id"]
    with pytest.raises(RunError, match="补充"):
        service.transition(
            "case-1",
            first["id"],
            actor,
            expected_version=1,
            idempotency_key="early",
            operation="resume",
        )
    business.version, business.status = 2, "HUMAN_REVIEW"
    # Recompile using the same persisted checkpoint store.
    restarted = _service(environment)
    second = restarted.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="resume",
        operation="resume",
    )
    assert second["status"] == "NEEDS_REVIEW"
    assert second["reason"] == "HUMAN_REVIEW"
    assert second["thread_id"] == first["thread_id"]
    assert second["cycle"] == 1
    events = service.events("case-1", first["id"], actor, 0)
    assert {e["payload"]["role"] for e in events if e["kind"] == "agent"} == roles
    assert all(
        e["payload"]["source"] == "rules"
        for e in events
        if e["kind"] == "agent" and e["payload"]["stage"] == "start"
    )
    assert service.events("case-1", first["id"], actor, events[-2]["seq"]) == events[-1:]


def test_separate_model_roles_with_real_tool_message_shape(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    selected = []

    def fixture_transport(spec, messages, tools, _timeout, _output):
        selected.append(spec.alias)
        if tools:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_case_facts",
                        "args": {},
                        "id": "fact-call",
                    }
                ],
            )
        assert isinstance(messages[-1], ToolMessage)
        assert messages[-1].tool_call_id == "fact-call"
        return AIMessage(content='{"intent":"damaged","notes":"需要人工核实"}')

    service = _service(environment, model=ModelRunner(registry(), transport=fixture_transport))
    result = _start(service, actor)
    assert result["status"] == "COMPLETED"
    assert selected == ["economy", "economy", "analysis", "analysis", "analysis", "analysis"]
    events = service.events("case-1", result["id"], actor, 0)
    assert [e["payload"]["role"] for e in events if e["kind"] == "tool"] == [
        "coordinator",
        "policy",
        "verifier",
    ]
    attempts = [e["payload"] for e in events if e["kind"] == "attempt"]
    assert len(attempts) == 12
    assert all(a["estimated_cost_usd"] is None for a in attempts)
    service.process(result["id"])
    assert len(selected) == 6


def test_multi_role_handoff_fits_cost_cap_without_reusing_upstream_quotes(environment, monkeypatch):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    policy_text = "商品损坏需要商品损坏照和包裹照。" + "退款金额受已付金额及已执行退款约束。" * 25
    monkeypatch.setitem(POLICY, "text", policy_text)
    model_config = registry(max_usd="0.05", max_run_usd="0.10").model_dump()
    for spec, rates in zip(
        model_config["models"],
        [("1", "5", "0.1", "1.25"), ("2", "10", "0.2", "2.5")],
        strict=True,
    ):
        spec.update(
            zip(
                (
                    "input_usd_per_million",
                    "output_usd_per_million",
                    "cache_read_usd_per_million",
                    "cache_write_usd_per_million",
                ),
                rates,
                strict=True,
            ),
            price_date=date(2026, 1, 1),
            source="controlled",
            cache_write_ttl="30m",
        )
    usage = [
        (906, 25, 0),
        (1276, 516, 1273),
        (1053, 16, 1050),
        (2012, 765, 2009),
        (1627, 16, 1624),
        (1500, 300, 0),
    ]
    roles = ("coordinator", "policy", "verifier")
    calls = []
    notes = "独立核验的详细意见。" * 35
    line = {"line_id": "L1", "amount_cents": 1300}
    citation = {"id": "P1", "version": "1.0", "quote": policy_text}

    def transport(spec, messages, tools, *_):
        index = len(calls)
        role = roles[index // 2]
        calls.append((role, spec.alias, bool(tools)))
        input_tokens, output_tokens, cache_writes = usage[index]
        metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {"cache_creation": cache_writes},
        }
        if role == "verifier":
            upstream = json.loads(messages[1].content)["unverified_role_opinions"]
            assert upstream["coordination"]["lines"] == [line]
            assert upstream["policy"]["citations"] == [{"id": "P1", "version": "1.0"}]
            assert notes not in messages[1].content and policy_text not in messages[1].content
        if tools:
            return AIMessage(
                content="",
                tool_calls=[{"name": "read_case_facts", "args": {}, "id": f"facts-{role}"}],
                usage_metadata=metadata,
            )
        assert isinstance(messages[-1], ToolMessage)
        if role != "coordinator":
            policies = json.loads(messages[-1].content)["policies"]["applicable_policies"]
            assert [p["text"] for p in policies] == [policy_text]
        return AIMessage(
            content=json.dumps(
                {
                    "intent": "damaged",
                    "notes": notes,
                    "action": "REFUND",
                    "lines": [line],
                    "citations": [] if role == "coordinator" else [citation],
                },
                ensure_ascii=False,
            ),
            usage_metadata=metadata,
        )

    service = _service(
        environment, model=ModelRunner(Registry.model_validate(model_config), transport=transport)
    )
    result = _start(service, actor)
    assert result["status"] == "COMPLETED"
    assert result["output"]["lines"] == [line]
    assert result["output"]["citations"] == [citation]
    assert calls == [
        (role, "economy" if role == "coordinator" else "analysis", requests_tool)
        for role in roles
        for requests_tool in (True, False)
    ]
    attempts = [
        event["payload"]
        for event in service.events("case-1", result["id"], actor, 0)
        if event["kind"] == "attempt"
    ]
    spent = sum(Decimal(a["estimated_cost_usd"]) for a in attempts[:-2] if a["estimated_cost_usd"])
    assert spent == Decimal("0.02490075")
    assert spent + Decimal(attempts[-2]["reserved_cost_usd"]) <= Decimal("0.05")


def test_invalid_output_repaired_then_restricted_by_budget(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []

    def fixture_transport(spec, *_):
        calls.append(spec.alias)
        return AIMessage(content='{"intent":"damaged","notes":0}')

    service = _service(
        environment,
        model=ModelRunner(registry(max_attempts=2), transport=fixture_transport),
    )
    result = _start(service, actor)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"] == "MODEL_BUDGET_EXHAUSTED"
    assert calls == ["economy", "economy"]


def test_queued_cancel_and_idempotency(environment):
    _, _, actor, _ = environment
    queued = []
    service = _service(environment, dispatch=queued.append)
    result = _start(service, actor)
    assert result["status"] == "QUEUED"
    assert service.pending() == queued == [result["id"]]
    with pytest.raises(RunError) as conflict:
        _start(service, actor, mode="single")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    canceled = service.transition(
        "case-1",
        result["id"],
        actor,
        expected_version=1,
        idempotency_key="stop",
        operation="cancel",
    )
    service.process(result["id"])
    assert service.pending() == []
    assert canceled["status"] == "CANCELED"


def test_unknown_interrupted_attempt_is_recorded_and_never_replayed(environment, monkeypatch):
    engine, business, actor, _ = environment
    business.status = "ELIGIBLE"
    service = _service(
        environment,
        dispatch=lambda _: None,
        model=ModelRunner(
            registry(),
            transport=lambda *_: pytest.fail("Unexpected model call"),
        ),
    )
    run = _start(service, actor)

    def stop_after_context_binding(*_args, **_kwargs):
        raise QueueFenceLost()

    graph = service.graphs[run["mode"]]
    with monkeypatch.context() as patch:
        patch.setattr(graph, "invoke", stop_after_context_binding)
        with pytest.raises(QueueFenceLost):
            service.process(run["id"])
    assert any(
        row["kind"] == "context_binding" for row in service.events("case-1", run["id"], actor, 0)
    )
    with Session(engine) as session, session.begin():
        row = session.get(AgentRun, run["id"])
        session.add(
            ModelAttempt(
                run_id=row.id,
                cycle=row.cycle,
                logical_id="coordinator:0:0",
                ordinal=1,
                metadata_json={},
                status="STARTED",
                started_at=row.created_at,
            )
        )
    service.process(run["id"])
    assert service.get("case-1", run["id"], actor)["reason"] == "INTERRUPTED_MODEL_CALL"
    with Session(engine) as session:
        attempt = session.get(ModelAttempt, (run["id"], 0, "coordinator:0:0", 1))
        assert attempt.status == "UNKNOWN"


def test_policy_scope_time_rank_and_version_conflict():
    newer = {**POLICY, "version": "2.0", "effective_at": "2026-02-01T00:00:00+08:00"}
    unrelated = {**POLICY, "id": "P2", "intents": ["refund_status"]}
    valid, blockers = applicable(
        [POLICY, newer, unrelated],
        intent="damaged",
        as_of="2026-01-01T00:00:00+08:00",
    )
    assert valid == [POLICY] and blockers == []
    conflicting = {**deepcopy(POLICY), "text": "存在同一生效时间的相互冲突的条款"}
    assert applicable(
        [POLICY, conflicting],
        intent="damaged",
        as_of="2026-01-01T00:00:00+08:00",
    ) == ([], ["CONFLICTING_POLICY:P1"])
    assert bm25("商品损坏照片", {"a": POLICY["text"], "b": "退款进度"}) == ["a"]
    assert rrf(["a", "b"], ["b", "a", "c"]) == ["a", "b", "c"]


def test_registry_rejects_unverified_incompatible_and_missing_budget():
    config = registry(allow_unverified=False)
    assert eligible_models(config, TaskSpec("analysis", 10)) == (
        [],
        {"economy": "UNVERIFIED", "analysis": "UNVERIFIED"},
    )
    config = registry(max_usd="0.02")
    assert eligible_models(config, TaskSpec("analysis", 10)) == (
        [],
        {"economy": "UNPRICED_MODEL", "analysis": "UNPRICED_MODEL"},
    )
    config = registry()
    config.models[0].tool_calls = False
    config.models[1].context_tokens = 100
    assert eligible_models(config, TaskSpec("analysis", 10)) == (
        [],
        {"economy": "TOOLS_UNSUPPORTED", "analysis": "CONTEXT_LIMIT"},
    )


def test_rate_limit_switch_and_cooldown_never_retries_same_provider_attempt(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []

    class RateLimit(Exception):
        status_code = 429

    def fixture_transport(spec, *_):
        calls.append(spec.alias)
        if spec.alias == "economy":
            raise RateLimit()
        return AIMessage(content='{"intent":"damaged","notes":"需要材料"}')

    service = _service(environment, model=ModelRunner(registry(), transport=fixture_transport))
    result = _start(service, actor)
    assert result["status"] == "COMPLETED"
    assert calls == ["economy", "analysis", "analysis", "analysis"]
    events = service.events("case-1", result["id"], actor, 0)
    assert [e["payload"]["reason"] for e in events if e["kind"] == "model_switch"] == [
        "MODEL_RATE_LIMIT",
    ]


def test_multirole_conflict_has_one_rework_and_never_publishes_draft(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"

    def fixture_transport(_spec, messages, *_):
        if "独立识别" in messages[0].content:
            return AIMessage(content='{"intent":"wrong_item","notes":"错发"}')
        return AIMessage(content='{"intent":"damaged","notes":"损坏"}')

    service = _service(environment, model=ModelRunner(registry(), transport=fixture_transport))
    result = _start(service, actor)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"] == "ROLE_CONFLICT"
    assert result["output"]["action"] is None
    events = service.events("case-1", result["id"], actor, 0)
    assert [e["payload"]["decision"] for e in events if e["kind"] == "conflict"] == [
        "REWORK",
        "HUMAN_REVIEW",
    ]


def test_draft_rejects_claims_exceeding_order_line_and_validates_citations(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"

    def fixture_transport(_spec, messages, tools, *_):
        if tools:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_case_facts",
                        "args": {},
                        "id": "facts",
                    }
                ],
            )
        verified = json.loads(messages[-1].content)
        assert verified["policies"]["applicable_policies"][0]["text"] == POLICY["text"]
        return AIMessage(
            content=json.dumps(
                {
                    "intent": "damaged",
                    "notes": "拟退",
                    "action": "REFUND",
                    "lines": [{"line_id": "L1", "amount_cents": 1301}],
                    "citations": [
                        {"id": "P1", "version": "1.0", "quote": "商品损坏需要商品损坏照"}
                    ],
                }
            )
        )

    service = _service(environment, model=ModelRunner(registry(), transport=fixture_transport))
    result = _start(service, actor, mode="single")
    assert result["reason"] == "DRAFT_RULE_CONFLICT"
    assert result["output"]["action"] is None


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_material_review_pauses_again_before_eligibility(environment, mode):
    _, business, actor, _ = environment
    service = _service(environment)
    first = _start(service, actor, mode=mode)
    business.version, business.status = 2, "HUMAN_REVIEW"
    business.reason_code = "MATERIAL_REVIEW_REQUIRED"
    second = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="upload",
        operation="resume",
    )
    assert second["status"] == "WAITING_INPUT"
    assert second["reason"] == "MATERIAL_REVIEW_REQUIRED"
    assert second["output"]["missing_materials"] == []
    business.version, business.status = 3, "ELIGIBLE"
    business.reason_code = None
    third = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=3,
        idempotency_key="review",
        operation="resume",
    )
    assert third["status"] == "COMPLETED"
    assert third["thread_id"] == first["thread_id"]
    assert third["cycle"] == 2


def _legacy_attempt(engine, run_id, cost):
    # Retain earlier usage from a run started before deterministic gate moved ahead of the model.
    with Session(engine) as session, session.begin():
        row = session.get(AgentRun, run_id)
        session.add(
            ModelAttempt(
                run_id=run_id,
                cycle=0,
                logical_id="old:0:0",
                ordinal=1,
                metadata_json={},
                status="SUCCEEDED",
                started_at=row.created_at,
                estimated_cost_usd=cost,
            )
        )


def test_run_cap_survives_material_resume(environment):
    engine, business, actor, _ = environment
    calls = []

    def fixture_transport(spec, *_):
        calls.append(spec.alias)
        return AIMessage(content='{"intent":"damaged","notes":"需要材料"}')

    service = _service(
        environment,
        model=ModelRunner(registry(max_run_attempts=1), transport=fixture_transport),
    )
    first = _start(service, actor)
    assert first["status"] == "WAITING_INPUT"
    _legacy_attempt(engine, first["id"], cost=None)
    business.version, business.status = 2, "ELIGIBLE"
    resumed = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="cap",
        operation="resume",
    )
    assert resumed["reason"] == "MODEL_RUN_BUDGET_EXHAUSTED"
    assert calls == []
    routes = [
        e["payload"]["budget"]
        for e in service.events("case-1", first["id"], actor, 0)
        if e["kind"] == "route"
    ]
    assert routes[-1]["attempts_used"] == 0
    assert routes[-1]["run_attempts_used"] == routes[-1]["run_attempts_limit"] == 1


def test_run_cost_cap_counts_prior_cycle(environment):
    engine, business, actor, _ = environment
    calls = []

    def fixture_transport(spec, *_):
        calls.append(spec.alias)
        return AIMessage(
            content='{"intent":"damaged","notes":"需要材料"}',
            usage_metadata={"input_tokens": 9000, "output_tokens": 500, "total_tokens": 9500},
        )

    config = registry(max_run_usd="0.010")
    for spec in config.models:
        spec.source = "controlled fixture"
        spec.price_date = date(2026, 11, 10)
        spec.input_usd_per_million = Decimal(1)
        spec.output_usd_per_million = Decimal(1)
    service = _service(environment, model=ModelRunner(config, transport=fixture_transport))
    first = _start(service, actor)
    assert first["status"] == "WAITING_INPUT"
    _legacy_attempt(engine, first["id"], cost="0.0095")
    business.version, business.status = 2, "ELIGIBLE"
    resumed = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="cost",
        operation="resume",
    )
    assert resumed["reason"] == "MODEL_RUN_COST_BUDGET_EXHAUSTED"
    assert calls == []


def _priced_openai(**overrides):
    return ModelSpec.model_validate(
        {
            "alias": "analysis",
            "provider": "openai",
            "model": "gpt-6.1-sol",
            "endpoint": "http://127.0.0.1:1/v1",
            "interface": "responses",
            "api_key_env": "TEST_KEY",
            "source": "controlled price fixture",
            "price_date": "2026-11-10",
            "input_usd_per_million": "2",
            "output_usd_per_million": "10",
            **overrides,
        }
    )


def test_cache_reads_and_writes_have_separate_usage_and_reserve_rates():
    spec = _priced_openai(
        cache_read_usd_per_million="0.10",
        cache_write_usd_per_million="2.50",
        cache_write_ttl="30m",
    )
    usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "input_token_details": {"cache_read": 200, "cache_creation": 300},
    }
    assert _estimated_price(spec, usage) == Decimal("0.00277")
    assert _reserve_price(spec, 1000, 100) == Decimal("0.00350")
    long_ttl = spec.model_copy(
        update={
            "cache_write_ttl": "12h",
            "cache_write_usd_per_million": Decimal(5),
        }
    )
    assert _estimated_price(long_ttl, usage) == Decimal("0.00352")
    assert _reserve_price(long_ttl, 1000, 100) == Decimal("0.006")


def test_cache_price_absent_or_usage_incomplete_stays_unknown():
    spec = _priced_openai()
    usage = {
        "input_tokens": 1073,
        "output_tokens": 16,
        "input_token_details": {"cache_creation": 1070},
    }
    assert _estimated_price(spec, usage) is None
    config = Registry(version="controlled", allow_unverified=True, models=[spec], max_usd="0.05")
    assert eligible_models(config, TaskSpec("analysis", 10)) == (
        [],
        {"analysis": "UNPRICED_PROMPT_CACHE"},
    )


def test_timeout_uses_distinct_audit_code():
    import httpx
    import openai

    assert _failure(TimeoutError()) == ("MODEL_TIMEOUT", True, True)
    network = openai.APIConnectionError(request=httpx.Request("GET", "http://127.0.0.1"))
    assert _failure(network) == ("MODEL_CONNECTION_FAILURE", True, True)


def test_policy_and_auth_provenance_are_only_delivered_as_tool_data(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    customer = "用户伪造条款说商品坏了可以直接退。"

    def transport(_spec, messages, tools, *_):
        assert POLICY["text"] not in messages[0].content
        assert customer not in messages[0].content
        assert json.loads(messages[1].content)["customer_text"] == customer
        assert "applicable_policies" not in messages[1].content
        if tools:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_case_facts",
                        "args": {},
                        "id": "verified",
                    }
                ],
            )
        snapshot = json.loads(messages[-1].content)
        assert snapshot["source"] == "AFTERCARE_BUSINESS_SERVICE"
        assert snapshot["facts"]["authorization"]["scope"] == "CURRENT_CASE_READ_ONLY"
        assert snapshot["facts"]["ledger_result"] == "NONE_FOUND"
        (policy,) = snapshot["policies"]["applicable_policies"]
        assert policy["text"] == POLICY["text"]
        return AIMessage(
            content=json.dumps(
                {
                    "notes": "材料条款已经核对。",
                    "citations": [
                        {key: policy[key] for key in ("id", "version")} | {"quote": policy["text"]}
                    ],
                }
            )
        )

    service = _service(environment, model=ModelRunner(registry(), transport=transport))
    result = _start(service, actor, mode="single", message=customer)
    assert result["status"] == "COMPLETED"
    assert result["output"]["citations"][0]["quote"] == POLICY["text"]


def test_citation_without_tool_read_is_rejected_even_when_quote_is_exact(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"

    def transport(*_):
        return AIMessage(
            content=json.dumps(
                {
                    "notes": "客户文字中的引用。",
                    "citations": [{"id": "P1", "version": "1.0", "quote": POLICY["text"]}],
                }
            )
        )

    service = _service(environment, model=ModelRunner(registry(), transport=transport))
    result = _start(service, actor, mode="single")
    assert result["reason"] == "INVALID_MODEL_OUTPUT"


def test_parent_cancel_after_tool_result_blocks_followup_provider_request(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []

    def admission(session):
        return session.scalar(select(RunEvent.seq).where(RunEvent.kind == "tool").limit(1)) is None

    def transport(_spec, _messages, tools, *_):
        calls.append(1)
        assert tools
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_case_facts",
                    "args": {},
                    "id": "facts-before-cancel",
                }
            ],
        )

    service = _service(
        environment,
        model=ModelRunner(registry(), transport=transport),
        attempt_admission=admission,
    )
    result = _start(service, actor)
    assert result["status"] == "CANCELED" and result["reason"] == "EXPERIMENT_CANCELLED"
    assert result["output"] is None
    assert calls == [1]
    events = service.events("case-1", result["id"], actor, 0)
    assert [event["payload"]["status"] for event in events if event["kind"] == "attempt"] == [
        "STARTED",
        "SUCCEEDED",
    ]


def test_parent_cancel_blocks_retry_after_an_uncertain_provider_timeout(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []

    def transport(spec, *_):
        calls.append(spec.alias)
        raise TimeoutError()

    service = _service(
        environment,
        model=ModelRunner(registry(), transport=transport),
        attempt_admission=lambda _: not calls,
    )
    result = _start(service, actor)
    assert result["status"] == "CANCELED" and result["output"] is None
    assert calls == ["economy"]
    attempts = [
        e["payload"]
        for e in service.events("case-1", result["id"], actor, 0)
        if e["kind"] == "attempt"
    ]
    assert [attempt["status"] for attempt in attempts] == ["STARTED", "UNKNOWN"]
    assert attempts[-1]["usage"] is None and attempts[-1]["estimated_cost_usd"] is None


def test_in_flight_provider_usage_preserved_when_cancelled(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []

    def transport(*_):
        calls.append(1)
        return AIMessage(
            content='{"notes":"模型响应已返回"}',
            usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
        )

    service = _service(
        environment,
        model=ModelRunner(registry(), transport=transport),
        attempt_admission=lambda _: not calls,
    )
    result = _start(service, actor)
    assert result["status"] == "CANCELED" and result["output"] is None
    attempts = [
        e["payload"]
        for e in service.events("case-1", result["id"], actor, 0)
        if e["kind"] == "attempt"
    ]
    assert attempts[-1]["status"] == "SUCCEEDED"
    assert attempts[-1]["usage"]["total_tokens"] == 20


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_known_missing_materials_are_visible_with_no_compatible_model(environment, mode):
    _, _, actor, _ = environment
    model = ModelRunner(
        Registry(version="no-provider", models=[]),
        transport=lambda *_: pytest.fail("Known missing evidence requires no provider"),
    )
    service = _service(environment, model=model)
    run = _start(service, actor, mode=mode)
    assert run["status"] == "WAITING_INPUT"
    assert run["output"]["missing_materials"] == ["ITEM", "PARCEL"]
    assert not any(e["kind"] == "attempt" for e in service.events("case-1", run["id"], actor, 0))


@pytest.mark.parametrize(
    ("mode", "missing", "reason_code", "notes"),
    [
        (
            "multi",
            ["WRONG_ITEM", "LABEL"],
            None,
            "请上传错发商品实物照片、包裹外部标签照片。",
        ),
        ("single", ["LABEL"], None, "请上传包裹外部标签照片。"),
        (
            "multi",
            [],
            "MATERIALS_NEED_MORE",
            "当前材料审核要求补充，请查看本次材料审核意见。",
        ),
    ],
)
def test_wrong_item_wait_uses_only_missing_photos_or_review_guidance(
    environment, mode, missing, reason_code, notes
):
    _, business, actor, _ = environment
    business.intent, business.reason_code = "wrong_item", reason_code
    business.missing_materials = missing
    model = ModelRunner(
        Registry(version="no-provider", models=[]),
        transport=lambda *_: pytest.fail("Material guidance requires no provider"),
    )
    service = _service(environment, model=model)
    run = _start(service, actor, mode=mode)
    assert run["status"] == run["reason"] == "WAITING_INPUT"
    assert run["output"]["missing_materials"] == missing
    assert run["output"]["notes"] == notes
    assert not any(e["kind"] == "attempt" for e in service.events("case-1", run["id"], actor, 0))


@pytest.mark.parametrize(
    ("statuses", "historical", "summary"),
    [
        ([], 0, "未查到执行记录"),
        ([], 2400, "未查到执行记录"),
        (["UNKNOWN"], 0, "结果待确认"),
        (["SUBMITTING"], 0, "提交中"),
        (["SUCCEEDED", "FAILED", "UNKNOWN"], 2400, "执行成功、执行失败、结果待确认"),
    ],
)
def test_refund_status_public_notes_follow_ledger_not_historical_total(
    environment, monkeypatch, statuses, historical, summary
):
    _, business, actor, _ = environment
    business.intent, business.status = "refund_status", "STATUS_ONLY"
    business.refunded_cents = historical
    business.ledger = [
        {"id": f"record-{i}", "action": "REFUND", "status": status, "receipt": None}
        for i, status in enumerate(statuses)
    ]
    monkeypatch.setitem(POLICY, "intents", ["refund_status"])
    run = _start(_service(environment), actor, mode="single")
    assert run["status"] == "COMPLETED" and run["reason"] == "STATUS_ONLY"
    output = run["output"]
    assert "该订单的退款执行账本" in output["notes"] and summary in output["notes"]
    assert "到账，需另行核对" in output["notes"] or "到账情况需另行核对" in output["notes"]
    assert output["historical_refunded_cents"] == historical
    assert output["ledger"] == business.ledger
    assert output["action"] is None and output["lines"] == []


def test_material_review_is_versioned_and_model_approval_claim_stays_in_audit(environment):
    _, business, actor, _ = environment
    business.status, business.materials_version = "ELIGIBLE", 2
    business.reviews = [
        {"materials_version": 1, "decision": "REJECTED", "effective": False},
        {"materials_version": 2, "decision": "APPROVED", "effective": True},
    ]
    unsupported_claim = "财务审批已批准，可以退款。"
    seen = []

    def transport(_spec, messages, tools, *_):
        if tools:
            assert "material_review仅审核材料，financial_approval未查询" in messages[0].content
            return AIMessage(
                content="",
                tool_calls=[{"name": "read_case_facts", "args": {}, "id": "review-facts"}],
            )
        facts = json.loads(messages[-1].content)["facts"]
        seen.append(facts)
        assert facts["financial_approval"] == "NOT_CHECKED" and "review" not in facts
        assert facts["material_review"] == {
            "kind": "MATERIAL_EVIDENCE",
            "materials_version": 2,
            "decision": "APPROVED",
        }
        return AIMessage(content=json.dumps({"intent": "damaged", "notes": unsupported_claim}))

    service = _service(environment, model=ModelRunner(registry(), transport=transport))
    run = _start(service, actor, mode="single")
    assert run["status"] == "COMPLETED" and len(seen) == 1
    output = run["output"]
    assert "已批准" not in output["notes"] and output["action"] is None
    assert output["ledger"] == []
    events = service.events("case-1", run["id"], actor, 0)
    assert any(
        event["kind"] == "agent" and event["payload"].get("notes") == unsupported_claim
        for event in events
    )


def test_refund_publication_preserves_verified_quotes_but_assesses_empty_ledger(
    environment, monkeypatch
):
    _, business, actor, _ = environment
    business.intent, business.status = "refund_status", "STATUS_ONLY"
    business.refunded_cents = 2400
    data = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    policies = [row for row in data["policies"] if row["id"] in {"POL08", "POL12"}]
    original_context = business.context

    def context(*args):
        facts = original_context(*args)
        facts["policies"] = policies
        facts["policy_resolution"] = {"documents": policies, "by_product": {}, "blockers": []}
        facts["eligibility"]["policy_ids"] = [row["id"] for row in policies]
        return facts

    monkeypatch.setattr(business, "context", context)
    citations = [
        {"id": row["id"], "version": row["version"], "quote": row["text"]} for row in policies
    ]

    def transport(_spec, messages, tools, *_):
        if tools:
            return AIMessage(
                content="",
                tool_calls=[{"name": "read_case_facts", "args": {}, "id": "refund-ledger"}],
            )
        assert json.loads(messages[-1].content)["facts"]["ledger"] == []
        return AIMessage(
            content=json.dumps(
                {"intent": "refund_status", "notes": "已经核实执行超时。", "citations": citations}
            )
        )

    service = _service(environment, model=ModelRunner(registry(), transport=transport))
    run = _start(service, actor, mode="single")
    assert run["status"] == "COMPLETED" and run["reason"] == "STATUS_ONLY"
    output = run["output"]
    assert output["citations"] == citations
    assert [row["condition_status"] for row in output["citation_assessments"]] == ["UNMET", "UNMET"]
    assert all(row["scope"]["execution_ids"] == [] for row in output["citation_assessments"])
    assert "未查到执行记录" in output["notes"] and "超时" not in output["notes"]
    assert output["action"] is None and output["ledger"] == []
    history = service.events("case-1", run["id"], actor, 0)
    assert history[-1]["payload"]["output"] == output


def _native_registry():
    config = registry()
    models = [
        {
            **spec.model_dump(mode="json"),
            "streaming": True,
            "streaming_tools": True,
            "streaming_usage": True,
            "streaming_compatibility_version": "local-1",
            "streaming_checked_at": "2026-11-10",
        }
        for spec in config.models
    ]
    return Registry.model_validate(
        {
            **config.model_dump(mode="json"),
            "models": models,
            "native_streaming": True,
        }
    )


def test_partial_provider_tool_stream_creates_unknown_attempt_without_tool(environment):
    from time import monotonic

    from aftercare.intelligence.native_stream import collect_stream
    from langchain_core.messages import AIMessageChunk

    _, business, actor, _ = environment
    business.status = "ELIGIBLE"

    def native(_spec, _messages, _tools, _timeout, _output, on_delta):
        chunks = [
            AIMessageChunk(content="还在核实"),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "read_case_facts",
                        "args": "{}",
                        "id": "parseable-but-partial",
                        "index": 0,
                    }
                ],
            ),
        ]
        return collect_stream(
            chunks, interface="chat", deadline=monotonic() + 10, on_delta=on_delta
        )

    model = ModelRunner(
        _native_registry(),
        stream_transport=native,
        transport=lambda *_: pytest.fail("Unexpected non-streaming request"),
    )
    service = _service(environment, model=model)
    result = _start(service, actor, mode="single", strategy="economy")
    assert result["reason"] == "MODEL_STREAM_INTERRUPTED"
    assert result["output"] is None
    events = service.events("case-1", result["id"], actor, 0)
    assert not any(e["kind"] == "tool" for e in events)
    drafts = [e for e in events if e["kind"].startswith("model_draft_")]
    assert [e["kind"] for e in drafts] == ["model_draft_delta", "model_draft_discarded"]
    attempts = [e["payload"] for e in events if e["kind"] == "attempt"]
    assert attempts[-1]["status"] == "UNKNOWN"
    assert attempts[-1]["estimated_cost_usd"] is None and attempts[-1]["usage"] is None


def test_semantic_repair_does_not_echo_untrusted_field_name(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    observed = []

    def transport(_spec, messages, *_):
        observed.append(messages[-1].content)
        if len(observed) == 1:
            return AIMessage(content='{"notes":17,"secret-like-untrusted-field":"x"}')
        return AIMessage(content='{"notes":"核验完成"}')

    service = _service(environment, model=ModelRunner(registry(), transport=transport))
    result = _start(service, actor, mode="single", strategy="economy")
    assert result["status"] == "COMPLETED"
    assert "notes" in observed[-1] and "string_type" in observed[-1]
    assert "unknown_field" in observed[-1] and "secret-like-untrusted-field" not in observed[-1]


def test_null_action_with_leftover_lines_gets_field_specific_repair(environment):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    observed = []

    def transport(_spec, messages, *_):
        observed.append(messages[-1].content)
        if len(observed) == 1:
            return AIMessage(
                content=json.dumps(
                    {
                        "notes": "缺少具体退款请求。",
                        "action": None,
                        "lines": [{"line_id": "L1", "amount_cents": 1300}],
                    }
                )
            )
        return AIMessage(content='{"notes":"需客服确认处理方式","action":null,"lines":[]}')

    model = ModelRunner(registry(max_attempts=2), transport=transport)
    service = _service(environment, model=model)
    run = _start(service, actor, mode="single", strategy="economy")
    assert run["status"] == "COMPLETED"
    assert run["output"]["action"] is None and run["output"]["lines"] == []
    assert len(observed) == 2 and "LINES_REQUIRE_ACTION" in observed[-1]


def test_c001_style_resume_spends_only_after_verified_material_review(environment):
    _, business, actor, _ = environment
    calls = []

    def transport(spec, *_):
        calls.append(spec.alias)
        return AIMessage(content='{"intent":"damaged","notes":"已核验材料，待明确处理方式"}')

    model = ModelRunner(registry(max_run_attempts=12), transport=transport)
    service = _service(environment, model=model)
    first = _start(service, actor, mode="multi")
    assert first["status"] == "WAITING_INPUT"
    assert not calls

    business.version, business.status = 2, "HUMAN_REVIEW"
    business.reason_code = "MATERIAL_REVIEW_REQUIRED"
    second = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="review-wait",
        operation="resume",
    )
    assert second["reason"] == "MATERIAL_REVIEW_REQUIRED"
    assert second["thread_id"] == first["thread_id"]
    assert not calls

    business.version, business.status, business.reason_code = 3, "ELIGIBLE", None
    third = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=3,
        idempotency_key="review-confirmed",
        operation="resume",
    )
    assert third["status"] == "COMPLETED"
    assert third["thread_id"] == first["thread_id"]
    assert calls == ["economy", "analysis", "analysis"]
    attempts = [
        e["payload"]
        for e in service.events("case-1", first["id"], actor, 0)
        if e["kind"] == "attempt"
    ]
    assert {event["cycle"] for event in attempts} == {2}


def test_old_delivery_cannot_run_next_resume_cycle(environment):
    _, business, actor, _ = environment
    service = _service(environment, dispatch=lambda _: None)
    first = _start(service, actor)
    service.process(first["id"], expected_cycle=0)
    assert service.get("case-1", first["id"], actor)["status"] == "WAITING_INPUT"

    business.version, business.status = 2, "ELIGIBLE"
    resumed = service.transition(
        "case-1",
        first["id"],
        actor,
        expected_version=2,
        idempotency_key="resume-new-generation",
        operation="resume",
    )
    assert resumed["status"] == "QUEUED" and resumed["cycle"] == 1
    original = service.events("case-1", resumed["id"], actor, 0)
    service.process(resumed["id"], expected_cycle=0)
    assert service.get("case-1", resumed["id"], actor)["status"] == "QUEUED"
    assert service.events("case-1", resumed["id"], actor, 0) == original
    service.process(resumed["id"], expected_cycle=1)
    assert service.get("case-1", resumed["id"], actor)["status"] == "COMPLETED"


def test_lost_delivery_fences_graph_and_preserves_provider_usage(environment):
    engine, business, actor, _ = environment
    business.status = "ELIGIBLE"
    calls = []
    owned = [True]

    def transport(*_):
        calls.append(1)
        owned[0] = False
        return AIMessage(
            content='{"notes":"已核验状态"}',
            usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
        )

    service = _service(
        environment,
        dispatch=lambda _: None,
        model=ModelRunner(registry(), transport=transport),
    )
    service.queue_admission = lambda _session: owned[0]
    run = _start(service, actor, mode="single")
    with pytest.raises(QueueFenceLost, match="QUEUE_LEASE_LOST"):
        service.process(run["id"], expected_cycle=0)
    state = service.get("case-1", run["id"], actor)
    assert state["status"] == "RUNNING" and state["output"] is None and state["reason"] is None
    assert [
        e["payload"]["status"]
        for e in service.events("case-1", run["id"], actor, 0)
        if e["kind"] == "attempt"
    ] == ["STARTED"]
    with Session(engine) as db:
        attempt = db.get_one(ModelAttempt, (run["id"], 0, "single:0:0", 1))
        assert attempt.status == "SUCCEEDED" and attempt.usage["total_tokens"] == 20

    # A valid owner can use the completed model audit without issuing a second request.
    owned[0] = True
    service.process(run["id"], expected_cycle=0)
    assert service.get("case-1", run["id"], actor)["status"] == "COMPLETED"
    assert calls == [1]


def test_fenced_saver_never_delegates_lost_checkpoint_write():
    from contextlib import contextmanager

    from aftercare.intelligence.service import _FencedCheckpointSaver

    saver = InMemorySaver()
    delegated = []

    @contextmanager
    def lost():
        raise QueueFenceLost
        yield

    saver.put = lambda *_args, **_kwargs: delegated.append("checkpoint")
    saver.put_writes = lambda *_args, **_kwargs: delegated.append("writes")
    wrapped = _FencedCheckpointSaver(saver, lost)
    with pytest.raises(QueueFenceLost):
        wrapped.put({})
    with pytest.raises(QueueFenceLost):
        wrapped.put_writes({})
    assert delegated == []


@pytest.mark.parametrize(
    ("mode", "roles"), [("single", ["single"]), ("multi", ["policy", "verifier"])]
)
def test_retrieval_trace_records_every_consuming_role_in_run_and_cycle(environment, mode, roles):
    _, business, actor, _ = environment
    business.status = "ELIGIBLE"
    service = _service(environment)
    result = _start(service, actor, mode=mode)
    retrievals = [
        item["payload"]
        for item in service.events("case-1", result["id"], actor, 0)
        if item["kind"] == "retrieval"
    ]
    assert [item["role"] for item in retrievals] == roles
    assert [item["stage"] for item in retrievals] == (
        ["policy", "verification"] if mode == "multi" else ["policy"]
    )
    assert all(
        item["run_id"] == result["id"] and item["cycle"] == result["cycle"] == 0
        for item in retrievals
    )
