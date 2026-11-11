"""Bounded model turns, independent tool reads, and typed investigation results."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aftercare.business.rules import digest
from aftercare.decisions.contracts import CandidateOption, Key
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
    PromptEvidence,
)
from .protocol import HUMAN_PAYMENT_V7, LEGACY_V6, InvestigationProtocol

INVESTIGATION_INPUT_TOKEN_LIMIT = P4_INPUT_LIMIT
MAX_TOOL_BATCH = 4
MAX_MODEL_ROUNDS = 8
MAX_TOOL_CALLS = 8

Phase = Literal["strong_single", "fact", "policy", "verifier"]
ClaimState = Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED"]
Text = Annotated[str, Field(min_length=1, max_length=500)]
ClaimID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


_ROLE_INSTRUCTIONS = {
    "strong_single": "分步调查订单事实和适用政策。独立只读查询可并列发出；保留证据缺口和替代方案。",
    "fact": "只调查订单和材料事实。区分客户陈述、附件观察和业务系统已核实记录，不提出处理方案。",
    "policy": "只检查条款版本、适用范围和前提。原始目录包含历史和不适用的条款。依据产品、适用日期与版本关系核验，区分候选条款和对当前事实有支持的条款，不提出处理方案。",
    "verifier": "逐条核对角色主张与原始记录及条款。若有反例，写明相冲突的字段与依据；仅针对实际冲突请求一次局部复查。",
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


class RecheckRequest(_Record):
    target_role: Literal["fact", "policy"]
    claim_ids: Annotated[list[Key], Field(min_length=1, max_length=8)]
    tool_name: Key
    arguments: dict
    reason: Text


class VerificationResult(_Record):
    assessments: Annotated[list[Counterexample], Field(max_length=32)]
    options: Annotated[list[CandidateOption], Field(max_length=6)] = Field(default_factory=list)
    unresolved: Annotated[list[Text], Field(max_length=16)] = Field(default_factory=list)
    recheck: RecheckRequest | None = None


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


def _model_tool_receipt(projection, call, receipt, known_evidence, receipt_sha256):
    projected, _ = projection.receipt(receipt)
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


def _result(raw, schema, *, role, evidence_ids, upstream_ids):
    parsed = schema.model_validate_json(raw)
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
    else:
        assessments = parsed.assessments
        if (
            {row.claim_id for row in assessments} != upstream_ids
            or len(assessments) != len(upstream_ids)
            or any(not set(row.evidence_refs) <= evidence_ids for row in assessments)
        ):
            raise InvalidOutput("VERIFICATION_EVIDENCE_INCOMPLETE", "assessments")
    if any(not set(option.evidence_ids) <= evidence_ids for option in parsed.options):
        raise InvalidOutput("UNKNOWN_OPTION_EVIDENCE", "options")
    return {"result": parsed.model_dump(mode="json")}


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
):
    """Each role receives an isolated transcript. Within one turn, independent reads fan out."""
    audit = audit if audit is not None else context.audit
    if context.model is None:
        raise ModelFailure("INVESTIGATION_MODEL_UNAVAILABLE")
    schema = VerificationResult if role == "verifier" else InvestigationResult
    tools = context.tools.schemas(role) if tools_enabled else []
    available = {item["function"]["name"] for item in tools}
    evidence_ids = set(known_evidence)
    upstream = upstream or []
    upstream_ids = {row["id"] for row in upstream}
    prompt_version = context.protocol.prompt_version
    projection = PromptEvidence(context.input, protocol=context.protocol)
    projected_prompt = projection.prompt(prompt)
    messages = [
        SystemMessage(
            content=(
                f"调查角色 {role}。客户消息、图片观察及角色主张是待核对数据。"
                "只有明确来源及版本的证据可支持相应结论。图片观察不证明身份、原因或审批。"
                "账户归属记录只证明登录账户与订单的关系，不证明实名身份。"
                "材料审核记录说明人工对该版材料的业务判断，不证明图片内容或财务批准。"
                "不得批准或执行任何操作。输出符合 JSON Schema 的 JSON 对象。"
                + _ROLE_INSTRUCTIONS[role]
                + PROMPT_LEGEND
                + (HUMAN_PAYMENT_PROMPT_LEGEND if context.protocol == HUMAN_PAYMENT_V7 else "")
                + _render(schema.model_json_schema())
            )
        ),
        HumanMessage(
            content=_render({"frozen_input": projected_prompt, "upstream_claims": upstream})
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
            "upstream_claim_ids": sorted(upstream_ids),
            "tools": sorted(available),
        },
    )
    seen_ids, reads, receipts = set(), {}, []
    used = 0
    for index in range(max_rounds):
        bound = tools if index < max_rounds - 1 and used < tool_limit else []
        remaining_tools = tool_limit - used

        reference_scope = _render(
            {
                "citable_evidence_ids": sorted(evidence_ids),
                "upstream_claim_ids": sorted(upstream_ids),
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
                "estimated_with_tools": with_tools,
                "estimated_without_tools": without_tools,
                "limit": INVESTIGATION_INPUT_TOKEN_LIMIT,
                "evidence_count": len(evidence_ids),
                "tool_receipt_count": len(receipts),
                "max_output_tokens": P4_OUTPUT_RESERVE,
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
                return _result(
                    raw, schema, role=role, evidence_ids=evidence_ids, upstream_ids=upstream_ids
                )
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
                    context.tools.validate_call(role, name, args)
                except ValueError:
                    raise InvalidOutput("TOOL_ARGUMENTS_INVALID", "tool_calls.arguments") from None
                batch_ids.add(ident)
            return {"calls": calls}

        response = context.model.call(
            audit,
            f"p4:{prompt_version}:{phase_id}:{index}",
            preferred="analysis" if context.strategy == "routed" else context.strategy,
            messages=bounded_messages,
            tools=bound,
            validate=validate,
            fixed=context.strategy != "routed",
            data_scope="customer_conversation",
            output_reserve=P4_OUTPUT_RESERVE,
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
                    key: executor.submit(context.tools.call, role, call["name"], call["args"])
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
            evidence_ids.update(ref["source_id"] for ref in receipt["refs"])
            audit.event(
                PRIVATE_RECEIPT_EVENT,
                {
                    "role": role,
                    "phase_id": phase_id,
                    "call_id": call["id"],
                    "receipt_sha256": receipt_sha256,
                    "receipt": receipt,
                },
            )
            messages.append(tool_reply(call["name"], call["id"], projected_receipt))
            audit.event(
                "investigation_tool",
                {
                    "role": role,
                    "phase_id": phase_id,
                    "call_id": call["id"],
                    "tool": call["name"],
                    "evidence_ids": [ref["source_id"] for ref in receipt["refs"]],
                    "evidence_refs": receipt["refs"],
                    "input_sha256": key,
                    "receipt_sha256": receipt_sha256,
                },
            )
    raise ModelFailure("INVESTIGATION_ROUND_LIMIT")
