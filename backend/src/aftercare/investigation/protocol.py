"""Pair the executable investigation prompt with its frozen evidence projection."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InvestigationProtocol:
    prompt_version: str
    evidence_projection: str | None


LEGACY_V6 = InvestigationProtocol("compact-v6", None)
HUMAN_PAYMENT_V7 = InvestigationProtocol("compact-v7", "human-material-payment-v1")


def protocol_for_settings(settings):
    version = getattr(settings, "chat_investigation_protocol", LEGACY_V6.prompt_version)
    for protocol in (LEGACY_V6, HUMAN_PAYMENT_V7):
        if protocol.prompt_version == version:
            return protocol
    raise ValueError("INVESTIGATION_PROMPT_VERSION_UNSUPPORTED")


def protocol_for_scope(scope):
    projection = (scope.frozen_context or {}).get("evidence_projection")
    for protocol in (LEGACY_V6, HUMAN_PAYMENT_V7):
        if protocol.evidence_projection == projection:
            return protocol
    if projection == "human-material-observations-v1":
        raise ValueError("INVESTIGATION_EVIDENCE_PROJECTION_READ_ONLY")
    raise ValueError("INVESTIGATION_EVIDENCE_PROJECTION_UNSUPPORTED")


def protocol_for_binding(binding, scope):
    protocol = protocol_for_scope(scope)
    if binding.get("investigation_prompt_version") != protocol.prompt_version:
        raise ValueError("INVESTIGATION_PROMPT_VERSION_CHANGED")
    if binding.get("investigation_evidence_projection") != protocol.evidence_projection:
        raise ValueError("INVESTIGATION_EVIDENCE_PROJECTION_MISMATCH")
    return protocol
