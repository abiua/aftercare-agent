"""Paired execution through isolated local databases and the real worker core."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from threading import Lock

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

from aftercare.conversations.tables import Conversation
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.chat.manifest import ARMS, Manifest, bound_rows
from aftercare.evaluation.chat.runner import ChatEvaluationRunner, FrozenCase
from aftercare.evaluation.runner import _namespace
from aftercare.intelligence.models import ModelRunner, Registry
from aftercare.intelligence.service import AgentService, RunAudit
from aftercare.intelligence.tables import AgentRun, RunEvent

client, system, configured = _client, _system, _configured


def _controlled_transport(context, calls):
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
            return _reply({"claims": [_claim("amount", line_id, str(amount - (drafts % 2 == 0)))]})
        if role == "policy":
            if not isinstance(messages[-1], ToolMessage):
                return _call("read_policy_clause", policy_args)
            return _reply({"claims": [_claim("clause", ref, "适用")]})
        assert role == "verifier"
        fact = next(claim for claim in sent["upstream_claims"] if claim["id"] == "fact/amount")
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


@pytest.mark.parametrize("eligible", [True, False])
def test_frozen_scope_across_arms_and_interruption_is_not_resent(
    configured, tmp_path, monkeypatch, eligible
):
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
    with Session(configured.db) as db:
        conversation = db.get_one(Conversation, url.split("/")[-1])
    case = FrozenCase(
        "synthetic-ordinary",
        1,
        configured.service,
        SUPPORT,
        conversation.id,
        conversation.analysis_issue_id,
        datetime.now(UTC),
        "controlled synthetic fixture",
    )

    calls = []

    def no_model_expected(*_args, **_kwargs):
        pytest.fail("PROGRAM attempted model transport")

    transport = (
        _controlled_transport(configured.service.context(case_id, SUPPORT), calls)
        if eligible
        else no_model_expected
    )

    model = ModelRunner(
        Registry.model_validate(
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
        ),
        transport=transport,
    )
    runner = ChatEvaluationRunner(model, InMemorySaver(), calls=8, budget=Decimal("0.10"))
    manifest = Manifest(tmp_path / "paired")
    result = runner.run(manifest, "paired-fixture", [case])
    rows = result["rows"]
    assert [row["arm"] for row in rows] == list(ARMS)
    assert [row["route"] for row in rows] == (
        ["SINGLE", "STAGED", "MULTI"] if eligible else ["PROGRAM"] * 3
    )
    assert len({row["input_sha256"] for row in rows}) == 1
    assert len({row["run_id"] for row in rows}) == 3
    assert len({row["snapshot_id"] for row in rows}) == 3
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

    # Simulate an interrupted receipt write, then make a previously used arm object unreadable.
    row = rows[0]
    manifest.directory.joinpath(f"{row['id']}.result.json").unlink()
    arm_prefix = f"evaluation/{_namespace(row['id'])}/"
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
