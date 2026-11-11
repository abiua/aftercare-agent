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
from aftercare.investigation.prompt_evidence import PROMPT_VERSION
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

    def round_response(ids, response):
        def check(kwargs):
            system = kwargs["messages"][0].content
            scope = json.loads(system.rsplit("\n", 1)[1])
            assert scope == {"citable_evidence_ids": ids, "upstream_claim_ids": []}
            assert "depends_on_claims" in system
            assert "POL06:1.0" in system
            visible = json.loads(kwargs["messages"][1].content)["frozen_input"]
            assert visible["customer_statements"][0]["ref"][1] == "m"
            assert "m" not in scope["citable_evidence_ids"]
            return response

        return check

    model = Script(
        {
            f"p4:{PROMPT_VERSION}:single:0": round_response(
                [],
                [
                    _call("read_order_lines", line_ids=["line"]),
                    _call("read_order_lines", line_ids=["line2"]),
                ],
            ),
            f"p4:{PROMPT_VERSION}:single:1": round_response(
                ["line", "line2"],
                [
                    _call(
                        "read_policy_clause", policy_id="POL01", version="v1", product_id="product"
                    )
                ],
            ),
            f"p4:{PROMPT_VERSION}:single:2": round_response(
                ["POL01:v1", "line", "line2"],
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
        originals = payload["frozen_input"]["original_frozen_evidence"]
        assert any(row["data"].get("delivered") is True for row in originals)
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
            f"p4:{PROMPT_VERSION}:fact:0": start(
                "fact", _call("read_order_lines", line_ids=["line"])
            ),
            f"p4:{PROMPT_VERSION}:fact:1": {
                "claims": [
                    _claim("f", "line", value="未交付"),
                    _claim("child", "line", depends=("f",)),
                ]
            },
            f"p4:{PROMPT_VERSION}:policy:0": start(
                "policy",
                _call("read_policy_clause", policy_id="POL01", version="v1", product_id="product"),
            ),
            f"p4:{PROMPT_VERSION}:policy:1": {"claims": [_claim("p", "POL01:v1")]},
            f"p4:{PROMPT_VERSION}:verify-0:0": first_verifier,
            f"p4:{PROMPT_VERSION}:recheck:0": {"claims": [_claim("f", "line", value="已交付")]},
            f"p4:{PROMPT_VERSION}:verify-1:0": final_verifier,
        }
    )
    result, events = _run(investigation, "MULTI", model)
    assert result["reason"] == "INVESTIGATION_DEPENDENT_CLAIMS_UNRESOLVED"
    assert [
        payload["target_role"] for kind, payload in events if kind == "investigation_recheck"
    ] == ["fact"]
    assert len(model.calls) == 7


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
    from aftercare.business.rules import digest
    from aftercare.intelligence.models import ModelFailure
    from aftercare.investigation.prompt_evidence import PromptEvidence

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
    from aftercare.investigation.graph import _recheck

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
    assert script.calls == [f"p4:{PROMPT_VERSION}:recheck:0"]
