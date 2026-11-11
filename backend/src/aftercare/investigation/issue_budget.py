"""Cross-generation admission using the existing chat and run attempt ledgers."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from sqlalchemy import or_, select, text

from aftercare.conversation_analysis.tables import ChatModelAttempt
from aftercare.intelligence.tables import ModelAttempt, RunEvent


class IssueBudgetDenied(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IssueBudget:
    calls: int
    microusd: int

    @classmethod
    def from_settings(cls, settings):
        if not getattr(settings, "chat_investigation_enabled", False):
            return None
        return cls(
            getattr(settings, "chat_investigation_issue_max_calls", 16),
            getattr(settings, "chat_investigation_issue_max_microusd", 200_000),
        )


def to_microusd(value):
    try:
        amount = Decimal(value)
        if amount.is_finite() and amount >= 0:
            return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError, ValueError):
        pass
    raise IssueBudgetDenied("INVESTIGATION_ISSUE_COST_UNKNOWN")


def lock_issue(db, conversation_id, issue_id):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"chat-issue-budget:{conversation_id}:{issue_id}"},
        )


def admit_issue(db, conversation_id, issue_id, limits, reserve, *, protected_calls=0):
    """Caller holds the issue lock and persists STARTED before provider transport."""
    if reserve < 0 or not issue_id or not conversation_id:
        raise IssueBudgetDenied("INVESTIGATION_ISSUE_COST_UNKNOWN")
    bound_issue = ChatModelAttempt.model_evidence["issue_id"].as_string()
    images = list(
        db.scalars(
            select(ChatModelAttempt).where(
                ChatModelAttempt.conversation_id == conversation_id,
                or_(bound_issue == issue_id, bound_issue.is_(None)),
            )
        )
    )
    scope = RunEvent.payload["scope"]
    models = list(
        db.scalars(
            select(ModelAttempt)
            .join(RunEvent, RunEvent.run_id == ModelAttempt.run_id)
            .where(
                RunEvent.kind == "decision_input",
                RunEvent.payload["graph_profile"].as_string() == "chat-investigation-v1",
                scope["conversation_id"].as_string() == conversation_id,
                scope["issue_id"].as_string() == issue_id,
            )
        )
    )
    if len(images) + len(models) + protected_calls >= limits.calls:
        raise IssueBudgetDenied("INVESTIGATION_ISSUE_CALL_LIMIT")
    if any(attempt.budget_violation for attempt in images) or any(
        attempt.error_code == "INVESTIGATION_ESTIMATE_EXCEEDED" for attempt in models
    ):
        raise IssueBudgetDenied("INVESTIGATION_ISSUE_BUDGET_VIOLATION")
    image_charges = sum(
        max(row.reserved_microusd, row.estimated_cost_microusd or 0)
        if row.state in {"STARTED", "UNKNOWN"} or row.estimated_cost_microusd is None
        else row.estimated_cost_microusd
        for row in images
    )
    model_charges = 0
    for row in models:
        held = row.metadata_json.get("reserved_cost_usd")
        actual = row.estimated_cost_usd
        if row.status in {"STARTED", "UNKNOWN"} or actual is None:
            model_charges += max(
                to_microusd(held), to_microusd(actual) if actual is not None else 0
            )
        else:
            model_charges += to_microusd(actual)
    if image_charges + model_charges + reserve > limits.microusd:
        raise IssueBudgetDenied("INVESTIGATION_ISSUE_COST_LIMIT")
