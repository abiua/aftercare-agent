"""Internal-only SQLite controls for the formal state machine. No external calls."""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_formal_package import _open, _put, _synthetic

from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.business.rules import now
from aftercare.db import Base, User
from aftercare.evaluation.formal import execution
from aftercare.evaluation.formal.contracts import (
    ConditionControls,
    FormalFreezeDraftV2,
    FreezeQuota,
)
from aftercare.evaluation.formal.execution import FormalEvaluationService
from aftercare.evaluation.formal.files import json_bytes
from aftercare.evaluation.formal.native import project_runtime_controls
from aftercare.evaluation.gates import GatePolicy
from aftercare.evaluation.retrieval_evidence import catalogue_sha256, fixture_policies
from aftercare.evaluation.runner import _model_attempts
from aftercare.evaluation.service import EvaluationService
from aftercare.evaluation.tables import EvaluationResult, Experiment
from aftercare.intelligence.models import ModelFailure, ModelRunner, ModelSpec, Registry
from aftercare.intelligence.service import RunAudit
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

ORDER = ["single/analysis", "multi/routed", "single/routed", "single/economy"]
FREEZE = "a" * 64
CREATOR = UserContext(user_id="tester-a", role="tester")
NEXT = UserContext(user_id="tester-b", role="tester")
TOKENS = {actor.user_id: token_hash(actor.user_id * 10) for actor in (CREATOR, NEXT)}


class LocalExecution(FormalEvaluationService):
    def _guard_target(self):
        assert self.engine.dialect.name == "sqlite" and self.settings.profile == "test"

    def _runner(self, package):
        return self.runner


class Runner:
    def __init__(self, isolated, control=None):
        self.isolated = isolated
        self.control = control
        self.calls = []

    def recover_audit(self, _result_id):
        return {
            "model_attempts": [{"status": "UNKNOWN", "estimated_cost_usd": None, "usage": None}],
            "model_audit_complete": False,
        }, [{"kind": "attempt", "payload": {"status": "STARTED"}}]

    def run(self, result_id, case, _expected, config, quota, *, cancelled, attempt_admission, **_):
        assert not cancelled()
        assert quota["calls"] == 3 and quota["budget_usd"] == "0.01"
        self.calls.append((result_id, config["mode"] + "/" + config["strategy"], case["id"]))
        if self.control is not None:
            return self.control(result_id, attempt_admission)
        with Session(self.isolated) as db:
            assert attempt_admission(db)
        return outcome([])


def outcome(attempts):
    return {
        "status": "NEEDS_REVIEW",
        "failure_reason": "HUMAN_REFERENCE_PENDING",
        "actual": {"model_audit_complete": True, "model_attempts": attempts},
        "trace": [{"kind": "synthetic_fixture"}],
        "checks": [],
    }


@pytest.fixture
def system(tmp_path):
    parent = create_engine(f"sqlite:///{tmp_path / 'parent.sqlite'}")
    isolated = create_engine(f"sqlite:///{tmp_path / 'case.sqlite'}")
    Base.metadata.create_all(parent)
    Base.metadata.create_all(isolated)
    with Session(parent) as db, db.begin():
        for actor in (CREATOR, NEXT):
            db.add(
                User(
                    id=actor.user_id,
                    display_name=actor.user_id,
                    role="tester",
                    token_hash=TOKENS[actor.user_id],
                    enabled=True,
                )
            )
    settings = SimpleNamespace(profile="test", model_enabled=True, embedding_enabled=False)
    service = LocalExecution(parent, settings, None, model=fixture_model())
    service.runner = Runner(isolated)
    run_args, cases = fixture_arguments(tmp_path, service)
    yield SimpleNamespace(
        service=service, args=run_args, parent=parent, isolated=isolated, cases=cases
    )
    isolated.dispose()
    parent.dispose()


def fixture_model():
    return ModelRunner(
        Registry(
            version="fixture-1",
            native_streaming=True,
            allow_unverified=True,
            models=[
                ModelSpec(
                    alias="economy",
                    provider="fixture",
                    model="synthetic",
                    endpoint="https://example.invalid",
                    api_key_env="FORMAL_TEST_NO_KEY",
                    streaming=True,
                    streaming_usage=True,
                    streaming_checked_at="2026-11-10",
                    streaming_compatibility_version="fixture-1",
                    price_date="2026-11-10",
                    source="fixture",
                    input_usd_per_million=Decimal(1),
                    output_usd_per_million=Decimal(1),
                )
            ],
        )
    )


def fixture_arguments(tmp_path, service):
    package_path = tmp_path / "private"
    package_path.mkdir(mode=0o700)
    cases, refs = _synthetic()
    manifest = _put(package_path, cases, refs)
    package = _open(package_path, manifest)
    policy_digests = {
        row["id"]: catalogue_sha256(fixture_policies(row["fixture"])) for row in cases
    }
    runtime = service._runtime(policy_digests)
    controls = {}
    for key in ORDER:
        mode, strategy = key.split("/")
        projection = project_runtime_controls(
            {**runtime, "mode": mode, "strategy": strategy, "source": "live", "probe_intent": True}
        )
        assert projection
        controls[key] = ConditionControls(
            validation_experiment_id=uuid4(),
            config_sha256="0" * 64,
            controls=projection,
            controls_sha256=sha256(json_bytes(projection.model_dump(mode="json"))).hexdigest(),
            gate_policy=None,
        )
    gate = GatePolicy(
        id="fixture",
        version="1",
        name="fixture",
        source="isolated synthetic",
        thresholds={"min_task_success": 1, "min_independent_cases": 1},
    )
    cap = FreezeQuota(calls=72, budget_microusd=240_000)
    draft = FormalFreezeDraftV2(
        schema_version="ac-formal-freeze-draft-v2",
        candidate_sha256="b" * 64,
        manifest_sha256=manifest,
        validation_sha256="c" * 64,
        validation_plan_sha256="d" * 64,
        selected_validation_experiment_id=controls[ORDER[0]].validation_experiment_id,
        gate_policy=gate,
        group_b_strategy="routed",
        block_order=ORDER,
        conditions={
            key: {
                "validation_experiment_id": control.validation_experiment_id,
                "controls_sha256": control.controls_sha256,
                "quota": {"calls": 18, "budget_microusd": 60_000},
            }
            for key, control in controls.items()
        },
        row_order="REPETITION_THEN_CASE_ID_ASC",
        repetitions=2,
        case_count=3,
        row_quota={"calls": 3, "budget_microusd": 10_000},
        campaign_quota=cap,
    )
    run_args = {
        "freeze_sha256": FREEZE,
        "draft": draft,
        "controls": controls,
        "package": package,
        "execution_cap": cap,
    }
    return run_args, cases


def run(system, actor=CREATOR, **changes):
    return system.service.run(
        actor, credential_token_hash=TOKENS[actor.user_id], **{**system.args, **changes}
    )


def report(system, actor=CREATOR):
    return system.service.private_report(
        actor,
        freeze_sha256=FREEZE,
        draft=system.args["draft"],
        credential_token_hash=TOKENS[actor.user_id],
    )


def test_fixed_block_and_case_order_stable_binding_and_private_report(system, monkeypatch):
    db_order = EvaluationService._results
    monkeypatch.setattr(
        EvaluationService,
        "_results",
        staticmethod(lambda db, ident: list(reversed(db_order(db, ident)))),
    )
    first = run(system)
    assert first["status"] == "COMPLETED" and first["formal_freeze_sha256"] == FREEZE
    assert [row["condition_key"] for row in first["conditions"]] == ORDER
    assert all(row["counts"]["started"] == 6 for row in first["conditions"])
    assert all(row["accounting_complete"] for row in first["conditions"])
    case_ids = sorted(row["id"] for row in system.cases)
    assert [(key, case) for _, key, case in system.service.runner.calls] == [
        (key, case_id) for key in ORDER for case_id in case_ids * 2
    ]
    assert run(system, NEXT) == first
    assert len(system.service.runner.calls) == 24
    with Session(system.parent) as db:
        assert set(db.scalars(select(Experiment.created_by))) == {CREATOR.user_id}
    assert EvaluationService(system.parent, system.service.settings).experiments(CREATOR) == []
    private = report(system, NEXT)
    assert [block["id"] for block in private["conditions"]] == [
        block["experiment_id"] for block in first["conditions"]
    ]
    row = next(item for item in private["conditions"][0]["results"] if item["repetition"] == 1)
    assert row["trace"] == [{"kind": "synthetic_fixture"}] and row["reviews"] == []
    assert private["conditions"][0]["gate"]["status"] != "PASS"


@pytest.mark.parametrize("unknown_status", ["STARTED", "UNKNOWN", "FAILED"])
def test_raw_unknown_blocks_same_case_retry_and_all_later_work(system, unknown_status):
    def control(result_id, admit):
        with Session(system.isolated) as db, db.begin():
            assert admit(db)
            db.add(
                ModelAttempt(
                    run_id=str(uuid4()),
                    cycle=0,
                    logical_id=result_id,
                    ordinal=1,
                    status=unknown_status,
                    started_at=now(),
                    metadata_json={},
                )
            )
        with Session(system.isolated) as db:
            assert not admit(db)
        return outcome([{"status": unknown_status, "estimated_cost_usd": None, "usage": None}])

    system.service.runner.control = control
    result = run(system)
    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert len(system.service.runner.calls) == 1
    block = result["conditions"][0]
    assert block["model_calls"] == 1 and block["unknown_case_count"] == 1
    assert block["reserved_budget_microusd"] == 10_000
    assert [item["counts"]["not_run"] for item in result["conditions"]] == [5, 6, 6, 6]
    assert run(system, NEXT) == result


def test_recovered_running_result_is_not_replayed(system, monkeypatch):
    control = system.service.runner.run

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(system.service.runner, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(system)
    with Session(system.parent) as db:
        result = db.scalar(select(EvaluationResult).where(EvaluationResult.status == "RUNNING"))
        assert result is not None
        old_id = result.id
    monkeypatch.setattr(system.service.runner, "run", control)
    summary = run(system, NEXT)
    assert summary["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert system.service.runner.calls == []
    row = report(system)["conditions"][0]["results"][0]
    assert row["id"] == old_id
    assert row["machine_status"] == "ERROR" and row["failure_reason"] == "WORKER_INTERRUPTED"
    assert row["actual"]["model_attempts"][0]["status"] == "UNKNOWN"


def test_revoked_credential_stops_current_attempt_and_allows_different_actor(system):
    def revoke(_result_id, admit):
        with Session(system.isolated) as db:
            assert admit(db)
        with Session(system.parent) as db, db.begin():
            db.get_one(User, CREATOR.user_id).token_hash = "0" * 64
        with Session(system.isolated) as db:
            assert not admit(db)
        return outcome([])

    system.service.runner.control = revoke
    summary = run(system)
    assert summary["reason"] == "FORMAL_EXECUTION_ACTOR_REVOKED"
    with pytest.raises(BusinessError) as error:
        report(system)
    assert error.value.status == 403
    system.service.runner.control = None
    resumed = run(system, NEXT)
    assert resumed["status"] == "COMPLETED"
    assert len(system.service.runner.calls) == 24
    with Session(system.parent) as db:
        assert set(db.scalars(select(Experiment.created_by))) == {CREATOR.user_id}


def test_cap_and_runtime_drift_fail_before_any_write(system):
    with pytest.raises(BusinessError, match="封顶") as error:
        run(system, execution_cap=FreezeQuota(calls=1, budget_microusd=100))
    assert error.value.code == "FORMAL_BUDGET_CAP_EXCEEDED"
    system.service.model = ModelRunner(
        system.service.model.registry.model_copy(update={"version": "changed"})
    )
    with pytest.raises(BusinessError) as error:
        run(system)
    assert error.value.code == "FORMAL_RUNTIME_CHANGED"
    with Session(system.parent) as db:
        assert db.scalar(select(Experiment.id)) is None


def test_campaign_lease_loss_preserves_running_result_without_replaying(system, monkeypatch):
    active = True

    @contextmanager
    def lease(_engine, _freeze):
        nonlocal active
        active = True
        yield lambda: active

    def interrupt(_result_id, admit):
        nonlocal active
        with Session(system.isolated) as db:
            assert admit(db)
            active = False
            assert not admit(db)
        return outcome([])

    monkeypatch.setattr(execution, "_campaign_lease", lease)
    system.service.runner.control = interrupt
    first = run(system)
    assert first["reason"] == "FORMAL_EXECUTION_LEASE_LOST"
    assert report(system)["conditions"][0]["results"][0]["machine_status"] == "RUNNING"
    assert first["conditions"][0]["reserved_budget_microusd"] == 10_000
    second = run(system, NEXT)
    assert second["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert len(system.service.runner.calls) == 1
    assert report(system)["conditions"][0]["results"][0]["machine_status"] == "ERROR"


def test_over_quota_is_not_mislabeled_as_unknown_cost(system):
    def over_quota(_result_id, admit):
        with Session(system.isolated) as db:
            assert admit(db)
        return outcome([{"status": "SUCCEEDED", "estimated_cost_usd": "0.02", "usage": {}}])

    system.service.runner.control = over_quota
    result = run(system)
    assert result["reason"] == "FORMAL_ROW_QUOTA_EXCEEDED"
    first = result["conditions"][0]
    assert first["accounting_complete"] is True and first["quota_breach_case_count"] == 1
    assert first["known_cost_usd"] == "0.02" and first["reserved_budget_microusd"] == 0
    assert len(system.service.runner.calls) == 1


@pytest.mark.parametrize("fault", [None, "revoked", "lease", "unknown"])
def test_actual_modelrunner_stream_allows_own_started_but_fences_later_work(
    system, monkeypatch, fault
):
    stream_calls = []
    admitted = True
    statuses = []
    usage = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}

    @contextmanager
    def lease(_engine, _freeze):
        yield lambda: admitted

    class Client:
        def stream(self, *_args, **_kwargs):
            nonlocal admitted
            stream_calls.append("controlled stream")
            yield AIMessageChunk(content="a" * 160)
            if fault == "unknown":
                return
            if fault == "revoked":
                with Session(system.parent) as db, db.begin():
                    db.get_one(User, CREATOR.user_id).enabled = False
            if fault == "lease":
                admitted = False
            yield AIMessageChunk(content="b" * 160)
            yield AIMessageChunk(
                content="", response_metadata={"finish_reason": "stop"}, usage_metadata=usage
            )

    monkeypatch.setattr(execution, "_campaign_lease", lease)
    monkeypatch.setattr("aftercare.intelligence.models._client", lambda *_: Client())

    class StreamRunner(Runner):
        def run(
            self,
            result_id,
            case,
            _expected,
            config,
            quota,
            *,
            attempt_admission,
            attempt_continuation,
            **_,
        ):
            self.calls.append(result_id)
            if len(self.calls) > 1:
                return outcome([])
            with Session(self.isolated, expire_on_commit=False) as db, db.begin():
                db.add(
                    User(
                        id="support",
                        role="support",
                        enabled=True,
                        display_name="fixture",
                        token_hash="support",
                    )
                )
                stamp = now()
                run = AgentRun(
                    id=str(uuid4()),
                    thread_id=result_id,
                    case_id=case["id"],
                    owner_id="owner",
                    actor={"user_id": "support", "role": "support"},
                    status="RUNNING",
                    mode=config["mode"],
                    strategy=config["strategy"],
                    message=case["message"],
                    case_version=1,
                    cycle=0,
                    event_seq=0,
                    deadline=stamp + timedelta(minutes=1),
                    created_at=stamp,
                    updated_at=stamp,
                )
                db.add(run)
            audit = RunAudit(
                self.isolated, run, attempt_admission, attempt_continuation=attempt_continuation
            )
            model = system.service.model.with_evaluation_limits(
                calls=quota["calls"], budget=Decimal(quota["budget_usd"])
            )

            def call(logical_id):
                return model.call(
                    audit,
                    logical_id,
                    preferred="economy",
                    messages=[HumanMessage(content="fixture")],
                    tools=[],
                    fixed=True,
                    validate=lambda _calls, content: {"notes": content},
                )

            if fault:
                with pytest.raises(ModelFailure):
                    call("first")
                with pytest.raises(ModelFailure):
                    call("second")
            else:
                assert call("first")["notes"] == "a" * 160 + "b" * 160
            attempts = _model_attempts(self.isolated, run.id)
            statuses.extend(item["status"] for item in attempts)
            with Session(self.isolated) as db:
                deltas = [
                    item.payload["text"]
                    for item in db.scalars(
                        select(RunEvent)
                        .where(RunEvent.kind == "model_draft_delta")
                        .order_by(RunEvent.seq)
                    )
                ]
            assert deltas == (["a" * 160] if fault else ["a" * 160, "b" * 160])
            return outcome(attempts)

    system.service.runner = StreamRunner(system.isolated)
    summary = run(system)
    assert stream_calls == ["controlled stream"]
    assert statuses == (["UNKNOWN"] if fault else ["SUCCEEDED"])
    assert summary["status"] == ("INCOMPLETE" if fault else "COMPLETED")
    if fault:
        assert len(system.service.runner.calls) == 1
    else:
        assert summary["conditions"][0]["known_cost_usd"] == "0.000006"


@pytest.mark.parametrize(
    "mismatch",
    [None, "identity", "registry", "model", "response", "usage", "state", "cost"],
)
def test_settled_interrupted_audit_only_authorizes_later_rows(system, monkeypatch, mismatch):
    run_id = str(uuid4())
    identity = {"run_id": run_id, "cycle": 0, "logical_id": "initial", "ordinal": 1}
    frozen = {**identity, "status": "UNKNOWN", "estimated_cost_usd": None, "usage": None}
    stamp = now()
    model = system.service.model.registry.models[0]
    raw = {
        **identity,
        "status": "SUCCEEDED",
        "usage": {"input_tokens": 2, "output_tokens": 1},
        "estimated_cost_usd": "0.000003",
        "finished_at": stamp,
        "metadata_json": {
            "run_id": run_id,
            "alias": model.alias,
            "provider": model.provider,
            "model": model.model,
            "base_registry_digest": system.service.model.base_registry_digest,
            "transport_evidence": {
                "version": "ac-attempt-transport-v1",
                "path": "DEFAULT_STREAM",
                "response": "SDK_RETURNED",
            },
        },
    }
    original_run = system.service.runner.run

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(system.service.runner, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(system)
    monkeypatch.setattr(system.service.runner, "run", original_run)
    monkeypatch.setattr(
        system.service.runner,
        "recover_audit",
        lambda _: ({"model_attempts": [frozen], "model_audit_complete": False}, []),
    )
    runs = [{"id": run_id, "cycle": 0, "mode": "single", "strategy": "analysis"}]
    ledger = [{**raw, "status": "STARTED", "usage": None, "estimated_cost_usd": None}]
    monkeypatch.setattr(system.service, "_read_recovery", lambda *_: (runs, ledger))
    assert run(system, NEXT)["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    before = report(system)["conditions"][0]["results"][0]
    assert before["actual"]["model_attempts"] == [frozen]
    assert system.service.runner.calls == []

    if mismatch == "identity":
        raw["ordinal"] = 2
    elif mismatch == "registry":
        raw["metadata_json"]["base_registry_digest"] = "0" * 64
    elif mismatch == "model":
        raw["metadata_json"]["model"] = "different-model"
    elif mismatch == "response":
        raw["metadata_json"]["transport_evidence"]["response"] = "UNOBSERVED"
    elif mismatch == "usage":
        raw["usage"] = {"input_tokens": 2}
    elif mismatch == "state":
        raw["status"] = "UNKNOWN"
    elif mismatch == "cost":
        raw["estimated_cost_usd"] = "0.02"
    ledger[:] = [raw]
    summary = run(system, NEXT)
    private = report(system)["conditions"][0]
    frozen_result = private["results"][0]
    assert frozen_result == before and frozen_result["machine_status"] == "ERROR"
    if mismatch:
        assert summary["status"] == "INCOMPLETE"
        assert system.service.runner.calls == [] and private["recovery_accounting"] == []
        return
    assert summary["status"] == "COMPLETED"
    assert len(system.service.runner.calls) == 23
    assert private["recovery_accounting"] == [
        {
            "result_id": frozen_result["id"],
            "status": "SETTLED_FOR_LATER_ROWS",
            "model_calls": 1,
            "known_cost_usd": "0.000003",
            "attempts": [
                {
                    **{key: value for key, value in raw.items() if key != "metadata_json"},
                    "finished_at": stamp.isoformat(),
                }
            ],
        }
    ]
