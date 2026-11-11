"""File contracts for the local formal-evaluation register."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

from ..gates import GatePolicy

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PathText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Intent = Literal["damaged", "wrong_item", "refund_status"]
Strategy = Literal["economy", "analysis", "routed"]
Condition = Literal[
    "single/economy", "single/analysis", "single/routed",
    "multi/economy", "multi/analysis", "multi/routed",
]
SingleConditions = frozenset({"single/economy", "single/analysis", "single/routed"})
ModelCallExpectation = Literal[
    "MODEL_REQUIRED", "WAITING_INPUT", "MATERIAL_REVIEW_REQUIRED", "POLICY_BLOCKED", "TARGETED_REFUND"
]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class HumanRecord(Document):
    """A submitter's declaration, with no authentication of the person named."""

    actor: Note
    at: AwareDatetime
    source: Note
    identity_verification: Literal["UNAUTHENTICATED_DECLARATION"] = "UNAUTHENTICATED_DECLARATION"


class Candidate(Document):
    purpose: Literal["formal", "fixture"]
    dataset_id: Identifier
    manifest_sha256: Digest
    distribution: dict[Intent, Annotated[int, Field(strict=True, ge=1, le=144)]]
    grouping_evidence: Note
    custodian: HumanRecord
    exposed_before_freeze: bool

    @model_validator(mode="after")
    def validate_distribution(self):
        if len(self.distribution) != 3 or sum(self.distribution.values()) > 144:
            raise ValueError("Three intents and at most 144 independent cases are required")
        return self


class ValidationReport(Document):
    group: Literal["A", "B"]
    path: PathText
    sha256: Digest


class ValidationCase(Document):
    case_sha256: Digest
    reference_sha256: Digest
    model_call_expectations: dict[str, ModelCallExpectation] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_digest(self, handler):
        value = handler(self)
        if self.model_call_expectations is None:
            value.pop("model_call_expectations", None)
        return value


class ValidationPlan(Document):
    dataset_id: Identifier
    cases: Annotated[dict[str, ValidationCase], Field(min_length=1, max_length=144)]
    repetitions: Annotated[int, Field(strict=True, ge=1, le=3)]
    config_sha256: Annotated[dict[str, Digest], Field(min_length=4, max_length=4)]
    policies_digest: Digest
    retrieval_digest: Digest

    @model_validator(mode="after")
    def require_matrix(self):
        single = SingleConditions
        extra = set(self.config_sha256) - single
        if (
            not single <= self.config_sha256.keys()
            or extra
            not in (
                {"multi/economy"},
                {"multi/analysis"},
                {"multi/routed"},
            )
            or any(not 1 <= len(case_id) <= 64 for case_id in self.cases)
            or any(
                not set(case.model_call_expectations or {}) <= self.config_sha256.keys()
                for case in self.cases.values()
            )
        ):
            raise ValueError("Invalid validation matrix")
        return self


class Validation(Document):
    plan_sha256: Digest
    reports: Annotated[list[ValidationReport], Field(min_length=5, max_length=5)]
    selected_experiment_id: Note
    rationale: Note
    selection: HumanRecord


class ReportSnapshot(Document):
    group: Literal["A", "B"]
    experiment_id: Note
    report_sha256: Digest
    config_sha256: Digest
    source: Literal["live", "fixture"]
    mode: Literal["single", "multi"]
    strategy: Literal["economy", "analysis", "routed"]
    counts: dict[str, int]
    code_digest: Digest
    scorer_version: Note
    metrics_version: Note
    model_snapshot_digest: Digest | None


class ValidationSnapshot(Document):
    plan_sha256: Digest
    reports: Annotated[list[ReportSnapshot], Field(min_length=5, max_length=5)]
    selected_experiment_id: Note
    rationale: Note
    selection: HumanRecord
    source_verification: Literal["UNVERIFIED_LOCAL_REPORT"] = "UNVERIFIED_LOCAL_REPORT"
    human_identity: Literal["UNAUTHENTICATED_DECLARATION"] = "UNAUTHENTICATED_DECLARATION"


class NativeExperiment(Document):
    group: Literal["A", "B"]
    experiment_id: UUID


class NativeValidation(Document):
    plan_sha256: Digest
    experiments: Annotated[list[NativeExperiment], Field(min_length=5, max_length=5)]
    selected_experiment_id: UUID
    rationale: Note
    selection: HumanRecord


class NativeExperimentEvidence(Document):
    version: int
    status: str
    machine_statuses: dict[str, int]
    effective_statuses: dict[str, int]
    attempt_statuses: dict[str, int]
    frozen_attempt_statuses: dict[str, int]
    findings: dict[str, int]
    quota_sha256: Digest
    audit_sha256: Digest
    index_sha256: Digest
    source_blockers: list[str] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_digest(self, handler):
        value = handler(self)
        if self.source_blockers is None:
            value.pop("source_blockers", None)
        return value


class NativeReportSnapshot(ReportSnapshot):
    code_digest: Digest | None
    scorer_version: Note | None
    metrics_version: Note | None


class RuntimeControls(Document):
    schema_version: Literal["ac-formal-runtime-controls-v1"]
    mode: Literal["single", "multi"]
    strategy: Strategy
    source: Literal["live"]
    probe_intent: Annotated[bool, Field(strict=True)]
    code_digest: Digest
    dependency_lock_sha256: Digest
    scorer_version: Note
    metrics_version: Note
    model_snapshot_sha256: Digest
    registry_sha256: Digest
    policy_schema_version: Note
    policy_selector_sha256: Digest
    retrieval_sha256: Digest
    embedding_enabled: Annotated[bool, Field(strict=True)]
    embedding_model: Note | None


class ConditionControls(Document):
    validation_experiment_id: UUID
    config_sha256: Digest
    controls: RuntimeControls
    controls_sha256: Digest
    gate_policy: GatePolicy | None


class NativeValidationSnapshot(ValidationSnapshot):
    source_verification: Literal["NATIVE_LEDGER_ONLY", "NATIVE_VALIDATION_VERIFIED"] = "NATIVE_LEDGER_ONLY"
    reports: Annotated[list[NativeReportSnapshot], Field(min_length=5, max_length=5)]
    read_at: AwareDatetime
    evidence: dict[str, NativeExperimentEvidence]
    controls_by_condition: dict[Condition, ConditionControls] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_digest(self, handler):
        value = handler(self)
        if self.controls_by_condition is None:
            value.pop("controls_by_condition", None)
        return value


class FreezeDraft(Document):
    candidate_sha256: Digest
    validation_sha256: Digest
    code_digest: Digest
    model_snapshot_digest: Digest | None
    policies_digest: Digest
    retrieval_digest: Digest
    scorer_version: Note
    metrics_version: Note
    gate_policy: GatePolicy
    repetitions: Annotated[int, Field(strict=True, ge=1, le=3)]
    max_calls: Annotated[int, Field(strict=True, ge=1, le=144)]
    budget_usd: Annotated[float, Field(ge=0, le=10)]


class FreezeQuota(Document):
    calls: Annotated[int, Field(strict=True, ge=1, le=576)]
    budget_microusd: Annotated[int, Field(strict=True, ge=1, le=40_000_000)]


class FreezeCondition(Document):
    validation_experiment_id: UUID
    controls_sha256: Digest
    quota: FreezeQuota


class FormalFreezeDraftV2(Document):
    schema_version: Literal["ac-formal-freeze-draft-v2"]
    candidate_sha256: Digest
    manifest_sha256: Digest
    validation_sha256: Digest
    validation_plan_sha256: Digest
    selected_validation_experiment_id: UUID
    gate_policy: GatePolicy
    group_b_strategy: Strategy
    block_order: Annotated[list[Condition], Field(min_length=4, max_length=4)]
    conditions: dict[Condition, FreezeCondition]
    row_order: Literal["REPETITION_THEN_CASE_ID_ASC"]
    repetitions: Annotated[int, Field(strict=True, ge=1, le=3)]
    case_count: Annotated[int, Field(strict=True, ge=1, le=144)]
    row_quota: FreezeQuota
    campaign_quota: FreezeQuota

    @model_validator(mode="after")
    def validate_schedule(self):
        keys = SingleConditions | {f"multi/{self.group_b_strategy}"}
        if (
            set(self.conditions) != keys
            or set(self.block_order) != keys
            or len({block.validation_experiment_id for block in self.conditions.values()}) != 4
        ):
            raise ValueError("Four unique conditions with a shared single-agent baseline are required")
        rows = self.case_count * self.repetitions
        calls = self.row_quota.calls * rows
        fee = self.row_quota.budget_microusd * rows
        if (
            calls > 144 or fee > 10_000_000
            or any(
                block.quota.calls != calls or block.quota.budget_microusd != fee
                for block in self.conditions.values()
            )
            or self.campaign_quota.calls < calls * 4
            or self.campaign_quota.budget_microusd < fee * 4
        ):
            raise ValueError("Condition quotas must cover exactly their rows and fit campaign caps")
        return self


class FormalFreezeDraftV3(FormalFreezeDraftV2):
    """For each repetition and case, execute every condition in the frozen order."""

    schema_version: Literal["ac-formal-freeze-draft-v3"]
    schedule: Literal["REPETITION_CASE_CONDITION"]


class FormalExecutionDeployment(Document):
    """Local deployment limit bound to one already approved freeze."""

    schema_version: Literal["ac-formal-execution-deployment-v1"]
    campaign_id: Identifier
    freeze_sha256: Digest
    execution_cap: FreezeQuota


def parse_freeze_draft(value):
    if isinstance(value, dict) and value.get("schema_version") == "ac-formal-freeze-draft-v3":
        return FormalFreezeDraftV3.model_validate(value)
    if isinstance(value, dict) and "schema_version" in value:
        return FormalFreezeDraftV2.model_validate(value)
    return FreezeDraft.model_validate(value)


class FreezeConfirmation(Document):
    draft_sha256: Digest
    decision: Literal["CONFIRMED"]
    human: HumanRecord


class Unlock(Document):
    freeze_sha256: Digest
    directory: PathText
    handoff: HumanRecord


class PackageManifest(Document):
    schema_version: Literal["ac-formal-package-v1"]
    dataset_id: Identifier
    cases_sha256: Digest
    references_sha256: Digest


class ExposureEvent(Document):
    kind: Literal[
        "EXPOSED_BEFORE_FREEZE", "OPENING", "CASES_READ", "UNLOCKED", "EXPOSURE_UNCERTAIN"
    ]
    recorded_at: AwareDatetime
    human: HumanRecord
    reason: Note
    freeze_sha256: Digest | None = None
    package: PackageManifest | None = None
    case_fingerprints: Annotated[list[Digest], Field(max_length=288)] | None = None
