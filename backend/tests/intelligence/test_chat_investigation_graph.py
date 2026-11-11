import json
from threading import Barrier

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aftercare.intelligence.models import InvalidOutput
from aftercare.investigation.contracts import (
    CustomerMessage,
    Evidence,
    EvidenceRef,
    InvestigationInput,
    RouteDecision,
)
from aftercare.investigation.graph import build_investigation_graph
from aftercare.investigation.orchestration import InvestigationContext, InvestigationResult, _result
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


def _run(value, path, script, *, tools=None, arm="auto", extra=None):
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
    )
    graph = build_investigation_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "controlled"}, "recursion_limit": 30}
    output = graph.invoke({"cycle": 0}, config, context=ctx)
    assert graph.get_state(config).values == output
    return output, audit.events


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

    model = Script(
        {
            "p4:single:0": [
                _call("read_order_lines", line_ids=["line"]),
                _call("read_order_lines", line_ids=["line2"]),
            ],
            "p4:single:1": [
                _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product")
            ],
            "p4:single:2": {
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
        }
    )
    result, events = _run(investigation, "SINGLE", model, tools=ConcurrentTools(investigation))
    assert result["output"]["investigation_options"][0]["action"] == "HUMAN_HANDOFF"
    assert len(model.calls) == 3
    assert sum(kind == "investigation_tool" for kind, _ in events) == 3


def test_multi_independent_roles_and_one_local_recheck(investigation):
    barrier = Barrier(2)

    def start(role, call):
        def check(kwargs):
            data = json.loads(kwargs["messages"][1].content)
            assert data["upstream_claims"] == []
            assert ("customer_statements" in data["frozen_input"]) == (role == "fact")
            barrier.wait(timeout=3)
            return [call]

        return check

    def first_verifier(kwargs):
        payload = json.loads(kwargs["messages"][1].content)
        originals = payload["frozen_input"]["original_read_receipts"]
        assert any(row["data"]["records"][0]["data"].get("delivered") is True for row in originals)
        assert any(row["value"] == "未交付" for row in payload["upstream_claims"])
        result = _review(
            ("fact/f", "line"),
            ("fact/child", "line"),
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
        assert {row["id"] for row in payload["upstream_claims"]} == {"fact/f", "policy/p"}
        assert payload["frozen_input"]["unresolved_invalidated_claim_ids"] == ["fact/child"]
        return _review(("fact/f", "line"), ("policy/p", "POL01:v1"))

    model = Script(
        {
            "p4:fact:0": start("fact", _call("read_order_lines", line_ids=["line"])),
            "p4:fact:1": {
                "claims": [
                    _claim("f", "line", value="未交付"),
                    _claim("child", "line", depends=("f",)),
                ]
            },
            "p4:policy:0": start(
                "policy",
                _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product"),
            ),
            "p4:policy:1": {"claims": [_claim("p", "POL01:v1")]},
            "p4:verify-0:0": first_verifier,
            "p4:recheck:0": {"claims": [_claim("f", "line", value="已交付")]},
            "p4:verify-1:0": final_verifier,
        }
    )
    result, events = _run(investigation, "MULTI", model)
    assert result["reason"] == "INVESTIGATION_DEPENDENT_CLAIMS_UNRESOLVED"
    assert [
        payload["target_role"] for kind, payload in events if kind == "investigation_recheck"
    ] == ["fact"]
    assert len(model.calls) == 7


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
