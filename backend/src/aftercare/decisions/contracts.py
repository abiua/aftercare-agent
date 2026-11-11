from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from aftercare.business.rules import digest

Key = Annotated[str, Field(min_length=1, max_length=80)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Positive = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
Action = Literal["REFUND", "REPLACE"]
ReviewAction = Literal["ACCEPT", "MODIFY", "REJECT", "REQUEST_EVIDENCE", "TAKE_OVER"]
ResponseAction = Literal["ACCEPT", "REJECT", "REQUEST_CHANGE"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class InputReference(Contract):
    kind: Literal["MESSAGE", "ATTACHMENT", "FACT", "POLICY", "OBSERVATION"]
    id: Key
    sha256: Sha


class DecisionScope(Contract):
    conversation_id: Key
    issue_id: Key
    case_id: Key
    order_id: Key
    customer_id: Key
    line_ids: Annotated[tuple[Key, ...], Field(min_length=1, max_length=30)]
    input_through_seq: Positive
    input_sha256: Sha
    input_refs: Annotated[tuple[InputReference, ...], Field(min_length=1, max_length=250)]
    evidence_graph_sha256: Sha
    evidence_ids: Annotated[tuple[Key, ...], Field(max_length=100)]
    policy_catalogue_sha256: Sha
    dependency_sha256: Sha
    business_versions: dict[Key, int | str]
    frozen_context: dict | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_snapshot(self, handler):
        data = handler(self)
        if self.frozen_context is None:
            data.pop("frozen_context", None)
        return data

    @model_validator(mode="after")
    def verify_scope(self):
        if (
            not self.business_versions
            or len(self.line_ids) != len(set(self.line_ids))
            or len(self.evidence_ids) != len(set(self.evidence_ids))
            or len(self.input_refs) != len({(row.kind, row.id) for row in self.input_refs})
        ):
            raise ValueError("Decision scope contains duplicate or missing bindings")
        if self.frozen_context is not None and (
            not self.frozen_context
            or self.input_sha256
            != digest(
                [[row.model_dump(mode="json") for row in self.input_refs], self.frozen_context]
            )
        ):
            raise ValueError("Decision input digest does not match frozen context")
        return self


class DecisionRef(Contract):
    id: Key
    version: Positive
    sha256: Sha


class DecisionLine(Contract):
    line_id: Key
    amount_cents: Positive | None = None
    quantity: Positive | None = None


class Option(Contract):
    id: Key
    action: Action
    lines: Annotated[tuple[DecisionLine, ...], Field(min_length=1, max_length=30)]
    evidence_ids: Annotated[tuple[Key, ...], Field(min_length=1, max_length=100)]
    policy_ids: Annotated[tuple[Key, ...], Field(min_length=1, max_length=30)]
    rationale: Text

    @model_validator(mode="after")
    def verify_lines(self):
        if len({row.line_id for row in self.lines}) != len(self.lines):
            raise ValueError("Order lines must be unique")
        for row in self.lines:
            if (row.amount_cents is not None) != (self.action == "REFUND") or (
                row.quantity is not None
            ) != (self.action == "REPLACE"):
                raise ValueError("Line measure does not match action")
        return self

    def business_lines(self):
        return [row.model_dump(exclude_none=True) for row in self.lines]


class NonExecutingOption(Contract):
    id: Key
    action: Literal["REQUEST_EVIDENCE", "NOT_SUPPORTED", "HUMAN_HANDOFF"]
    target_line_ids: Annotated[tuple[Key, ...], Field(min_length=1, max_length=30)]
    evidence_ids: Annotated[tuple[Key, ...], Field(max_length=100)] = ()
    rationale: Text
    missing_evidence: Annotated[tuple[Text, ...], Field(max_length=20)] = ()
    alternatives: Annotated[tuple[Text, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def verify_gap(self):
        if self.action == "REQUEST_EVIDENCE" and not self.missing_evidence:
            raise ValueError("Evidence requests require a specific gap")
        if len(set(self.target_line_ids)) != len(self.target_line_ids):
            raise ValueError("Target order lines must be unique")
        return self


CandidateOption = Annotated[Option | NonExecutingOption, Field(discriminator="action")]


class Candidate(Contract):
    run_id: Key
    graph_version: Key
    route_version: Key
    model_binding_sha256: Sha
    options: Annotated[tuple[CandidateOption, ...], Field(min_length=1, max_length=8)]
    unresolved: Annotated[tuple[Text, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def verify_options(self):
        if len({row.id for row in self.options}) != len(self.options):
            raise ValueError("Provide distinct options")
        return self


class Review(Contract):
    action: ReviewAction
    reason: Text
    option_id: Key | None = None
    replacement: CandidateOption | None = None
    resolved_items: Annotated[tuple[Text, ...], Field(max_length=20)] = ()
    new_evidence_ids: Annotated[tuple[Key, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def verify_action(self):
        if (self.option_id is not None) != (self.action == "ACCEPT") or (
            self.replacement is not None
        ) != (self.action == "MODIFY"):
            raise ValueError("Review does not match its proposed option")
        if self.resolved_items and self.action != "MODIFY":
            raise ValueError("Resolved items belong to a modification")
        return self


class CustomerResponse(Contract):
    action: ResponseAction
    reason: Text | None = None
