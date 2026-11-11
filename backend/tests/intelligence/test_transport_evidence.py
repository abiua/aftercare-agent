from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from aftercare.business.rules import digest
from aftercare.db import User
from aftercare.evaluation.runner import EvaluationRunner, _model_attempts
from aftercare.evaluation.service import _live_model_snapshot
from aftercare.intelligence import models
from aftercare.intelligence.models import ModelFailure, ModelRunner, ModelSpec, Registry
from aftercare.intelligence.service import QueueFenceLost, RunAudit
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from openai import APIStatusError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'transport.db'}")
    for table in (User, AgentRun, RunEvent, ModelAttempt):
        table.__table__.create(engine)
    stamp = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.add(
            User(
                id="support",
                token_hash="fixture",
                role="support",
                display_name="Fixture",
                enabled=True,
            )
        )
        run = AgentRun(
            id="run",
            thread_id="thread",
            case_id="case",
            owner_id="owner",
            actor={"user_id": "support", "role": "support"},
            status="RUNNING",
            mode="single",
            strategy="economy",
            message="fixture",
            case_version=1,
            created_at=stamp,
            updated_at=stamp,
            deadline=stamp + timedelta(seconds=60),
        )
        session.add(run)
    monkeypatch.setenv("AFTERCARE_TEST_TRANSPORT_KEY", "local-fixture-only")
    yield engine, RunAudit(engine, run)
    engine.dispose()


def _registry(*, streaming=False):
    return Registry(
        version="fixture-version",
        allow_unverified=True,
        max_attempts=1,
        native_streaming=streaming,
        models=[
            ModelSpec(
                alias="economy",
                provider="fixture",
                model="fixture-model",
                endpoint="https://example.invalid",
                api_key_env="AFTERCARE_TEST_TRANSPORT_KEY",
                input_usd_per_million=1,
                output_usd_per_million=1,
                price_date=date(2026, 1, 1),
                source="controlled fixture",
                streaming=streaming,
                streaming_checked_at=date(2026, 1, 1),
                streaming_compatibility_version="fixture",
            )
        ],
    )


def _call(runner, audit, *, validate=None):
    return runner.call(
        audit,
        "logical",
        preferred="economy",
        messages=[HumanMessage(content="fixture")],
        tools=[],
        validate=validate or (lambda _calls, text: {"answer": text}),
        fixed=True,
    )


def _result(engine):
    with Session(engine) as session:
        row = session.scalars(select(ModelAttempt)).one()
        attempts = [
            item.payload
            for item in session.scalars(
                select(RunEvent).where(RunEvent.kind == "attempt").order_by(RunEvent.seq)
            )
        ]
        assert attempts[0]["transport_evidence"]["response"] == "UNOBSERVED"
        assert attempts[-1]["transport_evidence"] == row.metadata_json["transport_evidence"]
        assert row.metadata_json["run_id"] == row.run_id
        return row.status, row.error_code, row.metadata_json["transport_evidence"], row.usage


@pytest.mark.parametrize("outcome", ["valid", "invalid", "timeout", "http-error"])
def test_default_sync_records_observation_before_business_validation(ledger, monkeypatch, outcome):
    engine, audit = ledger
    usage = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}

    def invoke(_):
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "http-error":
            response = httpx.Response(401, request=httpx.Request("POST", "https://example.invalid"))
            raise APIStatusError("fixture", response=response, body=None)
        return AIMessage(content="reply", usage_metadata=usage)

    def validate(_calls, text):
        if outcome == "invalid":
            raise models.InvalidOutput("FIXTURE_INVALID")
        return {"answer": text}

    monkeypatch.setattr(models, "_client", lambda *_: SimpleNamespace(invoke=invoke))
    runner = ModelRunner(_registry())
    if outcome == "valid":
        assert _call(runner, audit, validate=validate) == {"answer": "reply"}
    else:
        with pytest.raises(ModelFailure):
            _call(runner, audit, validate=validate)
    status, _, marker, stored_usage = _result(engine)
    assert marker["path"] == "DEFAULT_SYNC"
    assert (
        status
        == {
            "valid": "SUCCEEDED",
            "invalid": "FAILED",
            "timeout": "UNKNOWN",
            "http-error": "FAILED",
        }[outcome]
    )
    assert (
        marker["response"]
        == {
            "valid": "SDK_RETURNED",
            "invalid": "SDK_RETURNED",
            "timeout": "UNOBSERVED",
            "http-error": "HTTP_ERROR_RESPONSE",
        }[outcome]
    )
    assert marker.get("http_status") == (401 if outcome == "http-error" else None)
    assert stored_usage == (usage if outcome in {"valid", "invalid"} else None)


@pytest.mark.parametrize("terminal", [True, False])
def test_default_stream_distinguishes_terminal_response_from_partial(ledger, monkeypatch, terminal):
    engine, audit = ledger
    chunk = AIMessageChunk(
        content="reply", response_metadata={"finish_reason": "stop"} if terminal else {}
    )
    monkeypatch.setattr(
        models,
        "_client",
        lambda *_: SimpleNamespace(stream=lambda *_args, **_kwargs: iter([chunk])),
    )
    runner = ModelRunner(_registry(streaming=True))
    if terminal:
        assert _call(runner, audit) == {"answer": "reply"}
    else:
        with pytest.raises(ModelFailure):
            _call(runner, audit)
    status, _, marker, _ = _result(engine)
    assert status == ("SUCCEEDED" if terminal else "UNKNOWN")
    assert marker["path"] == "DEFAULT_STREAM"
    assert marker["response"] == ("SDK_RETURNED" if terminal else "UNOBSERVED")


def test_evaluation_clone_preserves_injected_stream_and_sync(ledger):
    engine, audit = ledger
    injected_sync = lambda *_: pytest.fail("The selected path is streaming")

    def injected_stream(*_):
        return AIMessage(content="fixture reply")

    model = ModelRunner(
        _registry(streaming=True), transport=injected_sync, stream_transport=injected_stream
    )
    evaluation = SimpleNamespace(model=model, settings=SimpleNamespace(model_enabled=True))
    clone = EvaluationRunner._model(evaluation, {"source": "live"}, {"budget_usd": "1", "calls": 1})
    assert clone.transport is injected_sync and clone.stream_transport is injected_stream
    assert clone.base_registry_digest == model.base_registry_digest
    assert clone.base_registry_digest == _live_model_snapshot(model)["registry_digest"]
    assert clone.base_registry_digest != digest(clone.registry.model_dump(mode="json"))
    assert _call(clone, audit) == {"answer": "fixture reply"}
    assert _result(engine)[2]["path"] == "INJECTED_STREAM"
    assert _result(engine)[2]["response"] == "UNOBSERVED"
    assert _model_attempts(engine, "run")[0]["base_registry_digest"] == model.base_registry_digest


def test_base_registry_digest_binds_full_original_registry_without_mutable_aliases():
    original = _registry()
    runner = ModelRunner(original)
    registered = runner.registry
    changed = registered.model_copy(deep=True)
    changed.models[0].endpoint = "https://another.example.invalid"
    assert (
        changed.version == registered.version
        and changed.models[0].model == registered.models[0].model
    )
    assert ModelRunner(changed).base_registry_digest != runner.base_registry_digest
    original.models[0].endpoint = changed.models[0].endpoint
    registered.models[0].output_usd_per_million = Decimal(99)
    assert runner.registry.models[0].endpoint == "https://example.invalid"
    assert runner.registry.models[0].output_usd_per_million == Decimal(1)
    assert runner.base_registry_digest == digest(runner.registry.model_dump(mode="json"))
    with pytest.raises(AttributeError):
        runner.base_registry_digest = "a" * 64
    with pytest.raises(TypeError):
        ModelRunner(original, base_registry_digest="a" * 64)
    with pytest.raises(TypeError):
        runner.with_evaluation_limits(calls=1, budget=Decimal(1), endpoint="not-a-limit")


def test_legacy_attempt_finish_does_not_backfill_registry_digest(ledger):
    engine, audit = ledger
    registry = _registry()
    audit.begin_attempt("logical", 1, registry.models[0], Decimal("0.01"), registry)
    audit.finish_attempt("logical", 1, "FAILED", "LEGACY_FAILURE", None, None, None)
    assert "base_registry_digest" not in _model_attempts(engine, "run")[0]


def test_late_default_response_after_cancel_updates_attempt_only(ledger, monkeypatch):
    engine, audit = ledger

    def invoke(_):
        with Session(engine) as session, session.begin():
            run = session.get(AgentRun, "run")
            run.status, run.reason = "CANCELED", "USER_CANCELED"
        return AIMessage(
            content="reply",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(models, "_client", lambda *_: SimpleNamespace(invoke=invoke))
    with pytest.raises(ModelFailure, match="RUN_INACTIVE"):
        _call(ModelRunner(_registry()), audit)
    status, _, marker, usage = _result(engine)
    assert status == "SUCCEEDED" and marker["response"] == "SDK_RETURNED"
    assert usage["total_tokens"] == 2
    with Session(engine) as session:
        run = session.get(AgentRun, "run")
        assert (run.status, run.reason, run.output) == ("CANCELED", "USER_CANCELED", None)


def test_late_default_response_after_lease_loss_does_not_emit_attempt_event(ledger, monkeypatch):
    engine, audit = ledger
    owned = True
    audit.queue_admission = lambda _: owned

    def invoke(_):
        nonlocal owned
        owned = False
        return AIMessage(
            content="reply",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(models, "_client", lambda *_: SimpleNamespace(invoke=invoke))
    with pytest.raises(QueueFenceLost):
        _call(ModelRunner(_registry()), audit)
    with Session(engine) as session:
        attempt = session.scalars(select(ModelAttempt)).one()
        assert attempt.status == "SUCCEEDED" and attempt.usage["total_tokens"] == 2
        assert attempt.metadata_json["transport_evidence"]["response"] == "SDK_RETURNED"
        events = list(session.scalars(select(RunEvent).where(RunEvent.kind == "attempt")))
        assert (
            len(events) == 1 and events[0].payload["transport_evidence"]["response"] == "UNOBSERVED"
        )


@pytest.mark.parametrize(
    ("calls", "budget"),
    [
        (0, Decimal(1)),
        (True, Decimal(1)),
        (145, Decimal(1)),
        (1, Decimal(0)),
        (1, Decimal("NaN")),
        (1, Decimal("Infinity")),
        (1, 1.0),
    ],
)
def test_evaluation_clone_refuses_unallocated_or_invalid_limits(calls, budget):
    with pytest.raises(ValueError):
        ModelRunner(_registry()).with_evaluation_limits(calls=calls, budget=budget)
