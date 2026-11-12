"""Paired execution through isolated local databases and the real worker core."""

import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial, wraps
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, create_mock_engine, delete, func, select
from sqlalchemy.orm import Session
from test_business import SUPPORT, _evidence, _photo
from test_chat_decisions_integration import _link
from test_chat_decisions_integration import configured as _configured
from test_chat_investigation_candidate import _call, _claim, _reply
from test_conversations import client as _client
from test_conversations import send
from test_conversations import system as _system

from aftercare.business.rules import digest, utc
from aftercare.business.service import BusinessService
from aftercare.conversations.tables import Conversation
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.chat.manifest import ARMS, PROTOCOL, Manifest, bound_rows
from aftercare.evaluation.chat.runner import (
    ChatEvaluationRunner,
    FrozenCase,
    _elapsed,
    _freeze_case,
)
from aftercare.evaluation.runner import _namespace
from aftercare.intelligence.decision_candidate import load_binding
from aftercare.intelligence.models import ModelRunner, Registry
from aftercare.intelligence.service import AgentService, RunAudit, RunError
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from aftercare.investigation.graph import CHAT_GRAPH_PROFILE, NO_RECHECK_ARM

client, system, configured = _client, _system, _configured


def _controlled_transport(context, calls, protocol="compact-v6", *, conflict=True):
    eligible = context["eligibility"]
    line = eligible["lines"][0]
    line_id, amount = line["line_id"], line["available_cents"]
    policy_id = eligible["policy_ids_by_action"]["REFUND"][0]
    policy = next(
        row for row in context["policy_resolution"]["documents"] if row["id"] == policy_id
    )
    ref = f"{policy_id}:{policy['version']}"
    proposed = {
        "id": "refund",
        "action": "REFUND",
        "policy_ids": [policy_id],
        "lines": [{"line_id": line_id, "amount_cents": amount}],
        "evidence_ids": [line_id, ref],
        "rationale": "按适用条款及可退余额提案。",
    }
    line_args = {"line_ids": [line_id]}
    product_id = line["product_id"]
    policy_args = {"policy_id": policy_id, "version": policy["version"], "product_id": product_id}
    drafts = 0

    def transport(spec, messages, _tools, _timeout, _output):
        nonlocal drafts
        calls.append(spec.model)
        role = messages[0].content.split("。", 1)[0].split()[-1]
        sent = json.loads(messages[1].content)
        view = sent["frozen_input"]
        assert view["evidence_encoding"] == protocol
        if (
            protocol == "compact-v7"
            and role in {"strong_single", "fact"}
            and isinstance(messages[-1], ToolMessage)
        ):
            records = [
                record
                for message in messages
                if isinstance(message, ToolMessage)
                for record in json.loads(message.content).get("data", {}).get("records", [])
            ]
            assert any(
                (
                    view["available_evidence"][record["ref"]]
                    if type(record["ref"]) is int
                    else record["ref"]
                )[1] == "recorded-order-payment"
                and "paid_at" in record["data"]
                for record in records
            )
        if role == "strong_single":
            if not isinstance(messages[-1], ToolMessage):
                return _reply(
                    [
                        {"name": "read_order_lines", "id": "line", "args": line_args},
                        {"name": "read_policy_clause", "id": "policy", "args": policy_args},
                    ]
                )
            return _reply(
                {
                    "claims": [
                        _claim("amount", line_id, str(amount)),
                        _claim("clause", ref, "适用"),
                    ],
                    "options": [proposed],
                }
            )
        if role == "fact":
            if "check_receipt" in view:
                return _reply({"claims": [_claim("amount", line_id, str(amount))]})
            if not isinstance(messages[-1], ToolMessage):
                return _call("read_order_lines", line_args)
            drafts += 1
            return _reply(
                {
                    "claims": [
                        _claim("amount", line_id, str(amount - (conflict and drafts % 2 == 0)))
                    ]
                }
            )
        if role == "policy":
            if not isinstance(messages[-1], ToolMessage):
                return _call("read_policy_clause", policy_args)
            return _reply({"claims": [_claim("clause", ref, "适用")]})
        assert role == "verifier"
        upstream = sent["upstream_claims"]
        if protocol == "compact-v7" and isinstance(upstream, dict):
            upstream = [
                dict(zip(upstream["columns"], row, strict=True)) for row in upstream["rows"]
            ]
        fact = next(claim for claim in upstream if claim["id"] == "fact/amount")
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
                "evidence_refs": [ref],
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
                        "arguments": line_args,
                        "reason": "订单余额不一致。",
                    },
                }
            )
        return _reply({"assessments": assessments, "options": [proposed]})

    return transport


def _gated_controlled_run(runner, manifest, case, directory):
    """Send actual runner messages through Responses serialization and the pure P5 gate."""
    from langchain_openai import ChatOpenAI

    path = Path(__file__).resolve().parents[3] / "scripts/chat_p4_wire_gate/p5_policy.py"
    module_spec = importlib.util.spec_from_file_location("runner_p5_wire_policy", path)
    gate = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(gate)
    directory.mkdir()
    (directory / "lock").touch()
    campaign = "paired-fixture"
    frozen = _freeze_case(case).record
    plan = {
        "wire_rows": [
            {
                "id": str(
                    uuid5(
                        NAMESPACE_URL,
                        digest([PROTOCOL, campaign, case.repetition, case.case_id, arm]),
                    )
                ),
                "arm": arm,
            }
            for arm in ARMS
        ],
        "scope_sha256": frozen["scope_sha256"],
        "model": "gpt-6.1-terra",
        "output_tokens": 1800,
        "per_post_reserved_microusd": 49_000,
    }
    client = ChatOpenAI(model=plan["model"], api_key="unused-fixture", use_responses_api=True)

    def write(path, record):
        with path.open("x") as output:
            json.dump(record, output)

    original = runner.model.transport

    def gated(spec, messages, tools, timeout, output):
        payload = client._get_request_payload(messages)
        initial = next(item for item in payload["input"] if item.get("role") == "user")
        view = json.loads(initial["content"])["frozen_input"]
        assert view["input_digest"] == frozen["relevant_input_sha256"]
        assert view["evidence_encoding"] == "compact-v6"
        role, recheck = gate.request_role(payload["input"], view, frozen["catalogue_sha256"])
        index, row = gate.admit(
            directory,
            plan,
            write,
            json.dumps(payload).encode(),
            [item["function"]["name"] for item in tools],
            role=role,
            recheck=recheck,
        )
        write(
            directory / f"response-{index:02}.json",
            {
                "row_id": row,
                "call_index": index,
                "http_status": 200,
            },
        )
        return original(spec, messages, tools, timeout, output)

    runner.model.transport = gated
    runner._wire_row = partial(gate.active_row, directory, plan, write)
    try:
        result = runner.run(manifest, campaign, [case])
    finally:
        runner.model.transport = original
        runner._wire_row = None
    admission = [json.loads(path.read_bytes()) for path in directory.glob("admission-*.json")]
    assert {(row["arm"], row["phase_family"]) for row in admission} == {
        ("strong_single", "single"),
        ("single_staged", "staged-fact"),
        ("single_staged", "staged-policy"),
        ("single_staged", "verify-0"),
        ("multi", "fact"),
        ("multi", "policy"),
        ("multi", "verify"),
        ("multi", "recheck"),
    }
    return result


def _serialize_sqlite_audit(monkeypatch):
    # SQLite has no row locks. The fixture serializes audit transactions only.
    audit_lock = Lock()

    def serialized(method):
        @wraps(method)
        def invoke(*args, **kwargs):
            with audit_lock:
                return method(*args, **kwargs)

        return invoke

    for name in ("event", "begin_attempt", "finish_attempt"):
        monkeypatch.setattr(RunAudit, name, serialized(getattr(RunAudit, name)))


def _fixture_registry():
    return Registry.model_validate(
        {
            "version": "chat-eval-controlled",
            "allow_unverified": True,
            "models": [
                {
                    "alias": "analysis",
                    "provider": "fixture",
                    "model": "controlled",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "1",
                    "price_date": "2026-11-11",
                    "source": "synthetic",
                    "context_tokens": 32000,
                    "data_scopes": ["customer_conversation"],
                    "output_tokens": 1800,
                }
            ],
        }
    )


@pytest.mark.parametrize("eligible", [True, False])
@pytest.mark.parametrize("protocol", ["compact-v6", "compact-v7"])
def test_frozen_scope_across_arms_and_interruption_is_not_resent(
    configured, tmp_path, monkeypatch, eligible, protocol
):
    _serialize_sqlite_audit(monkeypatch)
    url, case_id = _link(configured)
    assert (
        send(
            configured.client,
            url,
            "商品照片用于本次合成评测。",
            photos=(("item.png", _photo("green")),),
        ).status_code
        == 201
    )
    if eligible:
        _evidence(configured.service, case_id)
    configured.service.settings.chat_investigation_enabled = True
    configured.service.settings.chat_investigation_protocol = protocol
    with Session(configured.db) as db:
        conversation = db.get_one(Conversation, url.split("/")[-1])
    case = FrozenCase(
        "synthetic-ordinary",
        1,
        configured.service,
        SUPPORT,
        conversation.id,
        conversation.analysis_issue_id,
        datetime.now(UTC) - timedelta(hours=1),
        "controlled synthetic fixture",
    )

    calls = []

    def no_model_expected(*_args, **_kwargs):
        pytest.fail("PROGRAM attempted model transport")

    transport = (
        _controlled_transport(configured.service.context(case_id, SUPPORT), calls, protocol)
        if eligible
        else no_model_expected
    )

    model = ModelRunner(_fixture_registry(), transport=transport)
    runner = ChatEvaluationRunner(model, InMemorySaver(), calls=8, budget=Decimal("0.10"))
    manifest = Manifest(tmp_path / "paired")
    result = (
        _gated_controlled_run(runner, manifest, case, tmp_path / "wire")
        if eligible and protocol == "compact-v6"
        else runner.run(manifest, "paired-fixture", [case])
    )
    rows = result["rows"]
    controls = manifest.read("plan.json")["controls"]
    assert controls["prompt"] == protocol
    assert controls.get("evidence_projection") == (
        "human-material-payment-v1" if protocol == "compact-v7" else None
    )
    assert [row["arm"] for row in rows] == list(ARMS)
    assert [row["route"] for row in rows] == (
        ["SINGLE", "STAGED", "MULTI"] if eligible else ["PROGRAM"] * 3
    )
    assert len({row["input_sha256"] for row in rows}) == 1
    assert len({row["run_id"] for row in rows}) == 3
    assert len({row["snapshot_id"] for row in rows}) == 3
    for row in rows:
        assert row["candidate_persisted_ms"] >= 0 and row["latency_issue"] is None
        arm = create_engine(f"sqlite:///{manifest.directory / (_namespace(row['id']) + '.sqlite')}")
        try:
            with Session(arm) as db:
                run = db.get_one(AgentRun, row["run_id"])
                binding = load_binding(db, run)
                assert binding["investigation_prompt_version"] == protocol
                assert binding.get("investigation_evidence_projection") == (
                    "human-material-payment-v1" if protocol == "compact-v7" else None
                )
                snapshot = db.get_one(DecisionSnapshot, row["snapshot_id"])
                assert utc(snapshot.created_at) >= utc(run.created_at) > case.decision_at
                assert snapshot.input["frozen_context"]["policy_catalogue"]["decision_at"] == (
                    case.decision_at.isoformat()
                )
        finally:
            arm.dispose()
    if eligible:
        assert all(row["status"] == "COMPLETED" for row in rows), rows
        assert all(row["program_checks"]["refund"]["status"] == "PASSED" for row in rows)
        assert [set(row["phases"]) for row in rows] == [
            {"single"},
            {"staged-fact", "staged-policy", "verify-0"},
            {"fact", "policy", "verify-0", "recheck", "verify-1"},
        ]
        assert [len(row["rechecks"]) for row in rows] == [0, 0, 1]
        assert rows[2]["rechecks"][0]["claim_ids"] == ["fact/amount"]
    assert len(calls) == sum(len(row["attempts"]) for row in rows) == (14 if eligible else 0)
    with Session(configured.db) as db:
        assert not db.scalar(select(func.count()).select_from(AgentRun))
    assert runner.run(manifest, "paired-fixture", [case]) == result
    assert len(calls) == (14 if eligible else 0)
    with pytest.raises(ValueError, match="CHAT_EVAL_PLAN_CHANGED"):
        runner.run(
            manifest,
            "paired-fixture",
            [replace(case, decision_at=case.decision_at + timedelta(seconds=1))],
        )

    if eligible and protocol == "compact-v7":
        _exercise_recheck_ablation(case, configured, model.registry, tmp_path, monkeypatch)

    # A recorded start with no verified arm namespace cannot be dispatched again.
    interrupted = Manifest(tmp_path / "interrupted")
    plan = manifest.read("plan.json")
    with interrupted.lock():
        staged = interrupted.prepare("interrupted-fixture", plan["cases"], plan["controls"])
        interrupted.start(bound_rows(staged)[0])
    recovered = runner.run(interrupted, "interrupted-fixture", [case])
    assert recovered["rows"][0]["status"] == "UNKNOWN"
    assert recovered["rows"][0]["candidate_status"] == "MISSING"
    assert len(calls) == (26 if eligible else 0)

    # Reconcile a missing receipt without resending, even if a cloned typed object is corrupt.
    row = rows[0]
    arm_prefix = f"evaluation/{_namespace(row['id'])}/"
    objects = configured.service.storage.objects
    if eligible:
        typed_key = next(key for key in objects if key.startswith(arm_prefix + "cases/"))
        original = objects[typed_key]
        manifest.directory.joinpath(f"{row['id']}.result.json").unlink()
        objects[typed_key] = b"corrupted typed material"
        typed = runner.run(manifest, "paired-fixture", [case])["rows"][0]
        assert typed["status"] == "FAILED"
        assert typed["reason"] == "CHAT_EVAL_ARM_MATERIALS_CHANGED"
        assert typed["candidate_status"] == "UNUSABLE"
        assert len(calls) == 26
        objects[typed_key] = original

    # Simulate an interrupted receipt write, then make a previously used arm object unreadable.
    manifest.directory.joinpath(f"{row['id']}.result.json").unlink()
    for key in configured.service.storage.objects:
        if key.startswith(arm_prefix):
            configured.service.storage.objects[key] = b"corrupted"
    reconciled = runner.run(manifest, "paired-fixture", [case])["rows"][0]
    assert reconciled["status"] == "FAILED"
    assert reconciled["reason"] == "CHAT_EVAL_ARM_IMAGES_CHANGED"
    assert reconciled["candidate_status"] == "UNUSABLE"
    assert reconciled["run_id"] == row["run_id"]
    assert reconciled["snapshot_id"] == row["snapshot_id"]
    assert len(calls) == (26 if eligible else 0)

    # Remove a route or damage the candidate's issue binding in existing arm ledgers.
    for index, field in ((1, "route"), (2, "issue")):
        observed = rows[index]
        db_file = manifest.directory / f"{_namespace(observed['id'])}.sqlite"
        arm = create_engine(f"sqlite:///{db_file}")
        try:
            with Session(arm) as db, db.begin():
                if field == "route":
                    route_events = select(RunEvent).where(
                        RunEvent.run_id == observed["run_id"],
                        RunEvent.kind == "investigation_route",
                    )
                    if protocol == "compact-v7":
                        event = db.scalars(route_events).one()
                        event.payload = {
                            key: value
                            for key, value in event.payload.items()
                            if key != "evidence_projection"
                        }
                    else:
                        db.execute(
                            delete(RunEvent).where(
                                RunEvent.run_id == observed["run_id"],
                                RunEvent.kind == "investigation_route",
                            )
                        )
                else:
                    db.get_one(DecisionSnapshot, observed["snapshot_id"]).issue_id = "wrong-issue"
        finally:
            arm.dispose()
        (manifest.directory / f"{observed['id']}.result.json").unlink()
    audited = runner.run(manifest, "paired-fixture", [case])["rows"]
    for observed, original in zip(audited[1:], rows[1:], strict=True):
        assert observed["status"] == "FAILED"
        assert observed["candidate_status"] == "UNUSABLE"
        assert observed["snapshot_id"] == original["snapshot_id"]
        assert observed["comparability_reason"] == "BINDING_MISMATCH"
    assert len(calls) == (26 if eligible else 0)

    if eligible:

        def failed_before_route(agent, run_id):
            with Session(agent.engine) as db, db.begin():
                run = db.get_one(AgentRun, run_id)
                run.status, run.reason = "FAILED", "CONTROLLED_PRE_ROUTE_FAILURE"

        with monkeypatch.context() as patched:
            patched.setattr(AgentService, "process", failed_before_route)
            early = runner.run(Manifest(tmp_path / "early"), "early-fixture", [case])["rows"]
        assert all(
            row["status"] == "FAILED"
            and row["reason"] == "CONTROLLED_PRE_ROUTE_FAILURE"
            and row["comparability_reason"] == "ROUTE_NOT_REACHED"
            and row["candidate_status"] == "MISSING"
            and not row["input_comparable"]
            for row in early
        )

        def failed_start(*_args, **_kwargs):
            raise RuntimeError("disrupted before durable run binding")

        monkeypatch.setattr(AgentService, "start", failed_start)
        uncertain = runner.run(Manifest(tmp_path / "unbound"), "unbound-fixture", [case])["rows"]
        assert all(
            row["status"] == "UNKNOWN"
            and row["reason"] == "CHAT_EVAL_RUN_UNBOUND"
            and row["known_estimated_usd"] is None
            and row["actual_billed_usd"] is None
            for row in uncertain
        )
        assert len(calls) == 26

        def unexpected_sql(*_args, **_kwargs):
            pytest.fail("PostgreSQL execution reached")

        pg = create_mock_engine("postgresql://", unexpected_sql)
        monkeypatch.setattr(configured.service, "engine", pg)
        with pytest.raises(ValueError, match="CHAT_EVAL_ISOLATED_TEST_SOURCE_REQUIRED"):
            runner.run(Manifest(tmp_path / "rejected"), "rejected-fixture", [case])
        monkeypatch.setattr(configured.service, "engine", configured.db)

    receipt = manifest.directory.joinpath(f"{row['id']}.result.json")
    receipt.write_text(json.dumps({**reconciled, "controls_sha256": "changed"}))
    with pytest.raises(ValueError, match="CHAT_EVAL_RECEIPT_BINDING_MISMATCH"):
        runner.run(manifest, "paired-fixture", [case])
    assert len(calls) == (26 if eligible else 0)


def _exercise_recheck_ablation(case, configured, registry, tmp_path, monkeypatch):
    calls = []
    context = configured.service.context(_freeze_case(case).scope.case_id, SUPPORT)
    model = ModelRunner(
        registry, transport=_controlled_transport(context, calls, "compact-v7", conflict=False)
    )
    saver = InMemorySaver()
    runner = ChatEvaluationRunner(
        model, saver, calls=8, budget=Decimal("0.10"), recheck_ablation=True
    )
    manifest = Manifest(tmp_path / "ablation-live")
    result = runner.run(manifest, "ablation-fixture", [case])
    full, ablated = result["rows"]
    assert (full["arm"], ablated["arm"]) == ("multi", NO_RECHECK_ARM)
    assert full["input_sha256"] == ablated["input_sha256"]
    assert full["controls_sha256"] == ablated["controls_sha256"]
    assert manifest.read("plan.json")["controls"]["calls"] == 8
    assert manifest.read("plan.json")["controls"]["budget_usd"] == "0.10"
    assert full["status"] == ablated["status"] == "COMPLETED"
    assert full["program_checks"] == ablated["program_checks"]
    assert set(full["phases"]) == set(ablated["phases"]) == {"fact", "policy", "verify-0"}
    assert full["rechecks"] == ablated["rechecks"] == ablated["suppressed_rechecks"] == []
    assert len(full["attempts"]) == len(ablated["attempts"]) == 5
    candidates = []
    for row in (full, ablated):
        engine = create_engine(
            f"sqlite:///{manifest.directory / (_namespace(row['id']) + '.sqlite')}"
        )
        try:
            with Session(engine) as db:
                snapshot = db.get_one(DecisionSnapshot, row["snapshot_id"])
                candidates.append((snapshot.candidate["options"], snapshot.candidate["unresolved"]))
        finally:
            engine.dispose()
    assert candidates[0] == candidates[1]
    assert runner.run(manifest, "ablation-fixture", [case]) == result
    assert len(calls) == 10

    scope = _freeze_case(case).scope
    settings = configured.service.settings
    for agent in (
        AgentService(configured.db, settings, configured.service, None, model=model),
        AgentService(
            configured.db,
            SimpleNamespace(**{**vars(settings), "profile": "app"}),
            configured.service,
            None,
            model=model,
            allow_recheck_ablation=True,
        ),
    ):
        with pytest.raises(RunError) as denied:
            agent.start(
                scope.case_id,
                SUPPORT,
                expected_version=scope.business_versions["case"],
                idempotency_key="deny-ablation",
                mode="single",
                strategy="analysis",
                message=scope.frozen_context["legacy_agent_input"]["message"],
                decision_scope=scope,
                graph_profile=CHAT_GRAPH_PROFILE,
                investigation_arm=NO_RECHECK_ARM,
            )
        assert denied.value.code == "INVESTIGATION_PROFILE_UNAVAILABLE"

    arm = create_engine(f"sqlite:///{manifest.directory / (_namespace(ablated['id']) + '.sqlite')}")
    try:
        with Session(arm) as db, db.begin():
            run = db.get_one(AgentRun, ablated["run_id"])
            assert load_binding(db, run)["investigation_arm"] == NO_RECHECK_ARM
            run.status = "QUEUED"
        business = BusinessService(arm, settings, storage=configured.service.storage)
        recovered = AgentService(arm, settings, business, saver, model=model)

        def unexpected_checkpoint(*_args, **_kwargs):
            pytest.fail("checkpoint reached without ablation capability")

        monkeypatch.setattr(
            recovered.graphs[CHAT_GRAPH_PROFILE], "get_state", unexpected_checkpoint
        )
        recovered.process(ablated["run_id"])
        with Session(arm) as db:
            run = db.get_one(AgentRun, ablated["run_id"])
            assert run.status == "NEEDS_REVIEW"
            assert run.reason == "INVESTIGATION_PROFILE_UNAVAILABLE"
            assert db.scalar(select(func.count()).select_from(ModelAttempt)) == 5
        assert len(calls) == 10
    finally:
        arm.dispose()


def test_elapsed_rejects_negative_order_and_normalizes_clock_offsets():
    start = datetime.fromisoformat("2026-11-11T10:00:00+08:00")
    before = datetime.fromisoformat("2026-11-11T01:59:59.999+00:00")
    after = datetime.fromisoformat("2026-11-10T21:00:00.125-05:00")
    assert _elapsed(start, before) is None
    assert _elapsed(start, after) == 125
    assert _elapsed(start, after.astimezone(UTC).replace(tzinfo=None)) == 125
    assert _elapsed(start, None) is None


def test_recheck_ablation_manifest_has_distinct_arms_and_cannot_resume_standard(tmp_path):
    from aftercare.evaluation.chat.manifest import ARMS, RECHECK_ABLATION, Manifest

    cases = [{"case_id": "case", "repetition": 1, "scope_sha256": "a" * 64}]
    controls = {"comparison": RECHECK_ABLATION}
    ablation = Manifest(tmp_path / "ablation")
    standard = Manifest(tmp_path / "standard")
    with ablation.lock():
        plan = ablation.prepare_recheck_ablation("campaign", cases, controls)
        assert plan["protocol"] == RECHECK_ABLATION
        assert [row["arm"] for row in plan["rows"]] == ["multi", "multi_no_recheck"]
        assert {row["input_sha256"] for row in plan["rows"]} == {"a" * 64}
        assert ablation.prepare_recheck_ablation("campaign", cases, controls) == plan
        with pytest.raises(ValueError, match="CHAT_EVAL_PLAN_CHANGED"):
            ablation.prepare("campaign", cases, {})
    with standard.lock():
        default = standard.prepare("campaign", cases, {})
        assert [row["arm"] for row in default["rows"]] == list(ARMS)
        assert {row["id"] for row in default["rows"]}.isdisjoint(row["id"] for row in plan["rows"])


def test_sequential_claims_ablation_plan_is_distinct_from_default_and_recheck(tmp_path):
    from aftercare.evaluation.chat.manifest import SEQUENTIAL_CLAIMS_ABLATION

    cases = [{"case_id": "case", "repetition": 1, "scope_sha256": "a" * 64}]
    manifest = Manifest(tmp_path / "sequential")
    with manifest.lock():
        plan = manifest.prepare_sequential_claims_ablation("campaign", cases, {})
        assert plan["protocol"] == SEQUENTIAL_CLAIMS_ABLATION
        assert [row["arm"] for row in plan["rows"]] == ["multi", "multi_sequential_claims"]
        assert {row["input_sha256"] for row in plan["rows"]} == {"a" * 64}
        with pytest.raises(ValueError, match="CHAT_EVAL_PLAN_CHANGED"):
            manifest.prepare_recheck_ablation("campaign", cases, {})
        with pytest.raises(ValueError, match="CHAT_EVAL_PLAN_CHANGED"):
            manifest.prepare("campaign", cases, {})


def test_sequential_claims_capability_is_test_support_and_arm_specific():
    from aftercare.auth import UserContext
    from aftercare.investigation.graph import SEQUENTIAL_CLAIMS_ARM

    settings = SimpleNamespace(profile="test", model_enabled=False)
    default = AgentService(None, settings, None, None)
    allowed = AgentService(None, settings, None, None, allow_sequential_claims_ablation=True)
    app = AgentService(
        None,
        SimpleNamespace(profile="app", model_enabled=False),
        None,
        None,
        allow_sequential_claims_ablation=True,
    )
    assert not default._investigation_arm_allowed(SEQUENTIAL_CLAIMS_ARM, SUPPORT)
    assert allowed._investigation_arm_allowed(SEQUENTIAL_CLAIMS_ARM, SUPPORT)
    assert not allowed._investigation_arm_allowed(NO_RECHECK_ARM, SUPPORT)
    assert not allowed._investigation_arm_allowed(
        SEQUENTIAL_CLAIMS_ARM, UserContext(user_id="customer", role="customer")
    )
    assert not app._investigation_arm_allowed(SEQUENTIAL_CLAIMS_ARM, SUPPORT)
