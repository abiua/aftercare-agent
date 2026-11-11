"""Versioned contracts for input-first chat review."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Key = Annotated[str, Field(min_length=1, max_length=160)]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Note = Annotated[str, Field(max_length=2000)]
Gap = Annotated[str, Field(min_length=1, max_length=160)]
CandidateText = Annotated[str, Field(min_length=1, max_length=1000)]
Amount = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
Action = Literal[
    "REFUND", "REPLACE", "REQUEST_EVIDENCE", "NOT_SUPPORTED", "HUMAN_HANDOFF", "STATUS_ONLY"
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FrozenMessage(Contract):
    id: Key
    sequence: Amount
    text: Annotated[str, Field(max_length=4000)]


class FrozenImage(Contract):
    id: Key
    message_id: Key
    sha256: Sha
    size: Annotated[int, Field(strict=True, gt=0, le=8 * 1024 * 1024)]
    content_type: Literal["image/jpeg", "image/png", "image/webp"]


class FrozenFact(Contract):
    id: Key
    kind: Literal["ORDER_LINE", "CONDITION", "MATERIAL", "REVIEW", "EXECUTION", "OBSERVATION"]
    trust: Literal["VERIFIED_RECORD", "UNVERIFIED_OBSERVATION"]
    data: dict


class FrozenPolicy(Contract):
    id: Key
    version: Key
    title: Annotated[str, Field(max_length=500)]
    text: Annotated[str, Field(min_length=1, max_length=12000)]
    parameters: dict
    conditions: dict


class BlindInput(Contract):
    schema_version: Literal["chat-blind-input-v1", "chat-blind-input-v2"] = "chat-blind-input-v1"
    intent: Key
    messages: Annotated[list[FrozenMessage], Field(min_length=1, max_length=250)]
    images: Annotated[list[FrozenImage], Field(max_length=100)]
    facts: Annotated[list[FrozenFact], Field(max_length=200)]
    policy_coverage: Literal["SCOPED_ONLY", "COMPLETE_CATALOGUE"]
    policy_context: dict | None
    policies: Annotated[list[FrozenPolicy], Field(max_length=100)]


class PlannedLine(Contract):
    line_id: Key
    amount_min_cents: Amount | None = None
    amount_max_cents: Amount | None = None
    quantity: Amount | None = None


class AcceptablePlan(Contract):
    action: Action
    lines: Annotated[list[PlannedLine], Field(max_length=30)] = Field(default_factory=list)
    required_evidence: Annotated[list[Gap], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self):
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Plan line IDs must be unique")
        if self.action in {"REFUND", "REPLACE"} and not self.lines:
            raise ValueError("Executable plans require lines")
        if self.action == "REQUEST_EVIDENCE" and not self.required_evidence:
            raise ValueError("Evidence requests require a gap")
        for line in self.lines:
            if self.action == "REFUND":
                valid = (
                    line.quantity is None
                    and line.amount_min_cents is not None
                    and line.amount_max_cents is not None
                    and line.amount_min_cents <= line.amount_max_cents
                )
            elif self.action == "REPLACE":
                valid = (
                    line.quantity is not None
                    and line.amount_min_cents is None
                    and line.amount_max_cents is None
                )
            else:
                valid = (
                    line.quantity is None
                    and line.amount_min_cents is None
                    and line.amount_max_cents is None
                )
            if not valid:
                raise ValueError("Invalid amounts or quantities for plan action")
        return self


class PolicyAssessment(Contract):
    policy_id: Key
    version: Key
    status: Literal["APPLICABLE", "NOT_APPLICABLE", "UNKNOWN"]


class Versioned(Contract):
    expected_version: Amount


class InitialJudgment(Versioned):
    judgment: Literal["DECIDABLE", "UNDETERMINED"]
    acceptable_plans: Annotated[list[AcceptablePlan], Field(max_length=12)] = Field(
        default_factory=list
    )
    required_fact_ids: Annotated[list[Key], Field(max_length=80)] = Field(default_factory=list)
    policy_assessments: Annotated[list[PolicyAssessment], Field(max_length=80)] = Field(
        default_factory=list
    )
    notes: Note = ""

    @model_validator(mode="after")
    def validate_independent_label(self):
        if bool(self.acceptable_plans) != (self.judgment == "DECIDABLE"):
            raise ValueError("Only a decidable judgment provides acceptable plans")
        if len(set(self.required_fact_ids)) != len(self.required_fact_ids):
            raise ValueError("Duplicate fact assessment")
        if len({(item.policy_id, item.version) for item in self.policy_assessments}) != len(
            self.policy_assessments
        ):
            raise ValueError("Duplicate policy assessment")
        return self


class CandidateLine(Contract):
    line_id: Key
    amount_cents: Amount | None = None
    quantity: Amount | None = None


class RevealedReference(Contract):
    id: Key
    status: Literal["RESOLVED", "UNRESOLVED"]
    resolved_ids: Annotated[list[Key], Field(max_length=100)]


class CandidateOption(Contract):
    id: Key
    action: Action
    lines: Annotated[list[CandidateLine], Field(max_length=30)]
    required_evidence: Annotated[list[CandidateText], Field(max_length=20)]
    evidence_ids: Annotated[list[Key], Field(max_length=100)]
    policy_ids: Annotated[list[Key], Field(max_length=100)]
    rationale: CandidateText
    alternatives: Annotated[list[CandidateText], Field(max_length=10)]
    evidence_references: Annotated[list[RevealedReference], Field(max_length=100)]
    policy_references: Annotated[list[RevealedReference], Field(max_length=100)]


class BlindCandidate(Contract):
    id: Key
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    options: Annotated[list[CandidateOption], Field(max_length=8)]
    unresolved: Annotated[list[CandidateText], Field(max_length=20)] = Field(default_factory=list)


class CandidateLabel(Contract):
    candidate_id: Key
    decision: Literal["ACCEPTABLE", "UNACCEPTABLE", "UNDETERMINED"]
    notes: Note = ""


class PostLabels(Versioned):
    items: Annotated[list[CandidateLabel], Field(min_length=1, max_length=12)]


class OptionLabel(Contract):
    candidate_id: Key
    option_id: Key
    decision: Literal["ACCEPTABLE", "UNACCEPTABLE", "UNDETERMINED"]


class PostOptionLabelsV2(PostLabels):
    label_schema_version: Literal["chat-blind-labels-v2"]
    option_labels: Annotated[list[OptionLabel], Field(max_length=96)]
