"""Formal CLI boundary checks using local synthetic records and a controlled engine."""

import json
import stat
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.db import User
from aftercare.evaluation.formal import __main__ as cli
from aftercare.evaluation.formal import orchestrator
from aftercare.evaluation.formal.contracts import Candidate, FreezeQuota
from aftercare.evaluation.formal.register import CampaignRegister

CAMPAIGN = "synthetic"
FREEZE = "a" * 64
EXPERIMENT = str(UUID(int=8))
PRIVATE = "fixture-only case and reference contents"


def _never(*args, **kwargs):
    pytest.fail("Closed or read-only CLI must not reach this operation")


def _cli(monkeypatch, register, *args):
    monkeypatch.setattr(
        sys, "argv", ["formal", "--root", str(register.root), "run", "--campaign", CAMPAIGN, *args]
    )
    return cli.main()


@pytest.fixture
def register(tmp_path):
    store = CampaignRegister(tmp_path.resolve() / "register")
    store.register(CAMPAIGN, Candidate(
        purpose="formal", dataset_id="synthetic-private", manifest_sha256="c" * 64,
        distribution=dict.fromkeys(("damaged", "wrong_item", "refund_status"), 1),
        grouping_evidence="synthetic only", exposed_before_freeze=False,
        custodian={"actor": "fixture", "at": "2026-01-01T00:00:00Z", "source": "fixture only"},
    ))
    return store


def test_default_cli_is_preflight_and_explicit_cli_preserves_closed_gate(register, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "_deployment", _never)
    monkeypatch.setattr(orchestrator, "_runtime", _never)
    monkeypatch.setattr(orchestrator, "_execution_api", _never)
    monkeypatch.setattr(register.__class__, "open_for_execution", _never, raising=False)
    assert _cli(monkeypatch, register) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["status"] == "REGISTERED" and default["execution_available"] is False
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, register, "--execute")
    assert exc.value.code == 1
    assert capsys.readouterr().err == "FORMAL_EXECUTION_UNAVAILABLE\n"
    assert not list((register.root / CAMPAIGN).glob("execution-*.json"))


def _runtime_file(root, name, value):
    path = root / ".runtime" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("change", [
    "valid", "open-permissions", "wrong-freeze", "wrong-campaign", "bool-calls",
    "string-budget", "float-budget", "symlink",
])
def test_deployment_caps_are_private_strict_and_bound_to_one_freeze(tmp_path, monkeypatch, change):
    root = tmp_path.resolve()
    monkeypatch.setattr(orchestrator, "ROOT", root)
    document = {
        "schema_version": "ac-formal-execution-deployment-v1",
        "campaign_id": CAMPAIGN, "freeze_sha256": FREEZE,
        "execution_cap": {"calls": 20, "budget_microusd": 100},
    }
    if change == "wrong-freeze":
        document["freeze_sha256"] = "b" * 64
    elif change == "wrong-campaign":
        document["campaign_id"] = "different"
    elif change == "bool-calls":
        document["execution_cap"]["calls"] = True
    elif change == "string-budget":
        document["execution_cap"]["budget_microusd"] = "100"
    elif change == "float-budget":
        document["execution_cap"]["budget_microusd"] = 100.0
    path = _runtime_file(root, "formal-execution.json", document)
    if change == "open-permissions":
        path.chmod(0o644)
    elif change == "symlink":
        target = path.with_suffix(".other")
        path.rename(target)
        path.symlink_to(target)
    if change == "valid":
        assert orchestrator._deployment(CAMPAIGN, FREEZE) == FreezeQuota(calls=20, budget_microusd=100)
        return
    with pytest.raises(BusinessError) as exc:
        orchestrator._deployment(CAMPAIGN, FREEZE)
    assert exc.value.code == (
        "FORMAL_EXECUTION_CAP_MISMATCH" if change.startswith("wrong-") else
        "FORMAL_EXECUTION_CAP_UNAVAILABLE"
    )


def test_current_tester_selector_authenticates_the_matching_enabled_credential(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(orchestrator, "ROOT", root)
    tokens = {"first": "x" * 32, "second": "y" * 32}
    _runtime_file(root, "users-test.json", [
        {"user_id": ident, "role": "tester", "customer_id": None, "display_name": ident, "token": token}
        for ident, token in tokens.items()
    ])
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    with Session(engine) as db, db.begin():
        for ident, token in tokens.items():
            db.add(User(id=ident, token_hash=token_hash(token), role="tester", display_name=ident))
    monkeypatch.delenv("AFTERCARE_FORMAL_TESTER_ID", raising=False)
    with pytest.raises(BusinessError):
        orchestrator._tester(engine)
    for ident, token in tokens.items():
        monkeypatch.setenv("AFTERCARE_FORMAL_TESTER_ID", ident)
        assert orchestrator._tester(engine) == (
            UserContext(user_id=ident, role="tester"), token_hash(token)
        )
    with Session(engine) as db, db.begin():
        db.get_one(User, "second").enabled = False
    with pytest.raises(BusinessError) as exc:
        orchestrator._tester(engine)
    assert exc.value.code == "FORMAL_TESTER_UNAVAILABLE"
    engine.dispose()


def _enabled_state():
    return {
        "status": "UNLOCKED", "purpose": "formal", "freeze_sha256": FREEZE,
        "source_verification": "NATIVE_VALIDATION_VERIFIED", "execution_available": True,
    }


def test_explicit_cli_pins_handoff_service_and_private_report_under_outer_lock(
    register, monkeypatch, capsys
):
    trace = []
    actor = UserContext(user_id="tester-first", role="tester")
    package, controls, draft = object(), object(), object()
    model, checkpointer = object(), object()
    cap = FreezeQuota(calls=20, budget_microusd=100)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    settings = SimpleNamespace(profile="test")
    monkeypatch.setattr(register.__class__, "preflight", lambda *_: _enabled_state())
    monkeypatch.setattr(orchestrator, "_deployment", lambda *_: cap)

    @contextmanager
    def runtime(source):
        assert source == "live"
        trace.append("runtime")
        yield settings, engine, checkpointer, model
        trace.append("runtime closed")

    @contextmanager
    def lease(candidate_engine, key):
        assert candidate_engine is engine and key == f"formal-cli:{FREEZE}"
        trace.append("lock")
        yield True
        assert len(list((register.root / CAMPAIGN).glob("execution-*.json"))) in {1, 2}
        trace.append("unlock")

    def tester(current_engine):
        assert current_engine is engine
        trace.append("tester")
        return actor, "d" * 64

    def open_for_execution(self, campaign_id, *, expected_freeze_sha256, execution_cap, operator):
        assert campaign_id == CAMPAIGN and expected_freeze_sha256 == FREEZE and execution_cap == cap
        assert operator.actor == actor.user_id
        trace.append("handoff")
        return SimpleNamespace(freeze_sha256=FREEZE, draft=draft, controls=controls, package=package)

    class Service:
        def __init__(self, actual_engine, actual_settings, actual_checkpointer, *, model):
            assert (actual_engine, actual_settings, actual_checkpointer) == (
                engine, settings, checkpointer
            )
            assert model is not None

        def run(self, user, **binding):
            assert user == actor
            assert binding == {
                "freeze_sha256": FREEZE, "draft": draft, "controls": controls, "package": package,
                "credential_token_hash": "d" * 64, "execution_cap": cap,
            }
            trace.append("service")
            return {
                "status": "INCOMPLETE", "reason": "ATTEMPT_USAGE_UNKNOWN",
                "path": "/private/omitted", "conditions": [{
                    "condition_key": "single/routed", "experiment_id": EXPERIMENT,
                    "status": "FAILED", "counts": {"failed": 1, "private_detail": PRIVATE},
                }],
            }

        def private_report(self, user, *, freeze_sha256, draft, credential_token_hash):
            assert user == actor and draft is not None and credential_token_hash == "d" * 64
            assert freeze_sha256 == FREEZE
            trace.append("private report")
            return {
                "formal_freeze_sha256": FREEZE,
                "conditions": [{
                    "id": EXPERIMENT, "formal_freeze_sha256": FREEZE,
                    "formal_condition_key": "single/routed",
                    "results": [{"actual": PRIVATE, "expected": PRIVATE}],
                }],
            }

    monkeypatch.setattr(orchestrator, "_runtime", runtime)
    monkeypatch.setattr(orchestrator, "_lease", lease)
    monkeypatch.setattr(orchestrator, "_tester", tester)
    monkeypatch.setattr(orchestrator, "_execution_api", lambda: Service)
    monkeypatch.setattr(register.__class__, "open_for_execution", open_for_execution, raising=False)
    assert _cli(monkeypatch, register, "--execute") == 1
    first = next((register.root / CAMPAIGN).glob("execution-*.json"))
    previous = first.read_bytes()
    assert _cli(monkeypatch, register, "--execute") == 1
    for line in capsys.readouterr().out.splitlines():
        assert PRIVATE not in line and "/private/" not in line and "d" * 64 not in line
        summary = json.loads(line)
        assert summary["conditions"] == [{
            "condition_key": "single/routed", "counts": {"failed": 1},
            "experiment_id": EXPERIMENT, "status": "FAILED",
        }]
    assert first.read_bytes() == previous
    artifacts = sorted((register.root / CAMPAIGN).glob("execution-*.json"))
    assert len(artifacts) == 2
    for path in artifacts:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1
        report = json.loads(path.read_bytes())
        assert report["recorded_at"].endswith("+00:00")
        assert report["formal_freeze_sha256"] == FREEZE
        condition = report["conditions"][0]["report"]
        assert condition["formal_condition_key"] == "single/routed"
        assert condition["formal_freeze_sha256"] == FREEZE
        assert condition["results"][0]["expected"] == PRIVATE
    assert trace == [
        "runtime", "lock", "tester", "handoff", "service", "private report", "unlock", "runtime closed",
    ] * 2


def test_private_report_rejects_mismatched_experiment_without_writing_an_artifact(register):
    summary = {"conditions": [{"condition_key": "single/routed", "experiment_id": EXPERIMENT}]}
    detail = {"conditions": [{
        "id": str(UUID(int=9)), "formal_condition_key": "single/routed",
        "formal_freeze_sha256": FREEZE, "results": [{"expected": PRIVATE}],
    }]}
    with pytest.raises(BusinessError) as exc:
        orchestrator._private_report(register, CAMPAIGN, FREEZE, summary, detail)
    assert exc.value.code == "FORMAL_EXECUTION_REPORT_INVALID"
    assert not list((register.root / CAMPAIGN).glob("execution-*.json"))


@pytest.mark.parametrize("guard", ["app", "busy", "revoked"])
def test_explicit_cli_stops_before_handoff_on_target_lock_or_credential_failure(
    register, monkeypatch, capsys, guard
):
    monkeypatch.setattr(register.__class__, "preflight", lambda *_: _enabled_state())
    monkeypatch.setattr(register.__class__, "open_for_execution", _never, raising=False)
    monkeypatch.setattr(orchestrator, "_deployment", lambda *_: FreezeQuota(calls=1, budget_microusd=1))
    monkeypatch.setattr(orchestrator, "_execution_api", lambda: _never)

    @contextmanager
    def runtime(_):
        yield (
            SimpleNamespace(profile="app" if guard == "app" else "test"),
            SimpleNamespace(dialect=SimpleNamespace(name="postgresql")), object(), object(),
        )

    @contextmanager
    def lease(*_):
        yield guard != "busy"

    def denied(_):
        if guard != "revoked":
            _never()
        raise BusinessError("FORMAL_TESTER_UNAVAILABLE", "/private/never-echo")

    monkeypatch.setattr(orchestrator, "_runtime", runtime)
    monkeypatch.setattr(orchestrator, "_lease", lease)
    monkeypatch.setattr(orchestrator, "_tester", denied)
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, register, "--execute")
    assert exc.value.code == 1
    code = {
        "app": "FORMAL_EXECUTION_TEST_REQUIRED", "busy": "FORMAL_EXECUTION_BUSY",
        "revoked": "FORMAL_TESTER_UNAVAILABLE",
    }[guard]
    assert capsys.readouterr().err == f"{code}\n"
