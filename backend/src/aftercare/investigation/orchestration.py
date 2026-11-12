"""Bounded model turns, independent tool reads, and typed investigation results."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from aftercare.business.rules import digest
from aftercare.decisions.contracts import CandidateOption, Key
from aftercare.intelligence.language import LANGUAGE_VERSION, PLAIN_LANGUAGE
from aftercare.intelligence.models import (
    P4_INPUT_LIMIT,
    P4_OUTPUT_RESERVE,
    InvalidOutput,
    ModelFailure,
    _estimate_input,
    tool_reply,
)

from .prompt_evidence import (
    HUMAN_PAYMENT_PROMPT_LEGEND,
    PRIVATE_RECEIPT_EVENT,
    PROMPT_LEGEND,
    READ_INDEX_LEGEND,
    RECEIPT_LEGEND,
    VERIFIER_LEGEND,
    PromptEvidence,
    verifier_claims,
)
from .protocol import HUMAN_PAYMENT_V7, LEGACY_V6, InvestigationProtocol
from .tools import (
    ClauseArgs,
    LineArgs,
    MaterialArgs,
    PolicyArgs,
    RefundArgs,
    ToolRejected,
    recheck_executor,
)

INVESTIGATION_INPUT_TOKEN_LIMIT = P4_INPUT_LIMIT
MAX_TOOL_BATCH = 4
MAX_MODEL_ROUNDS = 8
MAX_TOOL_CALLS = 8

Phase = Literal["strong_single", "fact", "policy", "verifier", "synthesizer"]
ClaimState = Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED"]
Text = Annotated[str, Field(min_length=1, max_length=500)]
ClaimID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


_ROLE_INSTRUCTIONS = {
    "strong_single": "分步调查订单事实和适用政策。独立只读查询可并列发出；保留证据缺口和替代方案。",
    "fact": "只调查订单和材料事实。区分客户陈述、附件观察和业务系统已核实记录，不提出处理方案。",
    "policy": "只检查条款版本、适用范围和前提。原始目录包含历史和不适用的条款。依据产品、适用日期与版本关系核验，区分候选条款和对当前事实有支持的条款，不提出处理方案。",
    "synthesizer": "根据现有角色主张和对应证据整理候选与未解决事项。遇到不足可请求定向核对，保留不确定性。",
    "verifier": "逐条核对角色主张与原始记录及条款。若有反例，写明相冲突的字段与依据；仅针对实际冲突请求一次局部复查。补查工具由有权读取的角色执行，target_role是修订主张的角色。arguments严格使用工具对应的字段。",
}


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(_Record):
    id: ClaimID
    subject: Key
    predicate: Key
    value: Text
    state: ClaimState
    supporting_refs: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)
    conflicting_refs: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)
    depends_on_claims: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.state == "SUPPORTED" and not self.supporting_refs:
            raise ValueError("Supported claim requires evidence")
        if self.state == "CONTRADICTED" and not self.conflicting_refs:
            raise ValueError("Contradicted claim requires evidence")
        return self


class InvestigationResult(_Record):
    claims: Annotated[list[Claim], Field(max_length=16)]
    options: Annotated[list[CandidateOption], Field(max_length=6)] = Field(default_factory=list)
    unresolved: Annotated[list[Text], Field(max_length=16)] = Field(default_factory=list)
    escalate: Literal["FACT_CONFLICT", "POLICY_CONFLICT", "SCOPE_AMBIGUOUS"] | None = None


class Counterexample(_Record):
    claim_id: Key
    contradicted_field: Key
    verdict: Literal["CONFIRMED", "CONTRADICTED", "UNRESOLVED"]
    evidence_refs: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.verdict in {"CONFIRMED", "CONTRADICTED"} and not self.evidence_refs:
            raise ValueError("A confirmed or contradicted claim requires evidence")
        return self


class _RecheckRequest(_Record):
    target_role: Literal["fact", "policy"]
    claim_ids: Annotated[list[Key], Field(min_length=1, max_length=8)]
    reason: Text


class _RecheckLines(_RecheckRequest):
    tool_name: Literal["read_order_lines"]
    arguments: LineArgs


class _RecheckMaterials(_RecheckRequest):
    tool_name: Literal["read_material_evidence"]
    arguments: MaterialArgs


class _RecheckRefund(_RecheckRequest):
    tool_name: Literal["read_refund_execution"]
    arguments: RefundArgs


class _RecheckSearch(_RecheckRequest):
    tool_name: Literal["search_applicable_policy"]
    arguments: PolicyArgs


class _RecheckClause(_RecheckRequest):
    tool_name: Literal["read_policy_clause"]
    arguments: ClauseArgs


RecheckRequest = Annotated[
    _RecheckLines | _RecheckMaterials | _RecheckRefund | _RecheckSearch | _RecheckClause,
    Field(discriminator="tool_name"),
]
RECHECK_REQUEST = TypeAdapter(RecheckRequest)


def validate_recheck(context, request, claims, assessments):
    name = request.tool_name
    executor = recheck_executor(name)
    arguments = request.arguments.model_dump(mode="json")
    context.tools.validate_call(executor, name, arguments)
    disputed = {
        item["claim_id"]: set(item["evidence_refs"])
        for item in assessments
        if item["verdict"] != "CONFIRMED"
    }
    targets = set(request.claim_ids)
    owned = {row["id"]: row for row in claims if row["role"] == request.target_role}
    if len(targets) != len(request.claim_ids) or not targets <= owned.keys():
        raise InvalidOutput("INVESTIGATION_RECHECK_SCOPE_INVALID", "recheck.claim_ids")
    if assessments and not targets <= disputed.keys():
        raise InvalidOutput("INVESTIGATION_RECHECK_SCOPE_INVALID", "recheck.claim_ids")

    related = [
        disputed.get(ident, set())
        if executor != request.target_role
        else set(owned[ident]["supporting_refs"] + owned[ident]["conflicting_refs"])
        | {owned[ident]["subject"]}
        | disputed.get(ident, set())
        for ident in targets
    ]
    if (
        name == "search_applicable_policy"
        and executor == request.target_role
        and not arguments["condition_keys"]
    ):
        # Search selects candidate clauses by query; the resulting receipt must
        # still contain a policy reference for each target before revision.
        return executor, arguments, related

    if name == "read_refund_execution":
        execution = arguments["execution_id"]
        requested = {execution, *context.input.refund_execution_line_ids.get(execution, ())}
    elif name == "read_policy_clause":
        line_products = {
            row.ref.source_id: row.data["product_id"]
            for row in context.input.evidence
            if row.ref.source_kind == "ORDER_LINE" and row.data.get("product_id")
        }
        for ident in targets:
            claim = owned[ident]
            own_lines = {
                claim["subject"],
                *claim["supporting_refs"],
                *claim["conflicting_refs"],
            } & line_products.keys()
            if any(line_products[line] != arguments["product_id"] for line in own_lines):
                raise InvalidOutput(
                    "INVESTIGATION_RECHECK_SCOPE_INVALID", "recheck.arguments.product_id"
                )
        policy = arguments["policy_id"]
        requested = {f"{policy}:{arguments['version']}"}
        if context.input.policy_catalogue_version:
            requested.update(
                row.ref.source_id
                for row in context.input.evidence
                if row.ref.source_kind == "POLICY" and row.data["clause"]["id"] == policy
            )
    else:
        field = {
            "read_order_lines": "line_ids",
            "read_material_evidence": "evidence_ids",
            "search_applicable_policy": "condition_keys",
        }[name]
        requested = set(arguments[field])
        if not requested <= set().union(*related):
            raise InvalidOutput("INVESTIGATION_RECHECK_SCOPE_INVALID", "recheck.arguments")

    if any(not requested.intersection(refs) for refs in related):
        raise InvalidOutput("INVESTIGATION_RECHECK_SCOPE_INVALID", "recheck.arguments")
    return executor, arguments, related


class VerificationResult(_Record):
    assessments: Annotated[list[Counterexample], Field(max_length=32)]
    options: Annotated[list[CandidateOption], Field(max_length=6)] = Field(default_factory=list)
    unresolved: Annotated[list[Text], Field(max_length=16)] = Field(default_factory=list)
    recheck: RecheckRequest | None = None


class FinalVerificationResult(VerificationResult):
    recheck: None = None


class SynthesisResult(_Record):
    options: Annotated[list[CandidateOption], Field(max_length=6)] = Field(default_factory=list)
    unresolved: Annotated[list[Text], Field(max_length=16)] = Field(default_factory=list)


@dataclass(slots=True)
class InvestigationContext:
    input: object
    route: object
    model: object | None
    audit: object
    tools: object
    validate_draft: object
    strategy: str
    line_ids: frozenset[str] = frozenset()
    trusted_evidence_ids: frozenset[str] = frozenset()
    verifier_reserve: object = None
    arm: str = "auto"
    policy_authority: dict | None = None
    protocol: InvestigationProtocol = LEGACY_V6
    counterexample_ablation: bool = False
    output_reserve: int = P4_OUTPUT_RESERVE
    direct_recheck_request: bool = False


class ProtectedAudit:
    """Keep verifier admission capacity inside the same atomic run budget."""

    def __init__(self, audit, reserve):
        self.audit, self.reserve = audit, reserve

    def __getattr__(self, name):
        return getattr(self.audit, name)

    def begin_attempt(self, *args, **kwargs):
        return self.audit.begin_attempt(
            *args,
            **kwargs,
            protected_attempts=2,
            protected_cost=self.reserve,
        )


def _render(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prompt_schema(schema):
    """Omit generated titles from JSON Schema nodes, keeping instance data intact."""
    result = {key: value for key, value in schema.items() if key != "title"}
    for key in ("properties", "$defs"):
        if key in result:
            result[key] = {name: _prompt_schema(node) for name, node in result[key].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = _prompt_schema(result[key])
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [_prompt_schema(node) for node in result[key]]
    return result


POLICY_CITATION_LEGEND = 'policy_ids填裸ID（如"POL06"）；同一方案evidence_ids须同时填该条实际读取的版本键（如"POL06:1.0"）。'


def _compact_recheck_schema(projected):
    request = projected["properties"].get("recheck")
    if not request or "anyOf" not in request:
        return
    definitions = projected["$defs"]
    alternatives = request["anyOf"][0]["discriminator"]["mapping"]
    branch_defs = {
        tool: definitions.pop(ref.rsplit("/", 1)[-1]) for tool, ref in alternatives.items()
    }
    original = next(iter(branch_defs.values()))
    common = {**original, "properties": dict(original["properties"])}
    common["properties"]["tool_name"] = {"enum": list(branch_defs)}
    common["properties"]["arguments"] = {"type": "object"}
    common["allOf"] = [
        {
            "if": {"properties": {"tool_name": {"const": tool}}},
            "then": {"properties": {"arguments": branch["properties"]["arguments"]}},
        }
        for tool, branch in branch_defs.items()
    ]
    request["anyOf"][0] = common


def _v7_prompt_schema(schema, *, policy_ids=None):
    projected = _prompt_schema(schema)
    _compact_recheck_schema(projected)
    definitions = projected["$defs"]
    option = definitions["Option"]
    line = definitions["DecisionLine"]
    if policy_ids is not None:
        policy_items = option["properties"]["policy_ids"]
        if policy_ids:
            policy_items["items"]["enum"] = sorted(set(policy_ids))
        else:
            policy_items["maxItems"] = 0

    def require_line_field(field, forbidden):
        # DecisionLine exposes positive integer or null. The action requires
        # the positive integer branch and permits only null for the other field.
        integer = next(
            shape for shape in line["properties"][field]["anyOf"] if shape["type"] == "integer"
        )
        return {
            "properties": {
                "lines": {
                    "items": {
                        "properties": {field: integer, forbidden: {"type": "null"}},
                        "required": [field],
                    }
                }
            }
        }

    option.update(
        {
            "if": {"properties": {"action": {"const": "REFUND"}}},
            "then": require_line_field("amount_cents", "quantity"),
            "else": require_line_field("quantity", "amount_cents"),
        }
    )
    definitions["NonExecutingOption"].update(
        {
            "if": {"properties": {"action": {"const": "REQUEST_EVIDENCE"}}},
            "then": {
                "properties": {"missing_evidence": {"minItems": 1}},
                "required": ["missing_evidence"],
            },
        }
    )
    return projected


def _share_prompt_constraints(schema):
    """Use short references for repeated, identical generated constraints."""
    shared = {
        "K": {"type": "string", "minLength": 1, "maxLength": 80},
        "T": {"type": "string", "minLength": 1, "maxLength": 1000},
        "P": {"type": "integer", "exclusiveMinimum": 0, "maximum": 2147483647},
    }

    def compact(value):
        if isinstance(value, dict):
            for alias, constraint in shared.items():
                if value == constraint:
                    return {"$ref": f"#/$defs/{alias}"}
            return {key: compact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    result = compact(schema)
    assert not result["$defs"].keys() & shared.keys()
    result["$defs"].update(shared)
    return result


def _model_tool_receipt(projection, call, receipt, known_evidence, receipt_sha256):
    projected = projection.tool_receipt(receipt)
    name = "request_targeted_check"
    if call["name"] != name or receipt["name"] != name:
        return projected
    referenced = {ref["source_id"] for ref in receipt["refs"]}
    if (
        referenced <= known_evidence
        and referenced == set(call["args"].get("conflicting_refs", ()))
        and receipt["arguments_sha256"] == digest(call["args"])
        and receipt["data"] == {**call["args"], "status": "REQUESTED"}
    ):
        return {"status": "REQUESTED", "receipt_sha256": receipt_sha256}
    return projected


def _result(raw, schema, *, role, evidence_ids, upstream_ids, assessment_ref_ids=()):
    try:
        parsed = schema.model_validate_json(raw)
    except ValidationError as error:
        if schema is FinalVerificationResult and any(
            issue["loc"] == ("recheck",)
            for issue in error.errors(include_input=False, include_context=False, include_url=False)
        ):
            raise InvalidOutput("RECHECK_NOT_ALLOWED_IN_FINAL", "recheck") from None
        raise
    if isinstance(parsed, InvestigationResult):
        claims = parsed.claims
        if len({item.id for item in claims}) != len(claims):
            raise InvalidOutput("DUPLICATE_CLAIM", "claims")
        known = {claim.id for claim in claims} | upstream_ids
        for index, claim in enumerate(claims):
            field = f"claims.{index}"
            if not set(claim.supporting_refs) <= evidence_ids:
                raise InvalidOutput("UNKNOWN_SUPPORTING_EVIDENCE", f"{field}.supporting_refs")
            if not set(claim.conflicting_refs) <= evidence_ids:
                raise InvalidOutput("UNKNOWN_CONFLICTING_EVIDENCE", f"{field}.conflicting_refs")
            if claim.id in claim.depends_on_claims:
                raise InvalidOutput("SELF_CLAIM_DEPENDENCY", f"{field}.depends_on_claims")
            if not set(claim.depends_on_claims) <= known:
                raise InvalidOutput("UNKNOWN_CLAIM_DEPENDENCY", f"{field}.depends_on_claims")
        try:
            tuple(
                TopologicalSorter({row.id: row.depends_on_claims for row in claims}).static_order()
            )
        except CycleError:
            raise InvalidOutput("CYCLIC_CLAIM_DEPENDENCY", "claims") from None
        if role in {"fact", "policy"} and parsed.options:
            raise InvalidOutput("ROLE_ACTION_FORBIDDEN", "options")
    elif isinstance(parsed, VerificationResult):
        seen = set()
        for index, row in enumerate(parsed.assessments):
            field = f"assessments.{index}.claim_id"
            if row.claim_id not in upstream_ids:
                raise InvalidOutput("VERIFICATION_UNKNOWN_CLAIM", field)
            if row.claim_id in seen:
                raise InvalidOutput("VERIFICATION_DUPLICATE_CLAIM", field)
            seen.add(row.claim_id)
        if seen != upstream_ids:
            raise InvalidOutput("VERIFICATION_MISSING_CLAIM", "assessments")
        counterexample_refs = evidence_ids.union(assessment_ref_ids)
        for index, row in enumerate(parsed.assessments):
            permitted = evidence_ids if row.verdict == "CONFIRMED" else counterexample_refs
            if not set(row.evidence_refs) <= permitted:
                raise InvalidOutput(
                    "VERIFICATION_EVIDENCE_OUT_OF_SCOPE", f"assessments.{index}.evidence_refs"
                )
    if any(not set(option.evidence_ids) <= evidence_ids for option in parsed.options):
        raise InvalidOutput("UNKNOWN_OPTION_EVIDENCE", "options")
    return {"result": parsed.model_dump(mode="json")}


def _audit_read(audit, role, phase_id, call_id, receipt, receipt_sha256, input_sha256, **extra):
    audit.event(
        PRIVATE_RECEIPT_EVENT,
        {
            "role": role,
            "phase_id": phase_id,
            "call_id": call_id,
            "receipt_sha256": receipt_sha256,
            "receipt": receipt,
            **extra,
        },
    )
    audit.event(
        "investigation_tool",
        {
            "role": role,
            "phase_id": phase_id,
            "call_id": call_id,
            "tool": receipt["name"],
            "evidence_ids": [ref["source_id"] for ref in receipt["refs"]],
            "evidence_refs": receipt["refs"],
            "input_sha256": input_sha256,
            "receipt_sha256": receipt_sha256,
            **extra,
        },
    )


def run_phase(
    context,
    role,
    phase_id,
    *,
    prompt,
    upstream=None,
    known_evidence=(),
    audit=None,
    max_rounds=MAX_MODEL_ROUNDS,
    tool_limit=MAX_TOOL_CALLS,
    tools_enabled=True,
    assessment_ref_ids=(),
    initial_material_ids=(),
):
    """Each role receives an isolated transcript. Within one turn, independent reads fan out."""
    audit = audit if audit is not None else context.audit
    if context.model is None:
        raise ModelFailure("INVESTIGATION_MODEL_UNAVAILABLE")
    final_verifier = role == "verifier" and phase_id == "verify-1"
    direct_recheck = (
        role == "verifier" and phase_id == "verify-0" and context.direct_recheck_request
    )
    schema = {
        "verifier": FinalVerificationResult if final_verifier else VerificationResult,
        "synthesizer": SynthesisResult,
    }.get(role, InvestigationResult)
    tool_role = "verifier" if role == "synthesizer" else role
    scoped_arguments = context.protocol == HUMAN_PAYMENT_V7
    tools = (
        context.tools.schemas(tool_role, **({"scoped_arguments": True} if scoped_arguments else {}))
        if tools_enabled
        else []
    )
    available = {item["function"]["name"] for item in tools}
    evidence_ids = set(known_evidence)
    seen_ids, reads, receipts = set(), {}, []
    used = 0
    if role in {"fact", "strong_single"} and phase_id != "recheck":
        evidence_ids.update(
            row["ref"]["source_id"]
            for key in ("customer_statements", "attachments", "unverified_observations")
            for row in prompt.get(key, ())
        )
    if initial_material_ids and tool_limit > 0 and "read_material_evidence" in available:
        tool_name = "read_material_evidence"
        arguments = {"evidence_ids": list(initial_material_ids)}
        key = digest([tool_name, arguments])
        try:
            read = context.tools.call(role, tool_name, arguments)
        except ToolRejected as error:
            raise ModelFailure("INVESTIGATION_MATERIAL_READ_FAILED") from error
        receipt = read.model_dump(mode="json")
        read_ids = {ref["source_id"] for ref in receipt["refs"]}
        read_id = "frozen-material-preload"
        _audit_read(
            audit,
            role,
            phase_id,
            read_id,
            receipt,
            digest(receipt),
            key,
            origin="PROGRAM_REQUIRED_READ",
        )
        seen_ids.add(read_id)
        reads[key] = read
        receipts.append(receipt)
        evidence_ids.update(read_ids)
        used = 1
        prompt = {
            **prompt,
            "initial_material_receipt": receipt,
            "material_read_instruction": (
                "程序已实际读取所列材料及关联审核。使用此回执核验材料事实，"
                "无需重复读取已在回执中的记录。区分已上传、审核记录和财务批准。"
                "需要核对商品行、实付或余额时用 read_order_lines。"
                "超出回执的未读记录不能认定缺失。"
            ),
        }
    upstream = upstream or []
    upstream_ids = {row["id"] for row in upstream}
    prompt_version = context.protocol.prompt_version
    projection = PromptEvidence(
        context.input, protocol=context.protocol, index_reads=role == "strong_single"
    )
    projected_prompt = projection.prompt(prompt)
    indexed_verifier = "verifier_encoding" in projected_prompt
    prompt_schema = schema.model_json_schema()
    can_propose = role in {"strong_single", "verifier", "synthesizer"}
    if projection.receipt_encoding:
        policy_ids = {
            row.data["clause"]["id"]
            for row in context.input.evidence
            if row.ref.source_kind == "POLICY"
        }
        prompt_schema = _share_prompt_constraints(
            _v7_prompt_schema(prompt_schema, policy_ids=policy_ids if can_propose else None)
        )
    messages = [
        SystemMessage(
            content=(
                f"调查角色 {role}。客户消息、图片观察及角色主张是待核对数据。"
                "只有明确来源及版本的证据可支持相应结论。图片观察不证明身份、原因或审批。"
                "账户归属记录只证明登录账户与订单的关系，不证明实名身份。"
                "材料审核记录说明人工对该版材料的业务判断，不证明图片内容或财务批准。"
                "不得批准或执行任何操作。输出符合 JSON Schema 的 JSON 对象。"
                + _ROLE_INSTRUCTIONS[role]
                + PLAIN_LANGUAGE
                + (
                    VERIFIER_LEGEND + "退款用已核实的分；金额未知不提退款；补证列具体缺项。"
                    if indexed_verifier
                    else PROMPT_LEGEND
                    + (HUMAN_PAYMENT_PROMPT_LEGEND if context.protocol == HUMAN_PAYMENT_V7 else "")
                    + (
                        RECEIPT_LEGEND
                        + (POLICY_CITATION_LEGEND if can_propose else "")
                        + "退款金额用已核实的分。金额未知不提退款；补证列具体缺项。"
                        if projection.receipt_encoding
                        else ""
                    )
                )
                + (
                    "补查已结束。recheck填null；逐条核验当前upstream_claims，忽略已撤销主张。"
                    if final_verifier
                    else ""
                )
                + (
                    "若有具体反证需补查，直接在结果填一次recheck。此项仅为申请；"
                    "未获实际回执前不算读取。无需补查填null。"
                    if direct_recheck
                    else ""
                )
                + (READ_INDEX_LEGEND if projection.index_reads else "")
                + _render(prompt_schema)
            )
        ),
        HumanMessage(
            content=_render(
                {
                    "frozen_input": projected_prompt,
                    "upstream_claims": verifier_claims(upstream, projection.verifier_ref_positions)
                    if indexed_verifier
                    else upstream,
                }
            )
        ),
    ]
    audit.event(
        "investigation_input",
        {
            "role": role,
            "phase_id": phase_id,
            "input_sha256": digest(prompt),
            "prompt_sha256": digest(projected_prompt),
            "prompt_version": prompt_version,
            "language_version": LANGUAGE_VERSION,
            "upstream_claim_ids": sorted(upstream_ids),
            "tools": sorted(available),
            **({"recheck_request_mode": "structured_result"} if direct_recheck else {}),
            **({"tool_schema_sha256": digest(tools)} if scoped_arguments else {}),
        },
    )
    for index in range(max_rounds):
        bound = tools if index < max_rounds - 1 and used < tool_limit else []
        remaining_tools = tool_limit - used

        references = sorted(evidence_ids)
        if indexed_verifier:
            references = [projection.verifier_ref_positions.get(ref, ref) for ref in references]
        reference_scope = _render(
            {
                "citable_evidence_ids": references,
                **({} if indexed_verifier else {"upstream_claim_ids": sorted(upstream_ids)}),
            }
        )

        def with_budget(
            allowed, *, remaining_tools=remaining_tools, reference_scope=reference_scope
        ):
            hint = (
                f"本轮最多调用 {allowed} 个工具，当前阶段剩余 {remaining_tools} 次工具读取。"
                "工具不足时提交结构化结果并保留尚未解决的证据缺口。"
                f"\n{reference_scope}"
            )
            return [
                messages[0].model_copy(update={"content": messages[0].content + hint}),
                *messages[1:],
            ]

        allowed = min(MAX_TOOL_BATCH, remaining_tools) if bound else 0
        bounded_messages = with_budget(allowed)
        with_tools = _estimate_input(bounded_messages, bound, vision=False)[0]
        without_tools = (
            _estimate_input(with_budget(0), [], vision=False)[0] if bound else with_tools
        )
        audit.event(
            "investigation_prompt_budget",
            {
                "role": role,
                "phase_id": phase_id,
                "round": index,
                "prompt_version": prompt_version,
            "language_version": LANGUAGE_VERSION,
                "estimated_with_tools": with_tools,
                "estimated_without_tools": without_tools,
                "limit": INVESTIGATION_INPUT_TOKEN_LIMIT,
                "evidence_count": len(evidence_ids),
                "tool_receipt_count": len(receipts),
                "max_output_tokens": context.output_reserve,
                "allowed_tool_batch": allowed
                if with_tools <= INVESTIGATION_INPUT_TOKEN_LIMIT
                else 0,
                "preflight": "OVER_LIMIT"
                if without_tools > INVESTIGATION_INPUT_TOKEN_LIMIT
                else "FIT_WITHOUT_TOOLS"
                if with_tools > INVESTIGATION_INPUT_TOKEN_LIMIT
                else "FIT",
            },
        )
        if without_tools > INVESTIGATION_INPUT_TOKEN_LIMIT:
            raise ModelFailure("INVESTIGATION_PROMPT_LIMIT")
        if with_tools > INVESTIGATION_INPUT_TOKEN_LIMIT:
            bound, allowed = [], 0
            bounded_messages = with_budget(allowed)

        def validate(calls, raw, *, allowed=allowed):
            if not calls:
                validated = _result(
                    raw,
                    schema,
                    role=role,
                    evidence_ids=evidence_ids,
                    upstream_ids=upstream_ids,
                    assessment_ref_ids=assessment_ref_ids,
                )
                if role == "verifier" and validated["result"].get("recheck") is not None:
                    request = RECHECK_REQUEST.validate_python(validated["result"]["recheck"])
                    try:
                        validate_recheck(
                            context, request, upstream, validated["result"]["assessments"]
                        )
                    except ToolRejected as error:
                        raise InvalidOutput(
                            error.issue_type or error.code, "recheck.arguments"
                        ) from None
                return validated
            if len(calls) > allowed:
                raise InvalidOutput(
                    "TOOL_BATCH_LIMIT", "tool_calls", tool_counts=(len(calls), allowed)
                )
            batch_ids = set()
            for call in calls:
                name, ident, args = call["name"], call["id"], call["args"]
                if (
                    name not in available
                    or not isinstance(ident, str)
                    or not ident
                    or ident in seen_ids | batch_ids
                ):
                    raise InvalidOutput("TOOL_CALL_FORBIDDEN", "tool_calls")
                try:
                    context.tools.validate_call(tool_role, name, args)
                except ToolRejected as error:
                    if scoped_arguments:
                        field = f"tool_calls.arguments.{name}"
                        if error.field is not None:
                            field = f"{field}.{error.field}"
                        raise InvalidOutput(error.issue_type or error.code, field) from None
                    raise InvalidOutput("TOOL_ARGUMENTS_INVALID", "tool_calls.arguments") from None
                except ValueError:
                    raise InvalidOutput("TOOL_ARGUMENTS_INVALID", "tool_calls.arguments") from None
                batch_ids.add(ident)
            return {"calls": calls}

        from .model_routing import select_model

        selection = select_model(context.strategy, role, phase_id, getattr(context.model, "registry", None))
        audit.event("task_model_route", {
            "role": role, "phase_id": phase_id, "round": index,
            "preferred": selection.preferred, "fixed": selection.fixed,
            "reason": selection.reason, "strategy": context.strategy,
        })
        response = context.model.call(
            audit,
            f"p4:{prompt_version}:{phase_id}:{index}",
            preferred=selection.preferred,
            messages=bounded_messages,
            tools=bound,
            validate=validate,
            fixed=selection.fixed,
            data_scope="customer_conversation",
            output_reserve=context.output_reserve,
        )
        if "result" in response:
            return response["result"], sorted(evidence_ids), receipts, used
        calls = response["calls"]
        seen_ids.update(call["id"] for call in calls)
        used += len(calls)
        messages.append(AIMessage(content="", tool_calls=calls))
        keys = [digest([call["name"], call["args"]]) for call in calls]
        fresh = {key: call for key, call in zip(keys, calls, strict=True) if key not in reads}
        if fresh:
            with ThreadPoolExecutor(max_workers=min(MAX_TOOL_BATCH, len(fresh))) as executor:
                futures = {
                    key: executor.submit(context.tools.call, tool_role, call["name"], call["args"])
                    for key, call in fresh.items()
                }
                for key, future in futures.items():
                    reads[key] = future.result()
        for call, key in zip(calls, keys, strict=True):
            receipt = reads[key].model_dump(mode="json")
            if receipt not in receipts:
                receipts.append(receipt)
            receipt_sha256 = digest(receipt)
            projected_receipt = _model_tool_receipt(
                projection, call, receipt, evidence_ids, receipt_sha256
            )
            # A targeted-check request identifies references without reading their records.
            if receipt["name"] != "request_targeted_check":
                evidence_ids.update(ref["source_id"] for ref in receipt["refs"])
            _audit_read(audit, role, phase_id, call["id"], receipt, receipt_sha256, key)
            messages.append(tool_reply(call["name"], call["id"], projected_receipt))
    raise ModelFailure("INVESTIGATION_ROUND_LIMIT")
