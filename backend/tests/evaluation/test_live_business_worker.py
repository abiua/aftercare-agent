"""Exercise the dedicated bootstrap with native Celery tasks and no broker or provider."""

from contextlib import ExitStack, contextmanager
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aftercare import queue
from aftercare.conversations import investigation_delivery
from aftercare.intelligence.models import ModelFailure

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/live_business_worker.py"
_SPEC = spec_from_file_location("live_business_worker", _SCRIPT)
worker = module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


def test_native_celery_investigation_dispatch_is_exact_and_one_shot(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        redis_url="cache+memory://",
        chat_analysis_enabled=True,
        chat_decisions_enabled=True,
        chat_investigation_enabled=True,
    )
    business = SimpleNamespace(settings=settings)
    turn = str(uuid4())
    seen = []
    model = object()

    @contextmanager
    def offline_runtime(profile):
        assert profile == "test"
        yield settings, object(), business, SimpleNamespace(model=None)

    def process(current_business, agent, turn_id):
        assert current_business is business and agent.model is model
        seen.append(turn_id)

    monkeypatch.setattr(queue, "load_settings", lambda _: settings)
    monkeypatch.setattr(worker.runtime, "open_runtime", offline_runtime)
    monkeypatch.setattr(worker, "_scope_readback", lambda *_: None)
    monkeypatch.setattr(worker, "WIRE", tmp_path)
    monkeypatch.setattr(investigation_delivery, "process", process)
    app = queue.make_queue("test")
    app.conf.result_backend = "cache+memory://"
    worker._install(app, {"kind": "TEXT", "turn_id": turn}, model, settings)
    selected = app.tasks[worker.TASKS["TEXT"]]
    for ident in (str(uuid4()), turn, turn):
        assert selected.apply(args=[ident], throw=True).successful()
    assert seen == [turn]
    assert (tmp_path / "native-dispatch.json").is_file()
    assert worker.runtime.open_runtime is offline_runtime
    assert app.tasks["aftercare.process"].apply(args=[str(uuid4())], throw=True).successful()


def test_wire_context_follows_native_reservation_and_blocks_retries(monkeypatch):
    events = []

    class Audit:
        def begin_attempt(self, *_args, **_kwargs):
            events.append("STARTED")
            return 12

    class Gate:
        @contextmanager
        def attempt(self, binding):
            assert binding == {"native": "proof"}
            events.append("wire")
            yield
            events.append("closed")

    monkeypatch.setattr(worker, "_attempt_binding", lambda *_: {"native": "proof"})
    with ExitStack() as stack:
        audit = worker._GateAudit(Audit(), Gate(), {}, stack)
        assert audit.begin_attempt("first", 1, None, Decimal("0.01"), None) == 12
        with pytest.raises(ModelFailure, match="RETRY_OR_UNPRICED"):
            audit.begin_attempt("first", 2, None, Decimal("0.01"), None)
    assert events == ["STARTED", "wire", "closed"]


def test_invalid_tool_response_preserves_native_reason_without_consuming_another_round(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from aftercare.intelligence.models import InvalidOutput, Registry

    events = []

    class Audit:
        cooldown_aliases = staticmethod(lambda _: set())
        replay = staticmethod(lambda *_, **__: None)
        budget = staticmethod(lambda _: {})
        event = staticmethod(lambda *_: None)
        claim_provider = staticmethod(lambda *_: None)
        release_provider = staticmethod(lambda *_, **__: None)
        confirm_admission = staticmethod(lambda: None)

        def begin_attempt(self, logical_id, ordinal, *_args, **_kwargs):
            events.append(("STARTED", logical_id, ordinal))
            return 30

        def finish_attempt(self, logical_id, ordinal, status, code, *_args, **_kwargs):
            events.append((status, logical_id, ordinal, code))

    class Gate:
        @contextmanager
        def attempt(self, _binding):
            yield

    registry = Registry.model_validate(
        {
            "version": "live-worker-controlled-test",
            "allow_unverified": True,
            "max_attempts": 8,
            "max_run_attempts": 8,
            "max_usd": "0.40",
            "max_run_usd": "0.40",
            "models": [
                {
                    "alias": "analysis",
                    "provider": "fixture",
                    "model": "control",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "context_tokens": 32000,
                    "output_tokens": 1800,
                    "input_usd_per_million": 1,
                    "price_date": "2026-11-11",
                    "source": "controlled test",
                    "output_usd_per_million": 1,
                    "data_scopes": ["customer_conversation"],
                }
            ],
        }
    )
    runner = worker.ScopedModelRunner(registry, Gate(), {"kind": "TEXT"})
    transports = []

    def transport(*_args):
        transports.append(True)
        return AIMessage(content="{}")

    monkeypatch.setattr(runner, "transport", transport)
    monkeypatch.setattr(worker, "_attempt_binding", lambda *_: {"native": "proof"})

    def invalid(_tools, _raw):
        raise InvalidOutput("TOOL_ARGUMENTS_INVALID", "tool_calls.arguments")

    def run(phase, validate):
        return runner.call(
            Audit(),
            phase,
            preferred="analysis",
            messages=[HumanMessage(content="fixture")],
            tools=[],
            validate=validate,
            fixed=True,
            data_scope="customer_conversation",
            output_reserve=1800,
        )

    with pytest.raises(ModelFailure, match="^INVALID_MODEL_OUTPUT$"):
        run("first-round", invalid)
    assert run("distinct-round", lambda *_: {"result": "ok"}) == {"result": "ok"}
    assert len(transports) == 2
    assert events == [
        ("STARTED", "first-round", 1),
        ("FAILED", "first-round", 1, "INVALID_MODEL_OUTPUT"),
        ("STARTED", "distinct-round", 1),
        ("SUCCEEDED", "distinct-round", 1, None),
    ]


def test_v2_requires_observed_native_route_and_frozen_order_scope(monkeypatch):
    from aftercare.investigation.protocol import HUMAN_PAYMENT_V7

    plan = {
        "protocol": worker.SCOPED_PROTOCOL,
        "case_id": "case",
        "conversation_id": "chat",
        "issue_id": "issue",
        "expected_route": "MULTI",
        "case_scope": {
            "order_id": "DEV-ORDER-04",
            "customer_id": "C001",
            "line_ids": ["L01", "L02"],
            "order_version": 1,
        },
    }
    native = SimpleNamespace(
        case_id="case",
        conversation_id="chat",
        issue_id="issue",
        order_id="DEV-ORDER-04",
        customer_id="C001",
        line_ids=["L01", "L02"],
        business_versions={"order": 1},
        model_dump=lambda **_: {"scope": "native"},
    )
    binding = {
        "scope": {"scope": "native"},
        "decision_turn_id": "turn",
        "investigation_arm": "auto",
    }
    run = SimpleNamespace(id="run", owner_id="owner", case_id="case", cycle=0, status="RUNNING")
    request = SimpleNamespace(
        case_id="case",
        conversation_id="chat",
        issue_id="issue",
        actor_id="owner",
        state="PENDING",
        scope=binding["scope"],
    )
    route = {
        "selected_path": "MULTI",
        "arm": "auto",
        "prompt_version": HUMAN_PAYMENT_V7.prompt_version,
        "evidence_projection": HUMAN_PAYMENT_V7.evidence_projection,
    }
    db = SimpleNamespace(
        get_one=lambda table, _: run if table is worker.AgentRun else request,
        scalars=lambda _: [route],
    )
    monkeypatch.setattr(
        worker, "_scope", lambda *_args, **_kw: SimpleNamespace(id="turn", actor_id="owner")
    )
    monkeypatch.setattr(worker, "load_binding", lambda *_: binding)
    monkeypatch.setattr(worker, "DecisionScope", SimpleNamespace(model_validate=lambda _: native))
    monkeypatch.setattr(worker, "protocol_for_binding", lambda *_: HUMAN_PAYMENT_V7)
    monkeypatch.setattr(
        worker,
        "build_input",
        lambda *_args, **_kw: SimpleNamespace(
            relevant_input_sha256="a" * 64,
            policy_context={"catalogue_sha256": "b" * 64},
            role_view=lambda _: {"available_evidence": []},
        ),
    )
    audit = SimpleNamespace(run_id="run", cycle=0)
    assert worker._text_scope(db, plan, audit)["run_id"] == "run"
    expanded = {**plan, "protocol": worker.EXPANDED_PROTOCOL}
    with pytest.raises(ValueError, match="NATIVE_OUTPUT_RESERVE_REJECTED"):
        worker._text_scope(db, expanded, audit)
    binding["investigation_output_reserve"] = 4096
    assert worker._text_scope(db, expanded, audit)["native_output_tokens"] == 4096
    with pytest.raises(ValueError, match="NATIVE_OUTPUT_RESERVE_REJECTED"):
        worker._text_scope(db, plan, audit)
    del binding["investigation_output_reserve"]
    route["selected_path"] = "SINGLE"
    with pytest.raises(ValueError, match="NATIVE_ROUTE_REJECTED"):
        worker._text_scope(db, plan, audit)
    route["selected_path"] = "MULTI"
    native.line_ids = ["L02"]
    with pytest.raises(ValueError, match="NATIVE_ROUTE_REJECTED"):
        worker._text_scope(db, plan, audit)


def test_v2_rejects_unplanned_escalation_before_native_reservation():
    class Audit:
        def begin_attempt(self, *_args, **_kwargs):
            pytest.fail("unplanned phase reserved a native attempt")

    gate = SimpleNamespace(
        policy=SimpleNamespace(admitted_logical_phase=lambda _plan, _logical: False)
    )
    with ExitStack() as stack:
        audit = worker._GateAudit(Audit(), gate, {"protocol": worker.SCOPED_PROTOCOL}, stack)
        with pytest.raises(ModelFailure, match="UNPLANNED_NATIVE_PHASE"):
            audit.begin_attempt("p4:compact-v7:fact:0", 1, None, Decimal("0.01"), None)
