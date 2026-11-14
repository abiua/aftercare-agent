import json
from threading import Barrier
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aftercare.business.rules import digest
from aftercare.intelligence.models import InvalidOutput
from aftercare.investigation.contracts import (
    CustomerMessage,
    Evidence,
    EvidenceRef,
    InvestigationInput,
    RouteDecision,
)
from aftercare.investigation.graph import build_investigation_graph
from aftercare.investigation.orchestration import (
    InvestigationContext,
    InvestigationResult,
    _model_tool_receipt,
    _result,
)
from aftercare.investigation.prompt_evidence import PROMPT_VERSION, PromptEvidence
from aftercare.investigation.protocol import (
    HUMAN_PAYMENT_V7,
    LEGACY_V6,
    protocol_for_binding,
    protocol_for_scope,
    protocol_for_settings,
)
from aftercare.investigation.tools import InvestigationTools


def _ref(kind, ident):
    return EvidenceRef(
        source_kind=kind, source_id=ident, scope=("chat", "issue", "case"), version_or_sha256="v1"
    )


@pytest.fixture
def investigation():
    return InvestigationInput(
        conversation_id="chat",
        issue_id="issue",
        case_id="case",
        order_id="order",
        input_through_seq=1,
        source_snapshot_sha256="a" * 64,
        relevant_input_sha256="b" * 64,
        intent="damaged",
        eligibility={},
        policy_blockers=(),
        business_versions={},
        messages=(
            CustomerMessage(ref=_ref("MESSAGE", "m"), sequence=1, text="客户说明：没有收到商品"),
        ),
        evidence=(
            Evidence(
                ref=_ref("ORDER_LINE", "line"),
                trust="VERIFIED_RECORD",
                data={"delivered": True, "cents": 500},
            ),
            Evidence(
                ref=_ref("ORDER_LINE", "line2"), trust="VERIFIED_RECORD", data={"delivered": False}
            ),
            Evidence(
                ref=_ref("POLICY", "POL01:v1"),
                trust="POLICY",
                data={
                    "selected_for": ["product"],
                    "clause": {"id": "POL01", "version": "v1", "text": "原始条款"},
                },
            ),
        ),
    )


class Audit:
    def __init__(self):
        self.events = []

    def event(self, name, data):
        self.events.append((name, data))


class Script:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, _audit, logical_id, **kwargs):
        self.calls.append(logical_id)
        assert kwargs["data_scope"] == "customer_conversation"
        response = self.responses[logical_id]
        value = response(kwargs) if callable(response) else response
        return kwargs["validate"](value if isinstance(value, list) else [], json.dumps(value))


def _call(name, **args):
    return {
        "name": name,
        "id": next(iter(args.values()))[0] if name == "read_order_lines" else name,
        "args": args,
    }


def _claim(ident, ref, *, value="是", depends=()):
    return {
        "id": ident,
        "subject": ident,
        "predicate": "fact",
        "value": value,
        "state": "SUPPORTED",
        "supporting_refs": [ref],
        "depends_on_claims": list(depends),
    }


def _review(*claims, request=None):
    return {
        "assessments": [
            {
                "claim_id": ident,
                "contradicted_field": "fact",
                "verdict": "CONFIRMED",
                "evidence_refs": [ref],
            }
            for ident, ref in claims
        ],
        "recheck": request,
    }


def _run(
    value,
    path,
    script,
    *,
    tools=None,
    arm="auto",
    extra=None,
    protocol=LEGACY_V6,
    output_reserve=1800,
    direct_recheck_request=False,
):
    audit = Audit()
    ctx = InvestigationContext(
        value,
        RouteDecision(path=path, reason="FIXTURE", financial_risk={}, output=extra or {}),
        script,
        audit,
        tools or InvestigationTools(value),
        lambda **_: pytest.fail("unexpected money validator"),
        "economy",
        frozenset({"line", "line2"}),
        frozenset({"line", "POL01:v1"}),
        arm=arm,
        protocol=protocol,
        output_reserve=output_reserve,
        direct_recheck_request=direct_recheck_request,
    )
    graph = build_investigation_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "controlled"}, "recursion_limit": 30}
    output = graph.invoke({"cycle": 0}, config, context=ctx)
    assert graph.get_state(config).values == output
    return output, audit.events


def test_executable_protocol_pairs_leave_legacy_bindings_and_preparation_scope_distinct():
    legacy_scope = SimpleNamespace(frozen_context={"case": {}})
    assert protocol_for_settings(SimpleNamespace()) == LEGACY_V6
    assert protocol_for_scope(legacy_scope) == LEGACY_V6
    assert (
        protocol_for_binding({"investigation_prompt_version": "compact-v6"}, legacy_scope)
        == LEGACY_V6
    )
    with pytest.raises(ValueError, match="INVESTIGATION_PROMPT_VERSION_CHANGED"):
        protocol_for_binding({"investigation_prompt_version": "compact-v5"}, legacy_scope)

    prepared = SimpleNamespace(
        frozen_context={"evidence_projection": "human-material-observations-v1"}
    )
    with pytest.raises(ValueError, match="INVESTIGATION_EVIDENCE_PROJECTION_READ_ONLY"):
        protocol_for_scope(prepared)

    new_scope = SimpleNamespace(
        frozen_context={"evidence_projection": HUMAN_PAYMENT_V7.evidence_projection}
    )
    assert (
        protocol_for_settings(SimpleNamespace(chat_investigation_protocol="compact-v7"))
        == HUMAN_PAYMENT_V7
    )
    assert protocol_for_scope(new_scope) == HUMAN_PAYMENT_V7
    binding = {"investigation_prompt_version": "compact-v7"}
    with pytest.raises(ValueError, match="INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH"):
        protocol_for_binding(binding, new_scope)
    binding["investigation_evidence_projection"] = HUMAN_PAYMENT_V7.evidence_projection
    assert protocol_for_binding(binding, new_scope) == HUMAN_PAYMENT_V7
    assert "evidence_projection" not in legacy_scope.frozen_context


@pytest.mark.parametrize("arm", ("strong_single", "single_staged", "multi"))
def test_selected_v7_prompt_encoding_and_observation_limits_reach_every_arm(investigation, arm):
    option = {
        "id": "followup",
        "action": "REQUEST_EVIDENCE",
        "target_line_ids": ["line"],
        "evidence_ids": ["line"],
        "rationale": "目前证据不足。",
        "missing_evidence": ["请补充能确认受损商品的照片。"],
    }

    def checked(response):
        def invoke(kwargs):
            system = kwargs["messages"][0].content
            assert "表达规则plain-zh-v1" in system
            assert "amount_cents仍用整数分" in system
            view = json.loads(kwargs["messages"][1].content)["frozen_input"]
            if view.get("verifier_encoding"):
                assert "材料审核记录说明人工对该版材料的业务判断" in system
                assert "人审材料不证明图片真实性、运输责任或财务批准" in system
                assert "同一行受影响数量不跨图相加" in system
                assert "支付事实只取已记录数据" in system
                assert "输出只用完整原ID" in system
            else:
                assert "HUMAN_MATERIAL_REVIEW 仅表示审核员登记了此版材料的所见" in system
                assert "多图不得相加" in system and "财务批准" in system
                assert "订单付款事实仅使用已记录" in system
            assert view["evidence_encoding"] == "compact-v7"
            assert view["input_digest"] == investigation.relevant_input_sha256
            return response

        return invoke

    line_call = _call("read_order_lines", line_ids=["line"])
    clause_call = _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product")
    if arm == "strong_single":
        responses = {
            "single:0": [line_call],
            "single:1": {"claims": [_claim("f", "line")], "options": [option]},
        }
    else:
        fact = "staged-fact" if arm == "single_staged" else "fact"
        policy = "staged-policy" if arm == "single_staged" else "policy"
        responses = {
            f"{fact}:0": [line_call],
            f"{fact}:1": {"claims": [_claim("f", "line")]},
            f"{policy}:0": [clause_call],
            f"{policy}:1": {"claims": [_claim("p", "POL01:v1")]},
            "verify-0:0": {
                **_review(("fact/f", "line"), ("policy/p", "POL01:v1")),
                "options": [option],
            },
        }
    model = Script(
        {f"p4:compact-v7:{phase}": checked(response) for phase, response in responses.items()}
    )
    result, events = _run(investigation, "SINGLE", model, arm=arm, protocol=HUMAN_PAYMENT_V7)
    assert result["reason"] == "HUMAN_REVIEW"
    assert result["output"]["investigation_options"][0]["action"] == "REQUEST_EVIDENCE"
    assert set(model.calls) == set(model.responses)
    (route,) = [payload for kind, payload in events if kind == "investigation_route"]
    assert route["prompt_version"] == "compact-v7"
    assert route["evidence_projection"] == HUMAN_PAYMENT_V7.evidence_projection
    assert all(
        payload["prompt_version"] == "compact-v7"
        for kind, payload in events
        if kind in {"investigation_input", "investigation_prompt_budget"}
    )


def test_program_is_zero_model_and_retains_evidence_gap(investigation):
    model = Script({})
    result, _ = _run(
        investigation,
        "PROGRAM",
        model,
        extra={"status": "WAITING_INPUT", "missing_materials": ["ITEM", "PARCEL"]},
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["output"]["status"] == "WAITING_INPUT"
    assert result["output"]["missing_materials"] == ["ITEM", "PARCEL"]
    assert not model.calls


def test_strong_single_parallel_batch_then_second_read_turn(investigation):
    barrier = Barrier(2)

    class ConcurrentTools(InvestigationTools):
        def call(self, role, name, args):
            if name == "read_order_lines":
                barrier.wait(timeout=3)
            return super().call(role, name, args)

    def round_response(ids, response):
        def check(kwargs):
            system = kwargs["messages"][0].content
            scope = json.loads(system.rsplit("\n", 1)[1])
            assert scope == {"citable_evidence_ids": ids, "upstream_claim_ids": []}
            assert "depends_on_claims" in system
            assert "POL06:1.0" in system
            visible = json.loads(kwargs["messages"][1].content)["frozen_input"]
            assert visible["customer_statements"][0]["ref"][1] == "m"
            assert "m" in scope["citable_evidence_ids"]
            return response

        return check

    model = Script(
        {
            f"p4:{PROMPT_VERSION}:single:0": round_response(
                ["m"],
                [
                    _call("read_order_lines", line_ids=["line"]),
                    _call("read_order_lines", line_ids=["line2"]),
                ],
            ),
            f"p4:{PROMPT_VERSION}:single:1": round_response(
                ["line", "line2", "m"],
                [
                    _call(
                        "read_policy_clause", policy_id="POL01", version="v1", product_id="product"
                    )
                ],
            ),
            f"p4:{PROMPT_VERSION}:single:2": round_response(
                ["POL01:v1", "line", "line2", "m"],
                {
                    "claims": [_claim("one", "line")],
                    "options": [
                        {
                            "id": "hold",
                            "action": "HUMAN_HANDOFF",
                            "target_line_ids": ["line"],
                            "rationale": "订单证据待客服核对。",
                            "evidence_ids": ["line", "POL01:v1"],
                        }
                    ],
                },
            ),
        }
    )
    result, events = _run(investigation, "SINGLE", model, tools=ConcurrentTools(investigation))
    assert result["output"]["investigation_options"][0]["action"] == "HUMAN_HANDOFF"
    assert len(model.calls) == 3
    assert sum(kind == "investigation_tool" for kind, _ in events) == 3


@pytest.mark.parametrize(
    ("arm", "direct"),
    [
        ("multi", False),
        ("multi", True),
        ("multi_no_recheck", False),
        ("multi_sequential_claims", False),
    ],
)
def test_multi_independent_roles_and_one_local_recheck(investigation, arm, direct):
    barrier = Barrier(2)

    def start(role, call):
        def check(kwargs):
            data = json.loads(kwargs["messages"][1].content)
            if arm == "multi_sequential_claims" and role == "policy":
                assert {row["id"] for row in data["upstream_claims"]} == {
                    "fact/f",
                    "fact/child",
                    "fact/other",
                }
                reference_scope = json.loads(kwargs["messages"][0].content.rsplit("\n", 1)[-1])
                assert reference_scope["citable_evidence_ids"] == []
                assert {tool["function"]["name"] for tool in kwargs["tools"]} == {
                    "search_applicable_policy",
                    "read_policy_clause",
                }
            else:
                assert data["upstream_claims"] == []
            assert ("customer_statements" in data["frozen_input"]) == (role == "fact")
            if arm != "multi_sequential_claims":
                barrier.wait(timeout=3)
            return [call]

        return check

    check_call = _call(
        "request_targeted_check",
        claim_id="fact/f",
        conflicting_refs=["line"],
        target_tool="read_order_lines",
        reason="原订单字段与事实判断冲突。",
    )
    raw_check = (
        InvestigationTools(investigation)
        .call("verifier", check_call["name"], check_call["args"])
        .model_dump(mode="json")
    )

    def first_verifier(kwargs):
        assert len(kwargs["messages"]) == 2
        return [check_call]

    def after_check_request(kwargs):
        messages = kwargs["messages"]
        if direct:
            assert len(messages) == 2
            assert kwargs["tools"] == []
            assert "此项仅为申请" in messages[0].content
        else:
            assert messages[-2].tool_calls == [dict(check_call, type="tool_call")]
            assert messages[-1].tool_call_id == check_call["id"]
            assert json.loads(messages[-1].content) == {
                "status": "REQUESTED",
                "receipt_sha256": digest(raw_check),
            }
        payload = json.loads(messages[1].content)
        originals = payload["frozen_input"]["original_frozen_evidence"]
        assert any(row["data"].get("delivered") is True for row in originals)
        assert any(row["value"] == "未交付" for row in payload["upstream_claims"])
        result = _review(
            ("fact/f", "line"),
            ("fact/child", "line"),
            ("fact/other", "line2"),
            ("policy/p", "POL01:v1"),
            request={
                "target_role": "fact",
                "claim_ids": ["fact/f"],
                "tool_name": "read_order_lines",
                "arguments": {"line_ids": ["line"]},
                "reason": "原订单字段与事实判断冲突。",
            },
        )

        result["assessments"][0]["verdict"] = "CONTRADICTED"
        return result

    def final_verifier(kwargs):
        payload = json.loads(kwargs["messages"][1].content)
        assert {row["id"] for row in payload["upstream_claims"]} == {
            "fact/f",
            "fact/other",
            "policy/p",
        }
        assert payload["frozen_input"]["unresolved_invalidated_claim_ids"] == ["fact/child"]
        return _review(("fact/f", "line"), ("fact/other", "line2"), ("policy/p", "POL01:v1"))

    def replace_fact(kwargs):
        payload = json.loads(kwargs["messages"][1].content)["frozen_input"]
        assert [row["id"] for row in payload["specific_claims"]] == ["fact/f"]
        assert payload["counterexamples"] == [
            {
                "claim_id": "fact/f",
                "contradicted_field": "fact",
                "verdict": "CONTRADICTED",
                "evidence_refs": ["line"],
            }
        ]
        scope = json.loads(kwargs["messages"][0].content.rsplit("\n", 1)[-1])
        assert scope["citable_evidence_ids"] == ["line"]
        return {"claims": [_claim("f", "line", value="已交付")]}

    model = Script(
        {
            f"p4:{PROMPT_VERSION}:fact:0": start(
                "fact", _call("read_order_lines", line_ids=["line", "line2"])
            ),
            f"p4:{PROMPT_VERSION}:fact:1": {
                "claims": [
                    _claim("f", "line", value="未交付"),
                    _claim("child", "line", depends=("f",)),
                    _claim("other", "line2", value="未交付"),
                ]
            },
            f"p4:{PROMPT_VERSION}:policy:0": start(
                "policy",
                _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product"),
            ),
            f"p4:{PROMPT_VERSION}:policy:1": {"claims": [_claim("p", "POL01:v1")]},
            f"p4:{PROMPT_VERSION}:verify-0:0": after_check_request if direct else first_verifier,
            f"p4:{PROMPT_VERSION}:verify-0:1": after_check_request,
            f"p4:{PROMPT_VERSION}:recheck:0": replace_fact,
            f"p4:{PROMPT_VERSION}:verify-1:0": final_verifier,
        }
    )
    result, events = _run(investigation, "MULTI", model, arm=arm, direct_recheck_request=direct)
    if arm == "multi_no_recheck":
        assert result["status"] == "NEEDS_REVIEW"
        assert result["reason"] == "INVESTIGATION_RECHECK_SUPPRESSED"
        assert result["output"]["investigation_options"] == []
        assert result["output"]["investigation_unresolved"] == [
            "原订单字段与事实判断冲突。",
            "INVESTIGATION_RECHECK_SUPPRESSED",
        ]
        assert [
            payload for kind, payload in events if kind == "investigation_recheck_suppressed"
        ] == [
            {
                "claim_ids": ["fact/f"],
                "target_role": "fact",
                "executor_role": "fact",
                "tool_name": "read_order_lines",
                "reason": "原订单字段与事实判断冲突。",
            }
        ]
        assert not any(kind == "investigation_recheck" for kind, _ in events)
        assert len(model.calls) == 6
        return
    assert result["reason"] == "INVESTIGATION_DEPENDENT_CLAIMS_UNRESOLVED"
    (revision,) = [payload for kind, payload in events if kind == "investigation_claim_revision"]
    assert [row["id"] for row in revision["retired_claims"]] == ["fact/f", "fact/child"]
    assert revision["retired_claims"][0]["value"] == "未交付"
    assert revision["replacement_claims"][0]["value"] == "已交付"
    assert revision["unresolved_stale_claim_ids"] == ["fact/child"]
    assert [
        payload["target_role"] for kind, payload in events if kind == "investigation_recheck"
    ] == ["fact"]
    if direct:
        (initial,) = [
            payload
            for kind, payload in events
            if kind == "investigation_input" and payload["phase_id"] == "verify-0"
        ]
        assert initial["recheck_request_mode"] == "structured_result"
        assert not any(payload.get("tool") == "request_targeted_check" for _, payload in events)
        (actual,) = [
            payload["receipt"]
            for kind, payload in events
            if kind == "investigation_receipt" and payload["phase_id"] == "recheck"
        ]
        assert actual["name"] == "read_order_lines"
        assert [ref["source_id"] for ref in actual["refs"]] == ["line"]
        assert f"p4:{PROMPT_VERSION}:verify-0:1" not in model.calls
        assert len(model.calls) == 7
        return
    (raw_event,) = [
        payload
        for kind, payload in events
        if kind == "investigation_receipt" and payload.get("call_id") == check_call["id"]
    ]
    assert raw_event["receipt"] == raw_check
    assert raw_event["receipt_sha256"] == digest(raw_check)
    (tool_event,) = [
        payload
        for kind, payload in events
        if kind == "investigation_tool" and payload.get("call_id") == check_call["id"]
    ]
    assert tool_event["evidence_refs"] == raw_check["refs"]
    assert tool_event["receipt_sha256"] == digest(raw_check)
    assert len(model.calls) == 8


@pytest.mark.parametrize(
    ("updates", "field", "code"),
    [
        ({"supporting_refs": ["m"]}, "supporting_refs", "UNKNOWN_SUPPORTING_EVIDENCE"),
        ({"conflicting_refs": ["POL01"]}, "conflicting_refs", "UNKNOWN_CONFLICTING_EVIDENCE"),
        ({"depends_on_claims": ["line"]}, "depends_on_claims", "UNKNOWN_CLAIM_DEPENDENCY"),
        ({"depends_on_claims": ["second"]}, "depends_on_claims", "SELF_CLAIM_DEPENDENCY"),
    ],
)
def test_claim_repair_diagnostic_only_reports_field_index_and_code(updates, field, code):
    from aftercare.intelligence.models import _validation_fields

    original = {"claims": [_claim("first", "line"), {**_claim("second", "line"), **updates}]}
    kwargs = {"role": "strong_single", "evidence_ids": {"line", "POL01:v1"}, "upstream_ids": set()}
    with pytest.raises(InvalidOutput) as caught:
        _result(json.dumps(original), InvestigationResult, **kwargs)
    assert _validation_fields(caught.value) == [{"field": f"claims.1.{field}", "code": code}]

    repaired = {
        "claims": [_claim("first", "line"), _claim("second", "POL01:v1", depends=["first"])]
    }
    assert _result(json.dumps(repaired), InvestigationResult, **kwargs)["result"]["claims"][1][
        "depends_on_claims"
    ] == ["first"]


def test_claim_cycles_rejected():
    claims = [_claim("A", "line", depends=("B",)), _claim("B", "line", depends=("A",))]
    with pytest.raises(InvalidOutput, match="CYCLIC_CLAIM_DEPENDENCY"):
        _result(
            json.dumps({"claims": claims}),
            InvestigationResult,
            role="fact",
            evidence_ids={"line"},
            upstream_ids=set(),
        )


def test_chat_private_transport_uses_no_inherited_callbacks(monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage
    from langsmith import tracing_context
    from langsmith.utils import tracing_is_enabled

    from aftercare.intelligence import models

    calls = []
    schema = {"type": "function", "function": {"name": "read_order_lines", "parameters": {}}}

    class Client:
        def bind_tools(self, tools):
            return SimpleNamespace(kwargs={"tools": tools})

        def _generate(self, messages, run_manager, **kwargs):
            assert not tracing_is_enabled()
            assert run_manager is None
            assert kwargs == {"tools": [schema]}
            calls.append(messages[0].content)
            message = AIMessage(content="{}")
            return SimpleNamespace(
                generations=[SimpleNamespace(message=message, generation_info={})]
            )

        def invoke(self, *_):
            pytest.fail("chat used callback-enabled invoke")

    monkeypatch.setattr(models, "_client", lambda *_: Client())
    with tracing_context(enabled=True):
        models._private_transport(None, [HumanMessage(content="PRIVATE_FIXTURE")], [schema], 3, 900)
    assert calls == ["PRIVATE_FIXTURE"]


def test_compact_evidence_roundtrip_preserves_records_scope_and_duplicate_verifier_reads(
    investigation,
):
    from aftercare.intelligence.models import ModelFailure

    projection = PromptEvidence(investigation)
    scope = [investigation.conversation_id, investigation.issue_id, investigation.case_id]

    def unpack(row):
        ref = row["ref"]
        return {
            **row,
            "ref": {
                "source_kind": ref[0],
                "source_id": ref[1],
                "version_or_sha256": ref[2],
                "observed_at": ref[3] if len(ref) == 4 else None,
                "scope": scope,
            },
        }

    actual = InvestigationTools(investigation)
    raw = actual.call(
        "policy",
        "read_policy_clause",
        {
            "policy_id": "POL01",
            "version": "v1",
            "product_id": "product",
        },
    ).model_dump(mode="json")
    raw["data"]["retrieval"] = {
        "mode": "HYBRID",
        "retrieval_evidence": {"query_vector_sha256": "actual-audit"},
    }
    original = raw["data"]["records"][0]
    original["ref"]["observed_at"] = raw["refs"][0]["observed_at"] = "2026-11-11T00:00:00+00:00"
    projected, _ = projection.receipt(raw)
    assert [unpack(row) for row in projected["data"]["records"]] == raw["data"]["records"]
    assert projected["data"]["retrieval"] == {
        "mode": "HYBRID",
        "proof_sha256": digest(raw["data"]["retrieval"]),
    }
    assert "refs" not in projected
    empty = {**raw, "refs": [], "data": {"records": []}}
    assert projection.receipt(empty)[0]["data"] == {"records": []}
    with pytest.raises(ModelFailure, match="INVESTIGATION_EVIDENCE_INCONSISTENT"):
        projection.receipt({**empty, "refs": raw["refs"]})
    customer_view = investigation.role_view("strong_single")
    compact = projection.prompt(customer_view)
    assert compact["scope"] == scope
    assert [unpack(row) for row in compact["customer_statements"]] == customer_view[
        "customer_statements"
    ]
    verifier_view = {"original_frozen_evidence": [original], "original_read_receipts": [raw, raw]}
    verifier = projection.prompt(verifier_view)
    assert [unpack(row) for row in verifier["original_frozen_evidence"]] == [original]
    assert len(verifier["original_read_receipts"]) == 2
    assert all(
        item["refs"] == [verifier["original_frozen_evidence"][0]["ref"]]
        and "records" not in item["data"]
        for item in verifier["original_read_receipts"]
    )
    next_version = {
        **original,
        "ref": {**original["ref"], "version_or_sha256": "v2"},
        "data": {"clause": {"text": "另一版本原文"}},
    }
    separately_versioned = projection.prompt(
        {**verifier_view, "original_frozen_evidence": [original, next_version]}
    )
    assert [unpack(row) for row in separately_versioned["original_frozen_evidence"]] == [
        original,
        next_version,
    ]
    # The data owner supplied a contradictory value for the same immutable reference.
    conflicting = {**original, "data": {"clause": {"text": "被改动的文本"}}}
    with pytest.raises(ModelFailure, match="INVESTIGATION_EVIDENCE_INCONSISTENT"):
        projection.prompt({**verifier_view, "original_frozen_evidence": [conflicting]})
    with pytest.raises(ModelFailure, match="INVESTIGATION_EVIDENCE_INCONSISTENT"):
        projection.ref({**original["ref"], "scope": ["different", *scope[1:]]})


def test_targeted_check_compaction_keeps_new_or_extra_evidence_and_other_tools(investigation):
    tools = InvestigationTools(investigation)
    projection = PromptEvidence(investigation)
    check = _call(
        "request_targeted_check",
        claim_id="fact/f",
        conflicting_refs=["line2"],
        target_tool="read_order_lines",
        reason="核对另一订单行。",
    )
    receipt = tools.call("verifier", check["name"], check["args"]).model_dump(mode="json")
    assert _model_tool_receipt(projection, check, receipt, {"line2"}, digest(receipt)) == {
        "status": "REQUESTED",
        "receipt_sha256": digest(receipt),
    }
    assert (
        _model_tool_receipt(
            projection,
            check,
            receipt,
            {"line"},
            digest(receipt),
        )
        == projection.receipt(receipt)[0]
    )
    extra = {**receipt, "data": {**receipt["data"], "business_result": "保留新业务数据"}}
    assert (
        _model_tool_receipt(
            projection,
            check,
            extra,
            {"line2"},
            digest(extra),
        )
        == projection.receipt(extra)[0]
    )
    line_call = _call("read_order_lines", line_ids=["line2"])
    line_receipt = tools.call("fact", line_call["name"], line_call["args"]).model_dump(mode="json")
    assert (
        _model_tool_receipt(
            projection,
            line_call,
            line_receipt,
            {"line2"},
            digest(line_receipt),
        )
        == projection.receipt(line_receipt)[0]
    )


def test_compact_prompt_overflow_is_audited_before_transport(investigation):
    model = Script({})
    expanded = investigation.model_copy(
        update={
            "messages": (
                investigation.messages[0].model_copy(update={"text": "无法缩写的用户陈述" * 3000}),
            )
        }
    )
    outcome, events = _run(expanded, "SINGLE", model)
    assert outcome["reason"] == "INVESTIGATION_PROMPT_LIMIT"
    assert not model.calls
    (budget,) = [payload for kind, payload in events if kind == "investigation_prompt_budget"]
    assert budget["preflight"] == "OVER_LIMIT"
    assert budget["estimated_without_tools"] > budget["limit"] == 24_576


def test_p4_current_batch_limit_in_prompt_rejects_before_any_tool_read(investigation):
    from aftercare.intelligence.models import _validation_fields

    def excess(kwargs):
        assert "本轮最多调用 4 个工具" in kwargs["messages"][0].content
        assert "剩余 8 次工具读取" in kwargs["messages"][0].content
        assert kwargs["output_reserve"] == 1800
        return [_call("read_order_lines", line_ids=["line"])] * 5

    class NoReads(InvestigationTools):
        def call(self, *_):
            pytest.fail("oversize batch issued a tool read")

    with pytest.raises(InvalidOutput) as error:
        _run(
            investigation,
            "SINGLE",
            Script({f"p4:{PROMPT_VERSION}:single:0": excess}),
            tools=NoReads(investigation),
        )
    assert _validation_fields(error.value) == [
        {
            "field": "tool_calls",
            "code": "TOOL_BATCH_LIMIT",
            "attempted": 5,
            "allowed": 4,
        }
    ]


def test_historical_policy_option_is_preserved_with_exact_version_rejection(investigation):
    from aftercare.investigation.graph import _publish_options

    selected = {"id": "POL01", "version": "v1"}
    historical = {"id": "POL01", "version": "v0", "text": "原历史政策"}
    value = investigation.model_copy(
        update={
            "evidence": (
                *investigation.evidence,
                Evidence(
                    ref=_ref("POLICY", "POL01:v0"), trust="POLICY", data={"clause": historical}
                ),
            )
        }
    )
    audit = Audit()
    ctx = InvestigationContext(
        value,
        None,
        None,
        audit,
        None,
        lambda **_: pytest.fail("wrong version reached validator"),
        "analysis",
        frozenset({"line"}),
        frozenset({"line", "POL01:v0", "POL01:v1"}),
        policy_authority={
            "order": {"items": [{"line_id": "line", "product_id": "product"}]},
            "policy_catalogue": {"documents": [selected, historical]},
            "policies": {"by_product": {"product": [selected]}},
        },
    )
    result = _publish_options(
        ctx,
        {
            "claims": [_claim("claim", "POL01:v0")],
            "options": [
                {
                    "id": "old-version-option",
                    "action": "REFUND",
                    "lines": [{"line_id": "line", "amount_cents": 100}],
                    "policy_ids": ["POL01"],
                    "evidence_ids": ["line", "POL01:v0"],
                    "rationale": "历史版候选",
                }
            ],
        },
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["output"]["action"] is None
    assert result["output"]["investigation_options"][0]["lines"][0]["amount_cents"] == 100
    assert result["output"]["investigation_unresolved"]
    (event,) = [payload for kind, payload in audit.events if kind == "investigation_result"]
    assert event["program_checks"] == {
        "old-version-option": "INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE"
    }


def test_targeted_policy_recheck_can_read_another_frozen_version(investigation):
    from types import SimpleNamespace

    from aftercare.investigation.catalogue import CATALOGUE_VERSION
    from aftercare.investigation.graph import _publish_options, _recheck

    alternate = Evidence(
        ref=_ref("POLICY", "POL01:v0"),
        trust="POLICY",
        data={"clause": {"id": "POL01", "version": "v0", "text": "独立旧版全文"}},
    )
    value = investigation.model_copy(
        update={
            "policy_catalogue_version": CATALOGUE_VERSION,
            "policy_context": {
                "products": [{"id": "product", "category": None}],
                "catalogue_sha256": "f" * 64,
            },
            "evidence": (*investigation.evidence, alternate),
        }
    )

    def revised(kwargs):
        payload = json.loads(kwargs["messages"][1].content)["frozen_input"]
        scope = json.loads(kwargs["messages"][0].content.rsplit("\n", 1)[-1])
        assert scope["citable_evidence_ids"] == ["POL01:v0"]
        assert (
            payload["check_receipt"]["data"]["records"][0]["data"]["clause"]["text"]
            == "独立旧版全文"
        )
        return {"claims": [_claim("claim", "POL01:v0")]}

    script = Script({f"p4:{PROMPT_VERSION}:recheck:0": revised})
    audit = Audit()
    ctx = InvestigationContext(
        value, None, script, audit, InvestigationTools(value), None, "analysis"
    )
    result = _recheck(
        {
            "recheck": {
                "target_role": "policy",
                "claim_ids": ["policy/claim"],
                "tool_name": "read_policy_clause",
                "arguments": {"policy_id": "POL01", "version": "v0", "product_id": "product"},
                "reason": "核查同条款旧版的适用期。",
            },
            "active_claims": [
                {
                    **_claim("claim", "POL01:v1"),
                    "id": "policy/claim",
                    "role": "policy",
                    "conflicting_refs": [],
                }
            ],
        },
        SimpleNamespace(context=ctx),
    )
    assert result["path"] == "MULTI" and result["recheck_count"] == 1
    assert result["active_claims"][0]["supporting_refs"] == ["POL01:v0"]
    assert result["evidence_ids"] == ["POL01:v0"]
    assert result["retired_claims"][0]["supporting_refs"] == ["POL01:v1"]
    assert script.calls == [f"p4:{PROMPT_VERSION}:recheck:0"]
    # Even a previously trusted record cannot support a candidate once its only
    # associated claim has been replaced and it is outside the new evidence scope.
    ctx.line_ids = frozenset({"line"})
    ctx.trusted_evidence_ids = frozenset({"line", "POL01:v0", "POL01:v1"})
    ctx.validate_draft = lambda **_: pytest.fail("Retired evidence must never reach validation")
    published = _publish_options(
        ctx,
        {
            "options": [
                {
                    "id": "old",
                    "action": "REFUND",
                    "lines": [{"line_id": "line", "amount_cents": 100}],
                    "policy_ids": ["POL01"],
                    "rationale": "引用已退出本轮有效范围的政策。",
                    "evidence_ids": ["line", "POL01:v1"],
                }
            ],
        },
        claims=result["active_claims"],
        evidence_scope=["line", *result["evidence_ids"]],
    )
    assert published["reason"] == "INVESTIGATION_RULE_OR_EVIDENCE_BLOCKED"
    assert published["output"]["investigation_options"] == []


def test_targeted_order_read_does_not_fetch_unrelated_order_line(investigation):
    from aftercare.investigation.graph import _recheck

    class NoReads(InvestigationTools):
        def call(self, *_args, **_kwargs):
            pytest.fail("A scope-invalid request must not fetch order lines")

    ctx = InvestigationContext(
        investigation, None, None, Audit(), NoReads(investigation), None, "analysis"
    )
    result = _recheck(
        {
            "recheck": {
                "target_role": "fact",
                "claim_ids": ["fact/one"],
                "tool_name": "read_order_lines",
                "arguments": {"line_ids": ["line", "line2"]},
                "reason": "仅第一件商品的订单字段与原判断冲突。",
            },
            "active_claims": [
                {**_claim("one", "line"), "id": "fact/one", "role": "fact", "conflicting_refs": []},
                {
                    **_claim("two", "line2"),
                    "id": "fact/two",
                    "role": "fact",
                    "conflicting_refs": [],
                },
            ],
        },
        SimpleNamespace(context=ctx),
    )
    assert result["reason"] == "INVESTIGATION_RECHECK_SCOPE_INVALID"


@pytest.mark.parametrize("requested", [["target-material"], ["target-material", "other-material"]])
def test_targeted_material_read_uses_only_target_counterexample(investigation, requested):
    from aftercare.investigation.graph import _recheck

    value = investigation.model_copy(
        update={
            "evidence": (
                *investigation.evidence,
                *(
                    Evidence(ref=_ref("MATERIAL", ident), trust="VERIFIED_RECORD", data={})
                    for ident in ("target-material", "other-material")
                ),
            )
        }
    )
    script = Script(
        {
            f"p4:{PROMPT_VERSION}:recheck:0": {"claims": [_claim("one", "target-material")]},
        }
    )
    audit = Audit()
    ctx = InvestigationContext(
        value, None, script, audit, InvestigationTools(value), None, "analysis"
    )
    assessments = _review(("fact/one", "target-material"), ("fact/two", "other-material"))
    for assessment in assessments["assessments"]:
        assessment["verdict"] = "CONTRADICTED"
    result = _recheck(
        {
            "assessment": assessments,
            "recheck": {
                "target_role": "fact",
                "claim_ids": ["fact/one"],
                "tool_name": "read_material_evidence",
                "arguments": {"evidence_ids": requested},
                "reason": "核对与第一件商品主张相冲突的材料。",
            },
            "active_claims": [
                {**_claim("one", "line"), "id": "fact/one", "role": "fact", "conflicting_refs": []},
                {
                    **_claim("two", "line2"),
                    "id": "fact/two",
                    "role": "fact",
                    "conflicting_refs": [],
                },
            ],
        },
        SimpleNamespace(context=ctx),
    )
    if len(requested) == 1:
        assert result["recheck_count"] == 1
        assert result["active_claims"][-1]["supporting_refs"] == ["target-material"]
    else:
        assert result["reason"] == "INVESTIGATION_RECHECK_SCOPE_INVALID"
        assert script.calls == audit.events == []


@pytest.mark.parametrize("linked_line", ["line", "line2"])
def test_targeted_execution_read_requires_frozen_line_binding(investigation, linked_line):
    from aftercare.investigation.graph import _recheck

    execution_id = "12345678-1234-1234-1234-123456789abc"
    value = investigation.model_copy(
        update={
            "evidence": (
                *investigation.evidence,
                Evidence(
                    ref=_ref("EXECUTION", execution_id),
                    trust="VERIFIED_RECORD",
                    data={
                        "id": execution_id,
                        "action": "REFUND",
                        "status": "SUCCEEDED",
                        "amount_cents": 100,
                        "created_at": "2026-11-12",
                    },
                ),
            ),
            "refund_execution_line_ids": {execution_id: (linked_line,)},
        },
    )

    def revise(kwargs):
        payload = json.loads(kwargs["messages"][1].content)["frozen_input"]
        execution = payload["check_receipt"]["data"]["execution"]
        assert execution["scoped_line_ids"] == ["line"]
        assert execution["amount_scope"] == "ENTIRE_EXECUTION"
        return {"claims": [_claim("one", execution_id, value="部分已退")]}

    script = Script({f"p4:{PROMPT_VERSION}:recheck:0": revise})
    audit = Audit()
    ctx = InvestigationContext(
        value, None, script, audit, InvestigationTools(value), None, "analysis"
    )
    result = _recheck(
        {
            "recheck": {
                "target_role": "fact",
                "claim_ids": ["fact/one"],
                "tool_name": "read_refund_execution",
                "arguments": {"execution_id": execution_id},
                "reason": "核对目标商品是否有一笔已执行的部分退款。",
            },
            "active_claims": [
                {**_claim("one", "line"), "id": "fact/one", "role": "fact", "conflicting_refs": []},
            ],
        },
        SimpleNamespace(context=ctx),
    )
    if linked_line == "line":
        assert result["active_claims"][0]["supporting_refs"] == [execution_id]
        assert result["recheck_count"] == 1
    else:
        assert result["reason"] == "INVESTIGATION_RECHECK_SCOPE_INVALID"
        assert script.calls == audit.events == []


@pytest.mark.parametrize("requested", [["line"], ["line", "line2"]])
def test_policy_claim_recheck_dispatches_to_fact_tool_owner(investigation, requested):
    from aftercare.investigation.graph import _recheck

    calls = []

    class RecordingTools(InvestigationTools):
        def call(self, role, name, args):
            calls.append((role, name, args))
            return super().call(role, name, args)

    def revise(kwargs):
        payload = json.loads(kwargs["messages"][1].content)["frozen_input"]
        assert [row["id"] for row in payload["specific_claims"]] == ["policy/one"]
        prior = payload["prior_target_evidence"]
        assert [row["ref"]["source_id"] for row in prior] == ["POL01:v1"]
        assert prior[0]["data"]
        assert kwargs["tools"] == []
        return {"claims": [{**_claim("one", "line"), "supporting_refs": ["line", "POL01:v1"]}]}

    model = Script({f"p4:{PROMPT_VERSION}:recheck:0": revise})
    audit = Audit()
    ctx = InvestigationContext(
        investigation, None, model, audit, RecordingTools(investigation), None, "analysis"
    )
    assessments = _review(("policy/one", "line"))
    assessments["assessments"][0]["verdict"] = "CONTRADICTED"
    result = _recheck(
        {
            "assessment": assessments,
            "recheck": {
                "target_role": "policy",
                "claim_ids": ["policy/one"],
                "tool_name": "read_order_lines",
                "arguments": {"line_ids": requested},
                "reason": "核对适用政策依赖的商品行事实。",
            },
            "active_claims": [
                {
                    **_claim("one", "POL01:v1"),
                    "id": "policy/one",
                    "role": "policy",
                    "conflicting_refs": [],
                },
            ],
            "policy_result": {"evidence_ids": ["POL01:v1"]},
        },
        SimpleNamespace(context=ctx),
    )
    if requested == ["line"]:
        assert calls == [("fact", "read_order_lines", {"line_ids": ["line"]})]
        event = next(payload for kind, payload in audit.events if kind == "investigation_recheck")
        assert (event["target_role"], event["executor_role"]) == ("policy", "fact")
        assert result["active_claims"][0]["supporting_refs"] == ["line", "POL01:v1"]
    else:
        assert result["reason"] == "INVESTIGATION_RECHECK_SCOPE_INVALID"
        assert calls == model.calls == audit.events == []


def test_verifier_recheck_schema_and_validation_repair(investigation):
    from pydantic import ValidationError

    from aftercare.investigation.orchestration import run_phase

    valid = _review(
        ("policy/one", "line"),
        request={
            "target_role": "policy",
            "claim_ids": ["policy/one"],
            "tool_name": "read_order_lines",
            "arguments": {"line_ids": ["line"]},
            "reason": "核对适用政策依赖的商品行事实。",
        },
    )
    valid["assessments"][0]["verdict"] = "CONTRADICTED"

    class RepairingModel:
        def call(self, _audit, _logical_id, **kwargs):
            system = kwargs["messages"][0].content
            schema = json.JSONDecoder().raw_decode(system[system.index('{"$defs":') :])[0]
            definitions = schema["$defs"]
            rules = schema["properties"]["recheck"]["anyOf"][0]["allOf"]
            expected_fields = {
                "read_order_lines": {"line_ids"},
                "read_material_evidence": {"evidence_ids"},
                "read_refund_execution": {"execution_id"},
                "search_applicable_policy": {"query", "condition_keys"},
                "read_policy_clause": {"policy_id", "version", "product_id"},
            }
            for name, fields in expected_fields.items():
                branch = next(
                    rule for rule in rules if rule["if"]["properties"]["tool_name"]["const"] == name
                )
                ref = branch["then"]["properties"]["arguments"]["$ref"]
                arg = definitions[ref.rsplit("/", 1)[-1]]
                assert arg["additionalProperties"] is False
                assert set(arg["properties"]) == fields
            invalid = {
                **valid,
                "recheck": {
                    **valid["recheck"],
                    "arguments": {"line_ids": ["line"], "order_id": "order"},
                },
            }
            with pytest.raises(ValidationError) as caught:
                kwargs["validate"]([], json.dumps(invalid))
            assert caught.value.errors()[0]["type"] == "extra_forbidden"
            assert caught.value.errors()[0]["loc"][-1] == "order_id"
            return kwargs["validate"]([], json.dumps(valid))

    ctx = InvestigationContext(
        investigation,
        None,
        RepairingModel(),
        Audit(),
        InvestigationTools(investigation),
        None,
        "analysis",
        protocol=HUMAN_PAYMENT_V7,
    )
    result, *_ = run_phase(
        ctx,
        "verifier",
        "verify-0",
        prompt={},
        known_evidence=["line"],
        upstream=[
            {
                **_claim("one", "POL01:v1"),
                "id": "policy/one",
                "role": "policy",
                "conflicting_refs": [],
            }
        ],
        max_rounds=1,
        tools_enabled=False,
    )
    assert result["recheck"] == valid["recheck"]


def test_final_verifier_requires_null_recheck(investigation):
    from aftercare.investigation.orchestration import run_phase

    def verify(kwargs):
        system = kwargs["messages"][0].content
        schema = json.JSONDecoder().raw_decode(system[system.index('{"$defs":') :])[0]
        assert schema["properties"]["recheck"]["type"] == "null"
        assert kwargs["tools"] == []
        result = _review(("fact/one", "line"))
        for recheck in (
            {
                "target_role": "fact",
                "tool_name": "read_order_lines",
                "arguments": {"line_ids": ["line"]},
            },
            "malformed",
        ):
            with pytest.raises(InvalidOutput, match="RECHECK_NOT_ALLOWED_IN_FINAL") as error:
                kwargs["validate"]([], json.dumps({**result, "recheck": recheck}))
            assert error.value.field == "recheck"
        return {**result, "recheck": None}

    ctx = InvestigationContext(
        investigation,
        None,
        Script({"p4:compact-v7:verify-1:0": verify}),
        Audit(),
        InvestigationTools(investigation),
        None,
        "analysis",
        protocol=HUMAN_PAYMENT_V7,
    )
    result, *_ = run_phase(
        ctx,
        "verifier",
        "verify-1",
        prompt={},
        known_evidence=["line"],
        upstream=[
            {**_claim("one", "line"), "id": "fact/one", "role": "fact", "conflicting_refs": []}
        ],
        max_rounds=1,
        tools_enabled=False,
    )
    assert result["recheck"] is None


def test_v7_tool_receipts_link_previously_shown_records(investigation):
    from copy import deepcopy

    from aftercare.intelligence.models import ModelFailure

    projection = PromptEvidence(investigation, protocol=HUMAN_PAYMENT_V7)
    view = projection.prompt(investigation.role_view("strong_single"))
    assert view["receipt_encoding"] == "phase-record-links-v1"
    receipt = (
        InvestigationTools(investigation)
        .call("strong_single", "read_order_lines", {"line_ids": ["line2"]})
        .model_dump(mode="json")
    )
    original = deepcopy(receipt)
    first = projection.tool_receipt(receipt)["data"]
    second = projection.tool_receipt(receipt)["data"]
    assert first["records"]
    assert second == {
        "records": [],
        "record_links": [[index, row["ref"]] for index, row in enumerate(first["records"])],
    }
    assert receipt == original

    conflicting = deepcopy(receipt)
    conflicting["data"]["records"][0]["data"]["unexpected"] = True
    with pytest.raises(ModelFailure, match="INVESTIGATION_EVIDENCE_INCONSISTENT"):
        projection.tool_receipt(conflicting)


def test_v7_output_schema_omits_only_schema_titles():
    from aftercare.investigation.orchestration import _prompt_schema

    schema = {
        "title": "Root",
        "properties": {
            "title": {"title": "Label", "enum": ["x"]},
            "other": {"anyOf": [{"title": "Nested", "const": {"title": "data"}}]},
        },
        "required": ["title"],
    }
    assert _prompt_schema(schema) == {
        "properties": {
            "title": {"enum": ["x"]},
            "other": {"anyOf": [{"const": {"title": "data"}}]},
        },
        "required": ["title"],
    }
    assert schema["title"] == "Root"


def test_extended_reserve_uses_only_bound_v7_value_in_provider_call_and_trace(investigation):
    from aftercare.intelligence.models import ModelFailure
    from aftercare.intelligence.service import _bound_output_reserve

    assert _bound_output_reserve({}, LEGACY_V6) == 1800
    assert _bound_output_reserve({}, HUMAN_PAYMENT_V7) == 1800
    binding = {"investigation_output_reserve": 4096}
    with pytest.raises(ModelFailure, match="INVESTIGATION_OUTPUT_RESERVE_INVALID"):
        _bound_output_reserve(binding, LEGACY_V6)
    with pytest.raises(ModelFailure, match="INVESTIGATION_OUTPUT_RESERVE_INVALID"):
        _bound_output_reserve({"investigation_output_reserve": "4096"}, HUMAN_PAYMENT_V7)

    def complete(kwargs):
        assert kwargs["output_reserve"] == 4096
        return {"claims": [], "options": [], "unresolved": ["missing evidence"]}

    model = Script({"p4:compact-v7:single:0": complete})
    _, events = _run(
        investigation,
        "SINGLE",
        model,
        protocol=HUMAN_PAYMENT_V7,
        output_reserve=_bound_output_reserve(binding, HUMAN_PAYMENT_V7),
    )
    (trace,) = [row for kind, row in events if kind == "investigation_prompt_budget"]
    assert trace["max_output_tokens"] == 4096


def test_v7_visible_action_schema_explains_native_candidate_rejections():
    from pydantic import ValidationError

    from aftercare.investigation.orchestration import (
        SynthesisResult,
        VerificationResult,
        _v7_prompt_schema,
    )

    for result in (InvestigationResult, VerificationResult, SynthesisResult):
        original = result.model_json_schema()
        defs = _v7_prompt_schema(original)["$defs"]
        option = defs["Option"]
        assert "if" not in original["$defs"]["Option"]
        assert option["if"] == {"properties": {"action": {"const": "REFUND"}}}
        for branch, field, forbidden in (
            ("then", "amount_cents", "quantity"),
            ("else", "quantity", "amount_cents"),
        ):
            line = option[branch]["properties"]["lines"]["items"]
            assert line["required"] == [field]
            assert line["properties"][forbidden] == {"type": "null"}
            assert line["properties"][field]["type"] == "integer"
            assert line["properties"][field]["exclusiveMinimum"] == 0
        request = defs["NonExecutingOption"]
        assert request["if"]["properties"]["action"] == {"const": "REQUEST_EVIDENCE"}
        assert request["then"] == {
            "properties": {"missing_evidence": {"minItems": 1}},
            "required": ["missing_evidence"],
        }

    base = {"id": "x", "evidence_ids": ["line"], "rationale": "source evidence"}
    refund = {**base, "action": "REFUND", "policy_ids": ["POL01"]}
    replace = {**refund, "action": "REPLACE"}
    for option, code in (
        ({**refund, "lines": [{"line_id": "line"}]}, "REFUND_AMOUNT_REQUIRED"),
        ({**replace, "lines": [{"line_id": "line"}]}, "REPLACE_QUANTITY_REQUIRED"),
        (
            {**base, "action": "REQUEST_EVIDENCE", "target_line_ids": ["line"]},
            "EVIDENCE_GAP_REQUIRED",
        ),
    ):
        with pytest.raises(ValidationError) as error:
            InvestigationResult.model_validate({"claims": [], "options": [option]})
        assert error.value.errors()[0]["type"] == code
    assert InvestigationResult.model_validate(
        {
            "claims": [],
            "options": [{**refund, "lines": [{"line_id": "line", "amount_cents": 4900}]}],
        }
    ).options[0].business_lines() == [{"line_id": "line", "amount_cents": 4900}]


def test_v7_shared_prompt_constraints_expand_to_same_schema():
    from aftercare.investigation.orchestration import (
        SynthesisResult,
        VerificationResult,
        _share_prompt_constraints,
        _v7_prompt_schema,
    )

    for model in (InvestigationResult, VerificationResult, SynthesisResult):
        full = _v7_prompt_schema(model.model_json_schema())
        compact = _share_prompt_constraints(full)
        shared = {name: compact["$defs"][name] for name in ("K", "P", "T")}

        def expand(value, shared=shared):
            if isinstance(value, list):
                return [expand(item, shared) for item in value]
            if isinstance(value, dict):
                ref = value.get("$ref")
                if len(value) == 1 and ref in {f"#/$defs/{key}" for key in shared}:
                    return shared[ref.rsplit("/", 1)[1]]
                return {key: expand(item, shared) for key, item in value.items()}
            return value

        expanded = expand(compact)
        for name in shared:
            del expanded["$defs"][name]
        assert expanded == full


def test_v7_verifier_indexes_only_full_matching_refs_and_roundtrips_claims(investigation):
    from aftercare.investigation.prompt_evidence import verifier_claims

    tools = InvestigationTools(investigation)
    receipt = tools.call(
        "policy",
        "read_policy_clause",
        {"policy_id": "POL01", "version": "v1", "product_id": "product"},
    ).model_dump(mode="json")
    corpus = "f" * 64
    matching = receipt["data"]["records"][0]
    matching["data"]["catalogue_sha256"] = corpus
    foreign = {
        **matching,
        "ref": {**matching["ref"], "source_id": "POL02:v1"},
        "data": {**matching["data"], "catalogue_sha256": "e" * 64},
    }
    view = {
        **investigation.role_view("verifier"),
        "policy_context": {"catalogue_sha256": corpus},
        "original_frozen_evidence": [matching, foreign],
        "original_read_receipts": [receipt, receipt],
        "available_evidence": [
            receipt["refs"][0],
            {**receipt["refs"][0], "version_or_sha256": "other"},
        ],
    }
    legacy = PromptEvidence(investigation).prompt(view)
    compact = PromptEvidence(investigation, protocol=HUMAN_PAYMENT_V7).prompt(view)
    assert compact["verifier_encoding"] == "verifier-record-index-v2"
    encoded_records = compact["original_frozen_evidence"]
    records = [
        dict(zip(encoded_records["columns"], row, strict=True)) for row in encoded_records["rows"]
    ]
    assert compact["policy_corpus_rows"] == [0]
    assert "catalogue_sha256" not in records[0]["data"]
    assert records[1]["data"]["catalogue_sha256"] == "e" * 64
    for position in compact["policy_corpus_rows"]:
        records[position]["data"]["catalogue_sha256"] = compact["policy_context"][
            "catalogue_sha256"
        ]
    assert records == legacy["original_frozen_evidence"]

    def expand(refs):
        return [records[ref]["ref"] if type(ref) is int else ref for ref in refs]

    assert compact["available_evidence"][0] == 0
    assert compact["available_evidence"][1] == legacy["available_evidence"][1]
    assert expand(compact["available_evidence"]) == legacy["available_evidence"]
    assert [
        {**row, "refs": expand(row["refs"])} for row in compact["original_read_receipts"]
    ] == legacy["original_read_receipts"]
    claims = [
        {
            **_claim("first", "POL01:v1"),
            "id": "policy/first",
            "role": "policy",
            "conflicting_refs": [],
        },
        {
            **_claim("second", "m", depends=("policy/first",)),
            "subject": "POL01:v1",
            "id": "fact/second",
            "role": "fact",
            "conflicting_refs": ["unknown-fallback"],
        },
    ]
    refs = {"m": ["message", 0], "POL01:v1": 0}
    encoded = verifier_claims(claims, refs)
    assert "role" not in encoded["columns"]

    def decode(ref):
        if type(ref) is int:
            return records[ref]["ref"][1]
        if ref == ["message", 0]:
            return "m"
        return ref

    decoded = []
    for row in encoded["rows"]:
        claim = dict(zip(encoded["columns"], row, strict=True))
        claim["role"] = claim["id"].split("/", 1)[0]
        claim["subject"] = decode(claim["subject"])
        for key in ("supporting_refs", "conflicting_refs"):
            if key in claim:
                claim[key] = [decode(ref) for ref in claim[key]]
        decoded.append(claim)
    assert decoded == claims


def test_v7_strong_receipts_reuse_exact_available_ref_and_corpus(investigation):
    raw = (
        InvestigationTools(investigation)
        .call(
            "strong_single",
            "read_policy_clause",
            {"policy_id": "POL01", "version": "v1", "product_id": "product"},
        )
        .model_dump(mode="json")
    )
    record = raw["data"]["records"][0]
    corpus = "a" * 64
    record["data"]["catalogue_sha256"] = corpus
    projection = PromptEvidence(investigation, protocol=HUMAN_PAYMENT_V7, index_reads=True)
    view = projection.prompt(
        {
            "available_evidence": raw["refs"],
            "policy_context": {"catalogue_sha256": corpus},
        }
    )
    assert view["tool_record_encoding"] == "tool-record-index-v1"
    first = projection.tool_receipt(raw)["data"]
    assert first["records"][0]["ref"] == 0
    assert first["policy_corpus_rows"] == [0]
    assert first["records"][0]["data"] == {
        key: value for key, value in record["data"].items() if key != "catalogue_sha256"
    }
    assert projection.tool_receipt(raw)["data"]["record_links"] == [[0, 0]]
    restored = {
        **first["records"][0],
        "ref": view["available_evidence"][0],
        "data": {**first["records"][0]["data"], "catalogue_sha256": corpus},
    }
    assert restored == projection.record(record)


def test_targeted_request_does_not_make_unread_refs_citable(investigation):
    from aftercare.investigation.orchestration import run_phase

    check = _call(
        "request_targeted_check",
        claim_id="fact/f",
        conflicting_refs=["line2"],
        target_tool="read_order_lines",
        reason="核对另一订单行。",
    )

    def verify(kwargs):
        scope = json.loads(kwargs["messages"][0].content.rsplit("\n", 1)[-1])
        assert scope["citable_evidence_ids"] == ["line"]
        assert json.loads(kwargs["messages"][-1].content)["refs"][0][1] == "line2"
        result = _review(("fact/f", "line"))
        result["assessments"][0]["evidence_refs"] = ["line2"]
        return result

    model = Script({"p4:compact-v7:verify-0:0": [check], "p4:compact-v7:verify-0:1": verify})
    ctx = InvestigationContext(
        investigation,
        None,
        model,
        Audit(),
        InvestigationTools(investigation),
        None,
        "analysis",
        protocol=HUMAN_PAYMENT_V7,
    )
    with pytest.raises(InvalidOutput, match="VERIFICATION_EVIDENCE_OUT_OF_SCOPE"):
        run_phase(
            ctx,
            "verifier",
            "verify-0",
            prompt=investigation.role_view("verifier"),
            known_evidence=["line"],
            upstream=[_claim("fact/f", "line")],
            max_rounds=2,
            tool_limit=1,
        )


@pytest.mark.parametrize(
    ("rows", "code", "field"),
    [
        ([("fact/visible", "line")], "VERIFICATION_MISSING_CLAIM", "assessments"),
        (
            [("fact/visible", "line"), ("fact/visible", "line")],
            "VERIFICATION_DUPLICATE_CLAIM",
            "assessments.1.claim_id",
        ),
        (
            [("fact/visible", "line"), ("fact/untrusted-output", "line")],
            "VERIFICATION_UNKNOWN_CLAIM",
            "assessments.1.claim_id",
        ),
        (
            [("fact/visible", "line"), ("policy/visible", "retired-ref")],
            "VERIFICATION_EVIDENCE_OUT_OF_SCOPE",
            "assessments.1.evidence_refs",
        ),
    ],
)
def test_verifier_reports_only_safe_paths_for_coverage_and_scope(rows, code, field):
    from aftercare.intelligence.models import _validation_fields
    from aftercare.investigation.orchestration import VerificationResult

    with pytest.raises(InvalidOutput, match=code) as error:
        _result(
            json.dumps(_review(*rows)),
            VerificationResult,
            role="verifier",
            evidence_ids={"line"},
            upstream_ids={"fact/visible", "policy/visible"},
        )
    assert _validation_fields(error.value) == [{"field": field, "code": code}]


@pytest.mark.parametrize("recheck", [False, True])
def test_existing_material_is_available_for_verification_before_requesting_more(
    investigation, recheck, monkeypatch
):
    monkeypatch.setattr("aftercare.investigation.graph.MAX_READ_IDS", 1)
    initial = Evidence(
        ref=_ref("MATERIAL", "first"), trust="VERIFIED_RECORD", data={"kind": "PARCEL"}
    )
    review = Evidence(
        ref=_ref("REVIEW", "review"),
        trust="VERIFIED_RECORD",
        data={"material_ids": ["first"], "decision": "APPROVED"},
    )
    material = Evidence(
        ref=_ref("MATERIAL", "uploaded"), trust="VERIFIED_RECORD", data={"kind": "ITEM_PHOTO"}
    )
    value = investigation.model_copy(
        update={"evidence": (*investigation.evidence, initial, review, material)}
    )

    def fact_start(kwargs):
        view = json.loads(kwargs["messages"][1].content)["frozen_input"]
        scope = json.loads(kwargs["messages"][0].content.rsplit("\n", 1)[-1])
        assert "m" in scope["citable_evidence_ids"]
        assert {"first", "review"} <= set(scope["citable_evidence_ids"])
        assert "uploaded" not in scope["citable_evidence_ids"]
        assert view["initial_material_receipt"]["data"]["records"][1]["data"] == review.data
        assert "本轮最多调用 1 个工具" in kwargs["messages"][0].content
        return [_call("read_order_lines", line_ids=["line"])]

    def policy_start(kwargs):
        view = json.loads(kwargs["messages"][1].content)["frozen_input"]
        assert "initial_material_receipt" not in view
        return [
            _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product"),
        ]

    def verify(kwargs):
        view = json.loads(kwargs["messages"][1].content)["frozen_input"]
        assert view["customer_statements"][0]["ref"][1] == "m"
        assert view["unread_material_refs"] == [["MATERIAL", "uploaded", "v1"]]
        result = _review(("fact/photo", "uploaded"), ("policy/p", "POL01:v1"))
        result["assessments"][0]["verdict"] = "CONTRADICTED"
        if recheck:
            result["recheck"] = {
                "target_role": "fact",
                "claim_ids": ["fact/photo"],
                "tool_name": "read_material_evidence",
                "arguments": {"evidence_ids": ["uploaded"]},
                "reason": "目录显示已有材料，读取记录核实。",
            }
        else:
            result["options"] = [
                {
                    "id": "duplicate",
                    "action": "REQUEST_EVIDENCE",
                    "target_line_ids": ["line"],
                    "rationale": "等待照片。",
                    "missing_evidence": ["商品照片"],
                    "evidence_ids": ["m"],
                }
            ]
        return result

    def final(kwargs):
        view = json.loads(kwargs["messages"][1].content)["frozen_input"]
        assert "unread_material_refs" not in view
        return _review(("fact/photo", "uploaded"), ("policy/p", "POL01:v1"))

    script = Script(
        {
            f"p4:{PROMPT_VERSION}:fact:0": fact_start,
            f"p4:{PROMPT_VERSION}:fact:1": {"claims": [_claim("photo", "m", value="没有材料")]},
            f"p4:{PROMPT_VERSION}:policy:0": policy_start,
            f"p4:{PROMPT_VERSION}:policy:1": {"claims": [_claim("p", "POL01:v1")]},
            f"p4:{PROMPT_VERSION}:verify-0:0": verify,
            f"p4:{PROMPT_VERSION}:recheck:0": {
                "claims": [_claim("photo", "uploaded", value="已有材料")]
            },
            f"p4:{PROMPT_VERSION}:verify-1:0": final,
        }
    )
    state, events = _run(value, "MULTI", script)
    assert state["output"]["investigation_options"] == []
    assert state["fact_result"]["tool_calls"] == 2
    (preload,) = [
        payload
        for kind, payload in events
        if kind == "investigation_receipt" and payload.get("origin") == "PROGRAM_REQUIRED_READ"
    ]
    assert preload["receipt_sha256"] == digest(preload["receipt"])
    assert [row["source_id"] for row in preload["receipt"]["refs"]] == ["first", "review"]
    if recheck:
        assert state["recheck_receipt"]["data"]["records"][0]["data"] == material.data
        assert state["retired_claims"][0]["value"] == "没有材料"
        assert state["active_claims"][-1]["value"] == "已有材料"
    else:
        assert state["reason"] == "INVESTIGATION_MATERIAL_NOT_READ"
        assert any(
            kind == "investigation_material_unread" and payload["evidence_ids"] == ["uploaded"]
            for kind, payload in events
        )


def test_v7_policy_fields_show_bare_ids_and_require_a_read_version(investigation):
    from aftercare.investigation.graph import _publish_options

    def check_prompt(kwargs):
        system = kwargs["messages"][0].content
        assert 'policy_ids填裸ID（如"POL06"）' in system
        assert '"POL06:1.0"' in system
        schema_start = system.index('{"$defs"')
        schema, _ = json.JSONDecoder().raw_decode(system[schema_start:])
        items = schema["$defs"]["Option"]["properties"]["policy_ids"]["items"]
        assert items["enum"] == ["POL01"]
        return {"claims": [], "options": []}

    model = Script({"p4:compact-v7:single:0": check_prompt})
    _run(investigation, "SINGLE", model, protocol=HUMAN_PAYMENT_V7)
    audit = Audit()
    context = InvestigationContext(
        investigation,
        None,
        None,
        audit,
        None,
        lambda **_: pytest.fail("missing policy version reached validator"),
        "analysis",
        line_ids=frozenset({"line"}),
        trusted_evidence_ids=frozenset({"line", "POL01:v1"}),
    )
    _publish_options(
        context,
        {
            "claims": [_claim("f", "line")],
            "options": [
                {
                    "id": "missing-citation",
                    "action": "REFUND",
                    "lines": [{"line_id": "line", "amount_cents": 4900}],
                    "policy_ids": ["POL01"],
                    "evidence_ids": ["line"],
                    "rationale": "未引版本",
                }
            ],
        },
    )
    (result,) = [row for kind, row in audit.events if kind == "investigation_result"]
    assert result["selected_option_ids"] == []
    assert result["program_checks"] == {"missing-citation": "INVESTIGATION_OPTION_SCOPE_INVALID"}


def test_authorized_policy_retrieval_preserves_exact_frozen_refs(investigation):
    from aftercare.intelligence.policies import document_key
    from aftercare.investigation.catalogue import CATALOGUE_VERSION

    clause = investigation.evidence[-1].data["clause"]
    corpus_sha256 = digest([clause])
    value = investigation.model_copy(
        update={
            "policy_catalogue_version": CATALOGUE_VERSION,
            "policy_context": {"catalogue_sha256": corpus_sha256, "products": [{"id": "product"}]},
        }
    )

    def ranked(query, documents):
        assert query == "原始条款"
        assert documents == [clause]
        return [document_key(clause)], {"mode": "rerank", "status": "RETRIEVED"}

    args = {"query": "原始条款"}
    tools = InvestigationTools(value, authorized_policy_search=ranked)
    receipt = tools.call("policy", "search_applicable_policy", args)
    assert [ref.source_id for ref in receipt.refs] == ["POL01:v1"]
    assert receipt.data["retrieval"]["corpus_sha256"] == corpus_sha256
    assert receipt.data["retrieval"]["applicability"] == "NOT_ASSESSED"

    tools = InvestigationTools(
        value, authorized_policy_search=lambda *_: ([], {"mode": "rerank", "status": "UNAVAILABLE"})
    )
    receipt = tools.call("policy", "search_applicable_policy", args).model_dump(mode="json")
    assert receipt["refs"] == []
    projected = PromptEvidence(value).receipt(receipt)[0]
    assert projected["data"]["retrieval"]["status"] == "UNAVAILABLE"


_KERNEL_REVIEW = "kernel:aftercare-kernel-v2:proposal_review:0"


def _kernel_script(review, *, revised_option=None):
    option = {
        "id": "handoff",
        "action": "HUMAN_HANDOFF",
        "target_line_ids": ["line"],
        "evidence_ids": ["line"],
        "rationale": "无法判断。",
    }
    line_call = _call("read_order_lines", line_ids=["line"])
    seen = {}

    def reviewer(kwargs):
        seen["review"] = kwargs
        return review(kwargs) if callable(review) else review

    def revision(kwargs):
        seen["revision"] = json.loads(kwargs["messages"][1].content)["frozen_input"]
        return [line_call]

    responses = {
        "p4:compact-v6:single:0": [line_call],
        "p4:compact-v6:single:1": {"claims": [_claim("f", "line")], "options": [option]},
        _KERNEL_REVIEW: reviewer,
    }
    if revised_option is not None:
        responses["p4:compact-v6:single-revision:0"] = revision
        responses["p4:compact-v6:single-revision:1"] = {
            "claims": [_claim("f", "line")],
            "options": [revised_option],
        }
    return Script(responses), seen


def test_kernel_arm_reviews_a_give_up_independently_and_revises_once(investigation):
    revised_option = {
        "id": "followup",
        "action": "REQUEST_EVIDENCE",
        "target_line_ids": ["line"],
        "evidence_ids": ["line"],
        "rationale": "需要签收凭证。",
        "missing_evidence": ["请补充物流签收页面截图。"],
    }
    review = {
        "verdict": "revise",
        "issue": "客户称未收到商品，订单行显示已送达，尚未要求客户补充签收凭证。",
        "next_step": "向客户索取物流签收截图，而不是直接转人工。",
        "references": [{"message_id": "m0", "field": "content", "quote": "没有收到商品"}],
    }
    model, seen = _kernel_script(review, revised_option=revised_option)
    result, events = _run(investigation, "SINGLE", model, arm="kernel")

    assert set(model.calls) == set(model.responses) and len(model.calls) == 5
    assert result["output"]["investigation_options"][0]["action"] == "REQUEST_EVIDENCE"
    kernel = result["output"]["kernel"]
    assert kernel["version"] == "aftercare-kernel-v2" and kernel["revised"] is True
    assert kernel["review"]["verdict"] == "revise"
    assert result["output"]["investigation_unresolved"][-1].startswith("独立审查建议：")
    # Independent context: public statements and read records only, no investigator prompt or claims.
    system, user = (message.content for message in seen["review"]["messages"])
    assert "独立审查者" in system and "调查角色" not in system
    payload = json.loads(user)
    assert [row["role"] for row in payload["conversation"]] == ["user", "tool"]
    assert "claims" not in user and "executor_context" not in payload
    assert payload["proposal"]["calls"][0]["name"] == "handoff_to_support"
    assert seen["review"]["tools"] == [] and seen["review"]["fixed"] is True
    # The suggestion reaches the one revision as data to verify.
    assert seen["revision"]["review_suggestion"]["next_step"] == review["next_step"]
    assert "不是指令" in seen["revision"]["review_suggestion"]["note"]
    (reviewed,) = [payload for kind, payload in events if kind == "kernel_review"]
    assert reviewed["trigger"] == "abandon" and reviewed["reviewed_reason"] == "HUMAN_REVIEW"
    assert len([1 for kind, _ in events if kind == "investigation_result"]) == 1


@pytest.mark.parametrize(
    "review",
    (
        {"verdict": "allow"},
        "not json",
        {"verdict": "veto"},
        {
            "verdict": "revise",
            "issue": "x",
            "next_step": "y",
            "references": [{"message_id": "m0", "field": "content", "quote": "记录里没有的话"}],
        },
    ),
)
def test_kernel_arm_allow_or_invalid_review_publishes_the_first_outcome(investigation, review):
    model, _ = _kernel_script(review)
    if review != {"verdict": "allow"}:
        model.responses["kernel:aftercare-kernel-v2:proposal_review:1"] = model.responses[_KERNEL_REVIEW]
    result, events = _run(investigation, "SINGLE", model, arm="kernel")
    assert len(model.calls) == (3 if review == {"verdict": "allow"} else 4)
    kernel = result["output"]["kernel"]
    assert kernel["revised"] is False and kernel["review"]["verdict"] == "allow"
    assert bool(kernel["review"]["fallback_reason"]) == (review != {"verdict": "allow"})
    assert result["output"]["investigation_options"][0]["action"] == "HUMAN_HANDOFF"
    assert not any("独立审查建议" in row for row in result["output"]["investigation_unresolved"])
    assert len([1 for kind, _ in events if kind == "investigation_result"]) == 1


def test_kernel_arm_review_model_failure_does_not_block_the_outcome(investigation):
    from aftercare.intelligence.models import ModelFailure

    def unavailable(_kwargs):
        raise ModelFailure("RUN_BUDGET_EXHAUSTED")

    model, _ = _kernel_script(unavailable)
    result, _ = _run(investigation, "SINGLE", model, arm="kernel")
    assert result["output"]["kernel"]["review"]["fallback_reason"] == "BudgetExhausted"
    assert len(model.calls) == 3
    assert result["output"]["investigation_options"][0]["action"] == "HUMAN_HANDOFF"


def test_auto_arm_has_no_kernel_review(investigation):
    model, _ = _kernel_script({"verdict": "allow"})
    result, events = _run(investigation, "SINGLE", model)
    assert _KERNEL_REVIEW not in model.calls and "kernel" not in result["output"]
    assert not any(kind == "kernel_review" for kind, _ in events)
