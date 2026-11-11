"""Cross-store registration using original P3 rows from controlled SQLite arms."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, TESTER, _photo
from test_chat_blind_review import BASE, _initial
from test_chat_decisions_integration import _link
from test_chat_decisions_integration import configured as _configured
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.business.service import BusinessService
from aftercare.conversations.tables import Conversation
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.api import router
from aftercare.evaluation.chat import registration
from aftercare.evaluation.chat.manifest import Manifest, bound_rows
from aftercare.evaluation.chat.registration import register_pg_blind
from aftercare.evaluation.chat.runner import ChatEvaluationRunner, FrozenCase, _SQLiteBackend
from aftercare.evaluation.chat.tables import BlindTask
from aftercare.evaluation.runner import NamespacedObjects, _namespace
from aftercare.intelligence.models import ModelRunner, Registry

client, system, configured = _client, _system, _configured


@pytest.fixture
def paired(configured, tmp_path, monkeypatch):
    env = configured
    env.client.app.include_router(router, prefix="/api")
    url, _ = _link(env)
    image = _photo("green")
    assert (
        send(env.client, url, "合成商品照片。", photos=(("original.png", image),)).status_code
        == 201
    )
    env.service.settings.chat_investigation_enabled = True
    with Session(env.db) as db:
        conversation = db.get_one(Conversation, url.split("/")[-1])

    namespace = "eval_" + "4" * 32
    scoped_objects = NamespacedObjects(env.service.storage, namespace)
    for key, content in list(env.service.storage.objects.items()):
        scoped_objects.put(key, content, "image/png")
    source = BusinessService(env.db, env.service.settings, storage=scoped_objects)
    case = FrozenCase(
        "PAIRED-CASE",
        1,
        source,
        SUPPORT,
        conversation.id,
        conversation.analysis_issue_id,
        datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=1),
        "controlled no-provider fixture",
    )
    saver = InMemorySaver()

    class OwnedFixture(_SQLiteBackend):
        campaign = "paired-blind"

        def registration_business(self):
            return env.service

        def freeze(self, definition):
            frozen = super().freeze(definition)
            frozen.record.update(source_namespace=namespace, source_sha256="a" * 64)
            return frozen

        def control_fields(self):
            return {"storage_topology": "CONTROLLED_DISTINCT_SQLITE_ARM_STORES"}

        def checkpoint(self, _row, result):
            check = saver.get_tuple({"configurable": {"thread_id": result["thread_id"]}})
            return {
                "checkpoint_persisted": check is not None,
                "checkpoint_id": check.config["configurable"]["checkpoint_id"] if check else None,
            }

    monkeypatch.setattr(registration, "_PgRuntime", OwnedFixture)
    runtime = OwnedFixture(saver)

    def no_transport(*_args, **_kwargs):
        pytest.fail("program-gated fixture reached model transport")

    registry = Registry.model_validate(
        {
            "version": "blind-registration-fixture",
            "allow_unverified": True,
            "models": [
                {
                    "alias": "analysis",
                    "provider": "fixture",
                    "model": "controlled",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "data_scopes": ["customer_conversation"],
                    "output_tokens": 1800,
                }
            ],
        }
    )
    runner = ChatEvaluationRunner(
        ModelRunner(registry, transport=no_transport), saver, calls=8, budget=Decimal("0.10")
    )
    manifest = Manifest(tmp_path / "paired")
    result = runner._run(manifest, runtime.campaign, [case], runtime)
    assert all(row["candidate_status"] == "AVAILABLE" for row in result["rows"])
    assert all(row["checkpoint_persisted"] and not row["attempts"] for row in result["rows"])
    return env, runtime, manifest, case, image, result


def _register(runtime, manifest, case):
    return register_pg_blind(
        runtime,
        manifest,
        case,
        reviewer_id=TESTER.user_id,
        case_label="PAIRED-BLIND",
    )


def test_registers_three_native_snapshots_and_resumes_private_mapping_after_commit(
    paired, monkeypatch
):
    env, runtime, manifest, case, image, result = paired
    write = manifest._write

    def interrupted(name, payload):
        if name.startswith("blind-"):
            raise OSError("controlled mapping write interruption")
        write(name, payload)

    with monkeypatch.context() as patched:
        patched.setattr(manifest, "_write", interrupted)
        with pytest.raises(OSError, match="controlled"):
            _register(runtime, manifest, case)
    with Session(env.db) as db:
        task_id = db.scalar(select(BlindTask.id))
        assert task_id and db.scalar(select(func.count()).select_from(BlindTask)) == 1
        assert not db.scalar(select(func.count()).select_from(DecisionSnapshot))
    assignment = _register(runtime, manifest, case)
    assert assignment["id"] == task_id
    assert _register(runtime, manifest, case) == assignment
    mapping = manifest.read(f"blind-{task_id}.json")
    assert [row["snapshot_id"] for row in mapping["arms"]] == [
        row["snapshot_id"] for row in result["rows"]
    ]

    endpoint = f"{BASE}/{task_id}"
    initial = env.client.get(endpoint, headers=headers(TESTER)).json()
    assert initial["status"] == "BLIND" and initial["exposure"] is None
    assert "strong_single" not in str(initial) and all(
        row["run_id"] not in str(initial) for row in mapping["arms"]
    )
    image_path = f"{endpoint}/images/{initial['input']['images'][0]['id']}"
    assert env.client.get(image_path, headers=headers(TESTER)).content == image
    assert env.client.get(image_path, headers=headers(OWNER)).status_code == 404
    seal = env.client.post(
        f"{endpoint}/initial", json=_initial(initial["input"]), headers=headers(TESTER)
    )
    assert seal.status_code == 200, seal.text
    reveal = env.client.post(
        f"{endpoint}/reveal", json={"expected_version": 2}, headers=headers(TESTER)
    )
    assert reveal.status_code == 200, reveal.text
    exposure = reveal.json()["exposure"]
    assert {row["id"] for row in exposure["candidates"]} == {
        row["blind_candidate_id"] for row in mapping["arms"]
    }
    assert not any(row["snapshot_id"] in str(exposure) for row in mapping["arms"])
    assert "option_gate" not in str(exposure)
    assert mapping["option_gate_schema_version"] == "chat-blind-option-gates-v1"
    assert mapping["option_gates_sha256"] == digest(mapping["arms"])
    with Session(env.db) as db:
        task = db.get_one(BlindTask, task_id)
        private = {
            key: value for key, value in mapping.items() if key not in {"task_id", "source_sha256"}
        }
        assert (
            task.source_sha256
            == mapping["source_sha256"]
            == digest(
                [
                    "chat-blind-pg-registration-v1",
                    private,
                    mapping["scope_sha256"],
                    task.input_sha256,
                    task.image_objects,
                ]
            )
        )
    by_candidate = {row["id"]: row for row in exposure["candidates"]}
    for arm, native in zip(mapping["arms"], result["rows"], strict=True):
        gate = arm["option_gate"]
        assert gate["snapshot_sha256"] == arm["snapshot_sha256"]
        assert gate["checks_sha256"] == digest(native["program_checks"])
        options = by_candidate[arm["blind_candidate_id"]]["options"]
        assert [row["option_id"] for row in gate["options"]] == [row["id"] for row in options]
        assert {row["native_option_id"]: row["status"] for row in gate["options"]} == {
            option_id: check["status"] for option_id, check in native["program_checks"].items()
        }


def test_existing_legacy_registration_map_is_never_backfilled(paired):
    env, runtime, manifest, case, _, _ = paired
    assignment = _register(runtime, manifest, case)
    path = manifest.directory / f"blind-{assignment['id']}.json"
    mapping = manifest.read(path.name)
    legacy = {
        key: value
        for key, value in mapping.items()
        if key not in {"option_gate_schema_version", "option_gates_sha256"}
    }
    legacy["arms"] = [
        {key: value for key, value in row.items() if key != "option_gate"}
        for row in mapping["arms"]
    ]
    private = {
        key: value for key, value in legacy.items() if key not in {"task_id", "source_sha256"}
    }
    with Session(env.db) as db, db.begin():
        task = db.get_one(BlindTask, assignment["id"])
        task.source_sha256 = digest(
            [
                "chat-blind-pg-registration-v1",
                private,
                private["scope_sha256"],
                task.input_sha256,
                task.image_objects,
            ]
        )
        legacy["source_sha256"] = task.source_sha256
    path.unlink()
    manifest._write(path.name, legacy)
    old_bytes = path.read_bytes()
    assert _register(runtime, manifest, case) == assignment
    assert path.read_bytes() == old_bytes


def test_partial_and_empty_plans_keep_missing_arms_private(paired):
    env, runtime, manifest, case, _, _ = paired
    plan = manifest.read("plan.json")
    rows = bound_rows(plan)
    # Simulate an interrupted receipt and a planned row without a dispatch.
    for row in rows[1:]:
        (manifest.directory / f"{row['id']}.result.json").unlink()
    for phase in ("ready", "started"):
        (manifest.directory / f"{rows[2]['id']}.{phase}.json").unlink()
    assignment = _register(runtime, manifest, case)
    mapping = manifest.read(f"blind-{assignment['id']}.json")
    assert [row["status"] for row in mapping["arms"][1:]] == ["UNKNOWN", "NOT_RUN"]
    assert [row["reason"] for row in mapping["arms"][1:]] == [
        "STARTED_WITHOUT_RESULT",
        "PLANNED_NOT_STARTED",
    ]
    assert all(row["blind_candidate_id"] is None for row in mapping["arms"][1:])
    with Session(env.db) as db:
        assert len(db.get_one(BlindTask, assignment["id"]).candidates) == 1

    (manifest.directory / f"{rows[0]['id']}.result.json").unlink()
    outcome = _register(runtime, manifest, case)
    assert outcome["status"] == "NO_REVIEWABLE_CANDIDATE"
    assert manifest.read(f"blind-unavailable-{digest(outcome)}.json") == outcome
    with Session(env.db) as db:
        assert db.scalar(select(func.count()).select_from(BlindTask)) == 1


def test_native_candidate_tampering_fails_before_assignment(paired):
    env, runtime, manifest, case, _, result = paired
    row = result["rows"][1]
    engine = create_engine(f"sqlite:///{manifest.directory / (_namespace(row['id']) + '.sqlite')}")
    try:
        with Session(engine) as db, db.begin():
            snapshot = db.get_one(DecisionSnapshot, row["snapshot_id"])
            snapshot.candidate = {**snapshot.candidate, "unresolved": ["tampered"]}
    finally:
        engine.dispose()
    with pytest.raises(BusinessError) as denied:
        _register(runtime, manifest, case)
    assert denied.value.code == "BLIND_REVIEW_PAIR_ARM_INVALID"
    with Session(env.db) as db:
        assert not db.scalar(select(func.count()).select_from(BlindTask))


def test_unowned_runtime_or_changed_source_image_cannot_register(paired):
    env, runtime, manifest, case, _, _ = paired
    with pytest.raises(BusinessError) as denied:
        _register(object(), manifest, case)
    assert denied.value.code == "BLIND_REVIEW_ASSIGNMENT_INVALID"
    source_prefix = case.source.storage.prefix + "conversations/"
    for key in env.service.storage.objects:
        if key.startswith(source_prefix):
            env.service.storage.objects[key] = b"changed"
    with pytest.raises(BusinessError) as denied:
        _register(runtime, manifest, case)
    assert denied.value.code == "MATERIAL_INTEGRITY_ERROR"
    with Session(env.db) as db:
        assert not db.scalar(select(func.count()).select_from(BlindTask))
