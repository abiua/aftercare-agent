"""Versioned boundary contracts; no business-service or benchmark dependencies."""
from __future__ import annotations
from enum import StrEnum
from typing import Any, Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'aftercare-kernel-v1'
Role = Literal['executor','reviewer','self_check','fact_expert','policy_expert','vision_expert','user','judge']


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ToolKind(StrEnum):
    READ='read'
    WRITE='write'
    AUTH='auth'
    HANDOFF='handoff'


class ToolDefinition(Record):
    name: str
    kind: ToolKind
    parameters: dict = Field(default_factory=dict)
    description: str = ''


class EvidenceRef(Record):
    message_id: str
    field: str = ''
    quote: str = ''


class PublicMessage(Record):
    id: str
    role: Literal['user','assistant','tool']
    content: str = ''
    tool_name: str | None = None
    call_id: str | None = None
    arguments: dict = Field(default_factory=dict)
    error: bool = False


class ToolCall(Record):
    id: str
    name: str
    arguments: dict = Field(default_factory=dict)
    sources: dict[str, tuple[EvidenceRef, ...]] = Field(default_factory=dict)


class StepProposal(Record):
    text: str = ''
    calls: tuple[ToolCall, ...] = ()
    # Internal context stays with executor; never copied into independent review.
    private_context: tuple[dict, ...] = ()


class GuardResult(Record):
    allowed: bool
    feedback: str = ''
    rule_ids: tuple[str, ...] = ()
    confirmed_facts: dict = Field(default_factory=dict)


class ReviewResult(Record):
    verdict: Literal['allow','revise']
    issue: str = ''
    next_step: str = ''
    references: tuple[EvidenceRef, ...] = ()
    requests: tuple[dict, ...] = ()
    fallback_reason: str | None = None

    @model_validator(mode='after')
    def actionable(self):
        if self.verdict=='revise' and (not self.issue.strip() or not self.next_step.strip()):
            raise ValueError('Revision requires a concrete issue and next step')
        return self


class ExpertRequest(Record):
    role: Literal['fact_expert','policy_expert','vision_expert']
    question: str = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    user_id: str
    order_ids: tuple[str, ...]
    max_reads: int = Field(default=3, ge=0, le=8)


class ExpertClaim(Record):
    id: str
    subject: str
    predicate: str
    value: str
    state: Literal['SUPPORTED','CONTRADICTED','UNRESOLVED']
    supporting_refs: tuple[EvidenceRef, ...] = ()
    conflicting_refs: tuple[EvidenceRef, ...] = ()

    @model_validator(mode='after')
    def grounded(self):
        if self.state=='SUPPORTED' and not self.supporting_refs:
            raise ValueError('Supported claims need evidence')
        if self.state=='CONTRADICTED' and not self.conflicting_refs:
            raise ValueError('Contradicted claims need evidence')
        return self


class ExpertResult(Record):
    claims: tuple[ExpertClaim, ...] = ()
    candidates: tuple[dict, ...] = ()
    unresolved: tuple[str, ...] = ()
    policy_quotes: tuple[str, ...] = ()
    stop_reason: str = 'complete'


class ModelAssignment(Record):
    model: str
    reasoning: str = 'low'


class Architecture(Record):
    name: Literal['S0','S1','S2','S1c','M1','M2']
    guards: bool = True
    review: Literal['none','self','independent'] = 'none'
    experts: bool = False
    executor: ModelAssignment
    reviewer: ModelAssignment
    expert: ModelAssignment
    max_agent_calls: int = 48
    historical_only: bool = False


class TraceEvent(Record):
    version: str = VERSION
    role: Role
    stage: str
    model: str | None = None
    input_sha256: str
    output: dict
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: str | None = None
    elapsed_seconds: float = 0
    reason: str | None = None


class ToolPort(Protocol):
    def definitions(self) -> tuple[ToolDefinition, ...]: ...
    def read(self, call: ToolCall) -> PublicMessage: ...


class CommitPort(Protocol):
    def submit(self, proposal: StepProposal, review: ReviewResult | None) -> Any:
        """Official adapter executes; product adapter creates a reviewable snapshot."""
        ...


class ModelPort(Protocol):
    def generate(self, *, role: Role, assignment: ModelAssignment,
                 messages: list[dict], tools: tuple[ToolDefinition, ...], stage: str) -> StepProposal: ...


def architectures() -> dict[str, Architecture]:
    strong=ModelAssignment(model='gpt-6.1-sol')
    cheap=ModelAssignment(model='deepseek-flash',reasoning='disabled')
    return {name:Architecture(name=name, guards=name!='S0',
        review='self' if name=='S2' else 'independent' if name in {'M1','M2'} else 'none',
        experts=name in {'M1','M2'}, executor=cheap if name in {'S1c','M2'} else strong,
        reviewer=strong, expert=strong, historical_only=name=='S0')
        for name in ('S0','S1','S2','S1c','M1','M2')}
