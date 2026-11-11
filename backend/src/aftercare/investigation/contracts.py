"""Contracts for the versioned, read-only chat investigation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["strong_single", "fact", "policy", "verifier"]
SourceKind = Literal[
    "MESSAGE",
    "ATTACHMENT",
    "OBSERVATION",
    "MATERIAL",
    "REVIEW",
    "ORDER_LINE",
    "EXECUTION",
    "LEDGER",
    "POLICY",
    "CONDITION",
]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class EvidenceRef(Contract):
    source_kind: SourceKind
    source_id: str
    scope: tuple[str, str, str]
    version_or_sha256: str
    observed_at: str | None = None


class CustomerMessage(Contract):
    ref: EvidenceRef
    sequence: int
    text: str


class Evidence(Contract):
    ref: EvidenceRef
    data: dict
    trust: Literal["VERIFIED_RECORD", "CUSTOMER_ATTACHMENT", "UNVERIFIED_OBSERVATION", "POLICY"]


class InvestigationInput(Contract):
    conversation_id: str
    issue_id: str
    case_id: str
    order_id: str
    input_through_seq: int
    source_snapshot_sha256: str
    relevant_input_sha256: str
    messages: tuple[CustomerMessage, ...]
    evidence: tuple[Evidence, ...]
    eligibility: dict
    policy_blockers: tuple[str, ...]
    business_versions: dict
    intent: str
    policy_catalogue_version: str | None = None
    policy_context: dict = Field(default_factory=dict)
    selected_policy_keys: tuple[str, ...] = ()

    def role_view(self, role: Role) -> dict:
        """The graph should send this projection, never the complete snapshot."""
        result = {"intent": self.intent, "input_digest": self.relevant_input_sha256}
        if role in {"strong_single", "fact"} or (
            role == "policy" and self.policy_catalogue_version
        ):
            result["customer_statements"] = [msg.model_dump(mode="json") for msg in self.messages]
        if role in {"strong_single", "fact"}:
            result["attachments"] = self._visible("ATTACHMENT")
            result["unverified_observations"] = self._visible("OBSERVATION")
        if role in {"strong_single", "fact"}:
            kinds = {"ORDER_LINE", "MATERIAL", "REVIEW", "ATTACHMENT", "OBSERVATION", "EXECUTION"}
        elif role == "policy":
            kinds = {"POLICY", "CONDITION"}
        else:
            kinds = {"POLICY"} if role == "verifier" and self.policy_catalogue_version else set()
        if role == "strong_single":
            kinds |= {"POLICY", "CONDITION"}
        if role in {"strong_single", "policy", "verifier"} and self.policy_catalogue_version:
            result["policy_context"] = self.policy_context
        result["available_evidence"] = [
            row.ref.model_dump(mode="json") for row in self.evidence if row.ref.source_kind in kinds
        ]
        return result

    def _visible(self, kind):
        return [row.model_dump(mode="json") for row in self.evidence if row.ref.source_kind == kind]


class RouteDecision(Contract):
    version: Literal["chat-route-v1"] = "chat-route-v1"
    path: Literal["PROGRAM", "SINGLE", "MULTI"]
    reason: str
    complexity_features: tuple[str, ...] = ()
    financial_risk: dict
    evidence_refs: tuple[EvidenceRef, ...] = ()
    output: dict = Field(default_factory=dict)
