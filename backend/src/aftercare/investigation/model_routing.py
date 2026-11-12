"""Task-based selection: economy extraction, strong decisions, no silent downgrade."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Selection:
    preferred: str
    fixed: bool
    reason: str


def select_model(strategy, role, phase_id, registry):
    if strategy != 'routed':
        return Selection(strategy, True, 'CONTROLLED_FIXED_MODEL')
    # Rechecks can change the decision: never degrade verification to save money.
    if role != 'fact' or phase_id == 'recheck':
        return Selection('analysis', True, 'DECISION_OR_POLICY_REQUIRES_STRONG_MODEL')
    aliases = {model.alias for model in getattr(registry, "models", ())}
    if 'economy' not in aliases:
        return Selection('analysis', True, 'ECONOMY_MODEL_NOT_CONFIGURED')
    return Selection('economy', False, 'FACT_EXTRACTION_ECONOMY_FIRST')
