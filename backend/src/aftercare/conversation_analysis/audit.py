"""ModelRunner's synchronous single-attempt vision audit adapter."""

from decimal import ROUND_CEILING, Decimal

from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import utc
from aftercare.intelligence.model_limits import ProviderLimiter
from aftercare.intelligence.models import ModelFailure


def _micros(value):
    if value is None:
        return None
    return int((Decimal(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class ChatAnalysisAudit:
    def __init__(self, service, lease, *, limiter=None):
        self.service, self.lease = service, lease
        self.limiter = limiter if limiter is not None else ProviderLimiter(service.engine)
        self.attempt_id = None
        self.logical_id = None

    def cooldown_aliases(self, _seconds):
        return set()

    def replay(self, logical_id):
        with Session(self.service.engine) as db, db.begin():
            _, turn = self.service._active(db, self.lease.turn_id, self.lease.token)
            if turn.vision_input is None or turn.vision_input["logical_id"] != logical_id:
                raise ModelFailure("VISION_INPUT_UNBOUND")
            if self.service._attempt(db, turn.id) is not None:
                raise ModelFailure("ANALYSIS_ATTEMPT_NOT_REPLAYED")

    def budget(self, _registry):
        with Session(self.service.engine) as db, db.begin():
            _, turn = self.service._active(db, self.lease.turn_id, self.lease.token)
            return {
                "attempts_used": int(self.service._attempt(db, turn.id) is not None),
                "attempts_limit": 1,
                "remaining_seconds": max(
                    0, (utc(turn.lease_until) - self.service.clock()).total_seconds()
                ),
            }

    def event(self, kind, payload):
        if kind == "vision_input":
            self.service.bind_vision_input(self.lease.turn_id, self.lease.token, payload)

    def claim_provider(self, spec, registry):
        policy = registry.provider_limits.get(spec.provider)
        if policy is None:
            return None
        return self.limiter.claim(
            policy, registry.deadline_seconds + 2 * registry.timeout_seconds + 5
        )

    def release_provider(self, slot, *, attempted, uncertain=False, cooldown_seconds=0):
        if slot is not None:
            self.limiter.release(
                slot,
                attempted=attempted,
                uncertain=uncertain,
                cooldown_seconds=cooldown_seconds,
            )

    def begin_attempt(
        self,
        logical_id,
        ordinal,
        spec,
        reserve,
        registry,
        *,
        transport_evidence=None,
        base_registry_digest=None,
    ):
        if ordinal != 1 or reserve is None:
            raise ModelFailure("ANALYSIS_BUDGET_EXCEEDED")
        caps = [value for value in (registry.max_usd, registry.max_run_usd) if value is not None]
        if not caps or reserve > min(caps):
            raise ModelFailure("ANALYSIS_BUDGET_EXCEEDED")
        try:
            self.attempt_id, remaining = self.service.reserve(
                self.lease.turn_id,
                self.lease.token,
                _micros(reserve),
                {
                    "logical_id": logical_id,
                    "ordinal": ordinal,
                    "alias": spec.alias,
                    "provider": spec.provider,
                    "model": spec.model,
                    "registry_version": registry.version,
                    "base_registry_digest": base_registry_digest,
                    "transport_evidence": transport_evidence,
                },
            )
        except BusinessError as error:
            raise ModelFailure(error.code) from None
        self.logical_id = logical_id
        return min(remaining, registry.deadline_seconds)

    def finish_attempt(
        self, logical_id, ordinal, status, code, usage, price, value, *, transport_evidence=None
    ):
        if self.attempt_id is None or logical_id != self.logical_id or ordinal != 1:
            raise ModelFailure("ANALYSIS_ATTEMPT_NOT_FOUND")
        self.service.finish_attempt(
            self.attempt_id,
            status=status,
            usage=usage,
            estimated_cost_microusd=_micros(price),
            response_evidence=transport_evidence,
            error_code=code,
            validated_result=value,
        )

    def confirm_admission(self):
        try:
            self.service.confirm(self.lease.turn_id, self.lease.token)
        except BusinessError as error:
            raise ModelFailure(error.code) from None

    def confirm_inflight_admission(self):
        self.confirm_admission()
