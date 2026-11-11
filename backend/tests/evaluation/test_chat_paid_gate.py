"""Controlled checks for the P5 paid admission boundary. No provider access."""

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from aftercare.business.rules import digest
from aftercare.evaluation.chat.runner import ChatEvaluationRunner
from aftercare.intelligence.models import ModelRunner, Registry

ROOT = Path(__file__).resolve().parents[3]
LOCATION = ROOT / "scripts/chat_p4_wire_gate/p5_policy.py"
MODULE = importlib.util.spec_from_file_location("chat_p5_wire_policy", LOCATION)
gate = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(gate)


def _write(path, data):
    with path.open("x") as stream:
        json.dump(data, stream)


def _plan(tmp_path):
    (tmp_path / "lock").touch()
    scope = "a" * 64
    rows = [
        {"id": arm, "arm": arm, "input_sha256": scope}
        for arm in ("strong_single", "single_staged", "multi")
    ]
    return rows, {
        "wire_rows": rows,
        "scope_sha256": scope,
        "model": "gpt-6.1-terra",
        "output_tokens": 1800,
        "per_post_reserved_microusd": 49_000,
    }


def test_wire_admission_binds_row_and_counts_delivery_unknown_against_budget(tmp_path):
    rows, plan = _plan(tmp_path)
    with pytest.raises(RuntimeError, match="ACTIVE_ROW_REQUIRED"):
        gate.admit(tmp_path, plan, _write, b"text fixture", [], role="strong_single")
    with (
        pytest.raises(RuntimeError, match="ROW_BINDING_REJECTED"),
        gate.active_row(tmp_path, plan, _write, {**rows[0], "input_sha256": "wrong"}),
    ):
        pytest.fail("Unbound row was admitted")

    def attempt(index, role):
        try:
            return gate.admit(
                tmp_path, plan, _write, f"fixture-{index}".encode(), ["read_order_lines"], role=role
            )
        except RuntimeError as error:
            assert str(error) == "P5_WIRE_ADMISSION_LIMIT"
            return None

    for row, role in zip(rows, ("strong_single", "fact", "fact"), strict=True):
        with gate.active_row(tmp_path, plan, _write, row), ThreadPoolExecutor(4) as pool:
            admitted = [
                value
                for value in pool.map(lambda index, role=role: attempt(index, role), range(12))
                if value is not None
            ]
        assert len(admitted) == 8 and all(row_id == row["id"] for _, row_id in admitted)
    records = [json.loads(path.read_bytes()) for path in tmp_path.glob("admission-*.json")]
    assert len(records) == 24
    assert sum(item["reserved_microusd"] for item in records) == 1_176_000
    assert all(item["status"] == "DELIVERY_UNKNOWN" for item in records)
    assert all(
        item["attempt_binding"] == "ROW_ONLY_NOT_MATCHED_TO_LOGICAL_ATTEMPT" for item in records
    )


def test_row_cost_limit_can_bind_before_post_limit(tmp_path):
    rows, plan = _plan(tmp_path)
    plan["per_post_reserved_microusd"] = 80_001
    with gate.active_row(tmp_path, plan, _write, rows[0]):
        for index in range(4):
            gate.admit(
                tmp_path, plan, _write, f"fixture-{index}".encode(), [], role="strong_single"
            )
        with pytest.raises(RuntimeError, match="ADMISSION_LIMIT"):
            gate.admit(tmp_path, plan, _write, b"new fixture", [], role="strong_single")


def test_paid_constructor_requires_loaded_gate_and_owned_runtime(monkeypatch):
    from langchain_openai._compat import httpx

    model = {
        "alias": "analysis",
        "provider": "openai",
        "model": "gpt-6.1-terra",
        "endpoint": "https://api.openai.com/v1",
        "interface": "responses",
        "api_key_env": "AFTERCARE_OPENAI_KEY",
        "context_tokens": 32000,
        "output_tokens": 1800,
        "compatibility": "VERIFIED",
        "compatibility_version": "fixture",
        "checked_at": "2026-11-11",
        "source": "test-only model registration",
        "data_scopes": ["customer_conversation"],
    }
    registry = Registry.model_validate({"version": "test", "models": [model]})
    with pytest.raises(ValueError, match="FIXED_MODEL_REQUIRED"):
        ChatEvaluationRunner(ModelRunner(registry), None, calls=8, budget=None)
    runner_model = ModelRunner(registry)
    plan = {
        "protocol": "p5-paid-text-v2",
        "campaign": "chat-p5-paid-terra-ordinary-v2",
        "prompt_version": "compact-v6",
        "scope_sha256": "a" * 64,
        "registry_sha256": runner_model.base_registry_digest,
    }
    gate_sources = {"sitecustomize.py": b"stub wrapper", "p5_policy.py": b"stub policy"}
    plan["files"] = {name: sha256(value).hexdigest() for name, value in gate_sources.items()}
    options = {"approval_sha256": digest(plan), "scope_sha256": plan["scope_sha256"]}
    read = Path.read_bytes

    def read_pinned(path):
        if path.parent == Path("/p4-gate"):
            return gate_sources[path.name]
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", read_pinned)
    monkeypatch.delitem(sys.modules, "sitecustomize", raising=False)
    with pytest.raises(ValueError, match="PAID_GATE_REQUIRED"):
        ChatEvaluationRunner._admitted_paid_text(runner_model, **options)
    loaded = SimpleNamespace(
        READY=True,
        PROTOCOL=plan["protocol"],
        PLAN=plan,
        STATE=Path("/p4-wire"),
        p5_policy=gate,
        _write=_write,
        SEND=httpx.Client.send,
        HANDLE=httpx.HTTPTransport.handle_request,
        _send=lambda *_: None,
        _handle=lambda *_: None,
        __file__="/p4-gate/sitecustomize.py",
    )
    monkeypatch.setattr(gate, "__file__", "/p4-gate/p5_policy.py")
    monkeypatch.setitem(sys.modules, "p5_policy", gate)
    monkeypatch.setitem(sys.modules, "sitecustomize", loaded)
    with pytest.raises(ValueError, match="PAID_GATE_REQUIRED"):
        ChatEvaluationRunner._admitted_paid_text(runner_model, **options)
    monkeypatch.setattr(httpx.Client, "send", loaded._send)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", loaded._handle)
    historical = {
        **plan,
        "protocol": "p5-paid-text-v1",
        "campaign": "chat-p5-paid-terra-ordinary-v1",
        "prompt_version": "compact-v5",
    }
    loaded.PROTOCOL = historical["protocol"]
    loaded.PLAN = historical
    with pytest.raises(ValueError, match="PAID_GATE_REQUIRED"):
        ChatEvaluationRunner._admitted_paid_text(
            runner_model, **{**options, "approval_sha256": digest(historical)}
        )
    loaded.PROTOCOL = plan["protocol"]
    loaded.PLAN = plan
    with pytest.raises(ValueError, match="PAID_GATE_REQUIRED"):
        ChatEvaluationRunner._admitted_paid_text(
            runner_model, **{**options, "approval_sha256": "b" * 64}
        )
    with pytest.raises(ValueError, match="PAID_ADMISSION_INVALID"):
        ChatEvaluationRunner._admitted_paid_text(
            ModelRunner(registry, transport=lambda *_: None), **options
        )
    admitted = ChatEvaluationRunner._admitted_paid_text(runner_model, **options)
    with pytest.raises(ValueError, match="PAID_RUNTIME_REQUIRED"):
        admitted._run(
            None,
            plan["campaign"],
            [],
            SimpleNamespace(control_fields=lambda: {"storage_topology": "OWNED_POSTGRESQL_S3"}),
        )
    assert admitted.controls["evidence_grade"] == "REAL_PROVIDER_TEXT_ONLY"
    assert admitted.controls["effective_calls"] == 8
    assert admitted.controls["effective_budget_usd"] == "0.40"
    assert admitted.controls["latency_version"] == "runtime-utc-v1"


@pytest.mark.parametrize(
    ("protocol", "prompt_version"),
    [
        ("p5-paid-text-v1", "compact-v5"),
        ("p5-paid-text-v2", "compact-v6"),
    ],
)
def test_p5_http_gate_rejects_image_and_binds_real_wire_receipt_shape(
    tmp_path,
    monkeypatch,
    protocol,
    prompt_version,
):
    from langchain_openai._compat import httpx

    rows, plan = _plan(tmp_path)
    scope = ["conversation", "issue", "case"]
    plan.update(
        protocol=protocol,
        prompt_version=prompt_version,
        relevant_input_sha256="b" * 64,
        policy_catalogue_sha256="c" * 64,
        conversation_id=scope[0],
        issue_id=scope[1],
        case_id=scope[2],
    )
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    sends = []
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda _, request: sends.append(request) or SimpleNamespace(status_code=200),
    )
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    script = LOCATION.with_name("sitecustomize.py").read_text()
    monkeypatch.setitem(sys.modules, "p5_policy", gate)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", httpx.HTTPTransport.handle_request)
    namespace = {}
    exec(script.replace('Path("/p4-wire")', f"Path({str(tmp_path)!r})"), namespace)  # noqa: S102
    message = {
        "role": "user",
        "content": json.dumps(
            {
                "frozen_input": {
                    "input_digest": plan["relevant_input_sha256"],
                    "scope": scope,
                    "evidence_encoding": prompt_version,
                    "policy_context": {"catalogue_sha256": plan["policy_catalogue_sha256"]},
                }
            }
        ),
    }
    payload = {
        "model": plan["model"],
        "max_output_tokens": 1800,
        "store": False,
        "input": [{"role": "system", "content": "调查角色 strong_single。"}, message],
        "tools": [{"type": "function", "name": "read_order_lines"}],
    }

    def request(value):
        return httpx.Request(
            "POST",
            namespace["URL"],
            content=json.dumps(value),
            headers={"x-stainless-retry-count": "0"},
        )

    with gate.active_row(tmp_path, plan, _write, rows[0]):
        with pytest.raises(RuntimeError, match="TEXT_ONLY"):
            namespace["_handle"](
                None, request({**payload, "input": [*payload["input"], {"type": "input_image"}]})
            )
        with pytest.raises(RuntimeError, match="SCOPE_REJECTED"):
            namespace["_handle"](None, request({**payload, "input": []}))
        wrong = "compact-v6" if prompt_version == "compact-v5" else "compact-v5"
        mixed = {**message, "content": message["content"].replace(prompt_version, wrong)}
        with pytest.raises(RuntimeError, match="COMPACT_ENCODING_REQUIRED"):
            namespace["_handle"](None, request({**payload, "input": [payload["input"][0], mixed]}))
        assert namespace["_handle"](None, request(payload)).status_code == 200
    assert len(sends) == 1
    assert json.loads((tmp_path / "response-01.json").read_bytes()) == {
        "call_index": 1,
        "row_id": rows[0]["id"],
        "http_status": 200,
    }


def test_outcomes_distinguish_http_failures_unknown_and_missing_candidates():
    path = ROOT / "scripts/verify_chat_p5_paid.py"
    module_spec = importlib.util.spec_from_file_location("chat_p5_paid_entry", path)
    entry = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(entry)
    arms = ("strong_single", "single_staged", "multi")
    families = (
        ("single",),
        ("staged-fact", "staged-policy", "verify-0"),
        ("fact", "policy", "verify"),
    )
    native = (
        ("single",),
        ("staged-fact", "staged-policy", "verify-0"),
        ("fact", "policy", "verify-0"),
    )
    rows = [
        {
            "id": arm,
            "arm": arm,
            "status": "COMPLETED",
            "reason": "ELIGIBLE",
            "input_comparable": True,
            "candidate_status": "AVAILABLE",
            "phases": list(phases),
            "validated_phase_results": list(phases),
            "attempts": [
                {
                    "status": "SUCCEEDED",
                    "logical_id": f"p4:compact-v6:{phase}:0",
                    "ordinal": 1,
                    "usage": {"total_tokens": 200},
                    "estimated_cost_usd": "0.01",
                }
                for phase in phases
            ],
        }
        for arm, phases in zip(arms, native, strict=True)
    ]
    groups = [{"row_id": row["id"], "row_count_agrees": True} for row in rows]

    def result(statuses):
        admissions = [
            {
                "row_id": row["id"],
                "phase_family": phase,
                "response": None if status is None else {"http_status": status},
            }
            for row, status, group in zip(rows, statuses, families, strict=True)
            for phase in group
        ]
        return entry._outcomes(
            rows, {"rows": groups, "admissions": admissions}, prompt_version="compact-v6"
        )

    assert result((401, 401, 401))["status"] == "FAILED"
    unknown = result((None, None, None))
    assert unknown["status"] == "PARTIAL" and unknown["provider_compatibility"] == "UNKNOWN"
    assert result((200, 200, 200))["status"] == "PASSED"
    rows[2]["attempts"][0]["logical_id"] = "p4:compact-v5:fact:0"
    assert result((200, 200, 200))["status"] == "PARTIAL"
    rows[2]["attempts"][0]["logical_id"] = "p4:compact-v6:fact:0"
    rows[0]["candidate_status"] = "MISSING"
    missing = result((200, 200, 200))
    assert missing["status"] == "PARTIAL" and missing["provider_compatibility"] == "VERIFIED"
    assert missing["candidate_availability"] == "PARTIAL"
    rows[0]["candidate_status"] = "AVAILABLE"
    fact = rows[2]["attempts"][0]
    fact["status"] = "FAILED"
    failed_fact = result((200, 200, 200))
    assert failed_fact["provider_compatibility"] == "VERIFIED"
    assert failed_fact["workflow_completion"] == "PARTIAL" and failed_fact["status"] == "PARTIAL"
    assert failed_fact["arm_outcomes"][2]["missing_native_phase_success"] == ["fact"]
    events = [
        {"kind": "investigation_input", "payload": {"phase_id": "fact"}},
        {
            "kind": "investigation_claims",
            "payload": {"phase_id": "policy", "role": "policy", "claims": []},
        },
        {"kind": "investigation_assessment", "payload": {"recheck_count": 0, "assessments": []}},
    ]
    rows[2]["validated_phase_results"] = entry._validated_phase_results(events)
    rows[2]["attempts"].append({**fact, "status": "SUCCEEDED", "ordinal": 2})
    no_final = result((200, 200, 200))
    assert no_final["status"] == "PARTIAL"
    assert no_final["arm_outcomes"][2]["missing_validated_phase_results"] == ["fact"]
    rows[2]["validated_phase_results"].append("fact")
    assert result((200, 200, 200))["status"] == "PASSED"
    rows[2]["attempts"].append({**fact, "logical_id": "p4:compact-v6:fact:1"})
    assert result((200, 200, 200))["arm_outcomes"][2]["workflow_completion"] == "INCOMPLETE"
    rows[2]["attempts"].pop()
    rows[2].update(status="NEEDS_REVIEW", reason="INVALID_MODEL_OUTPUT")
    assert result((200, 200, 200))["status"] == "PARTIAL"
    rows[2]["reason"] = "HUMAN_REVIEW"
    assert result((200, 200, 200))["status"] == "PASSED"


def test_wire_does_not_resend_identical_unknown_body(tmp_path):
    rows, plan = _plan(tmp_path)
    row = rows[0]
    with gate.active_row(tmp_path, plan, _write, row):
        gate.admit(tmp_path, plan, _write, b"same body", [], role="strong_single")
        with pytest.raises(RuntimeError, match="UNKNOWN_REQUEST_REPLAY"):
            gate.admit(tmp_path, plan, _write, b"same body", [], role="strong_single")
        _write(
            tmp_path / "response-01.json",
            {"row_id": row["id"], "call_index": 1, "http_status": 503},
        )
        assert gate.admit(tmp_path, plan, _write, b"same body", [], role="strong_single") == (
            2,
            row["id"],
        )


def test_v7_wire_requires_exact_role_refs_and_original_observation_reference():
    observation = ["OBSERVATION", "vision-1", "a" * 64]
    human_review = ["REVIEW", "review-1", "b" * 64]
    payment = ["CONDITION", "recorded-order-payment", "c" * 64]
    plan = {
        "observation_commitments": [observation],
        "role_evidence": {
            "fact": [observation, human_review],
            "policy": [payment],
        },
    }
    fact_view = {
        "available_evidence": [observation, human_review],
        "unverified_observations": [{"ref": observation}],
    }
    gate.validate_v7_references(plan, "fact", fact_view)
    gate.validate_v7_references(plan, "policy", {"available_evidence": [payment]})
    with pytest.raises(RuntimeError, match="V7_EVIDENCE_BINDING_REJECTED"):
        gate.validate_v7_references(plan, "policy", {"available_evidence": [human_review]})
    with pytest.raises(RuntimeError, match="V7_EVIDENCE_BINDING_REJECTED"):
        gate.validate_v7_references(
            plan, "fact", {**fact_view, "available_evidence": [observation]}
        )
    with pytest.raises(RuntimeError, match="V7_OBSERVATION_BINDING_REJECTED"):
        gate.validate_v7_references(plan, "fact", {**fact_view, "unverified_observations": []})


@pytest.mark.parametrize("fault", [None, "missing", "mixed_attempt", "wire_order", "duplicate"])
def test_v7_live_source_links_two_frozen_observations_to_one_vision_wire(fault):
    path = ROOT / "scripts/verify_chat_p5_paid.py"
    module_spec = importlib.util.spec_from_file_location("chat_p5_paid_entry", path)
    entry = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(entry)
    observations = [
        {
            "attempt_id": "vision-1",
            "attachment_id": f"attachment-{index}",
            "original_sha256": str(index) * 64,
            "sent_sha256": str(index + 2) * 64,
        }
        for index in (1, 2)
    ]
    sent = [row["sent_sha256"] for row in observations]
    originals = [row["original_sha256"] for row in observations]
    attachments = [row["attachment_id"] for row in observations]
    source = {
        "images": [
            {"position": index, "id": attachment, "sha256": original}
            for index, (attachment, original) in enumerate(zip(attachments, originals, strict=True))
        ],
        "scope": {"frozen_context": {"observations": list(reversed(observations))}},
    }
    vision = {
        "plan": {
            "attachment_ids": attachments,
            "original_image_sha256s": originals,
            "sent_sha256s": sent,
        },
        "wire_response": {"http_status": 200},
        "wire": {"sent_sha256s": list(sent)},
        "audit": {
            "attempts": [
                {
                    "attempt_id": "vision-1",
                    "state": "SUCCEEDED",
                    "turn_state": "PUBLISHED",
                    "response_evidence": {"validated_result_sha256": "c" * 64},
                }
            ],
            "observations": [dict(row) for row in observations],
        },
    }
    if fault == "missing":
        vision["audit"]["observations"].pop()
    elif fault == "mixed_attempt":
        vision["audit"]["observations"][0]["attempt_id"] = "vision-2"
    elif fault == "wire_order":
        vision["wire"]["sent_sha256s"].reverse()
    elif fault == "duplicate":
        vision["audit"]["observations"][1]["attachment_id"] = attachments[0]
    if fault:
        with pytest.raises(ValueError, match="V3_NATIVE_VISION_BINDING_REQUIRED"):
            entry._check_v3_vision(source, vision)
    else:
        entry._check_v3_vision(source, vision)


def test_p5_v7_verifier_expands_only_scoped_original_record_references():
    clause = ["POLICY", "POL14:1.0", "a" * 64]
    fact = ["ORDER_LINE", "L02", "b" * 64]
    plan = {"role_evidence": {"verifier": [clause], "fact": [fact]}}
    table = {
        "columns": ["ref", "trust", "data"],
        "rows": [
            [fact, "VERIFIED_RECORD", {}],
            [clause, "POLICY", {}],
        ],
    }
    view = {
        "evidence_encoding": "compact-v7",
        "verifier_encoding": "verifier-record-index-v1",
        "original_frozen_evidence": table,
        "available_evidence": [1],
        "original_read_receipts": [{"refs": [0, 1]}],
        "policy_corpus_rows": [1],
    }
    gate.validate_v7_references(plan, "verifier", view)
    for variant in (
        {**view, "evidence_encoding": "compact-v6"},
        {**view, "available_evidence": [True]},
        {**view, "available_evidence": [2]},
        {
            **view,
            "original_frozen_evidence": {
                **table,
                "rows": [
                    [fact, "VERIFIED_RECORD", {}],
                    [["POLICY", "POL14:1.0", "c" * 64], "POLICY", {}],
                ],
            },
        },
    ):
        with pytest.raises(RuntimeError, match="V7_EVIDENCE_BINDING_REJECTED"):
            gate.validate_v7_references(plan, "verifier", variant)
