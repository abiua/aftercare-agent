from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from aftercare.intelligence import model_limits
from aftercare.intelligence.model_limits import (
    ProviderLimiter,
    ProviderRateLease,
    ProviderRateScope,
)
from aftercare.intelligence.models import ProviderLimit


@pytest.fixture
def limiter(monkeypatch):
    now = [datetime(2026, 11, 10, tzinfo=UTC)]
    monkeypatch.setattr(model_limits, "_clock", lambda _: now[0])
    engine = create_engine("sqlite://")
    ProviderRateScope.__table__.create(engine)
    ProviderRateLease.__table__.create(engine)
    return now, ProviderLimiter(engine), ProviderLimiter(engine)


def test_slot_concurrency_scope_rpm_and_unattempted_release(limiter):
    now, first, second = limiter
    config = ProviderLimit(scope="same-account", max_in_flight=1, requests_per_minute=2)
    started = first.claim(config, 30)
    with pytest.raises(model_limits.ProviderLimitError, match="IN_FLIGHT"):
        second.claim(config, 30)
    first.release(started, attempted=True, uncertain=False, cooldown_seconds=0)
    unused = second.claim(config, 30)
    second.release(unused, attempted=False, uncertain=False, cooldown_seconds=0)
    next_slot = second.claim(config, 30)
    second.release(next_slot, attempted=True, uncertain=False, cooldown_seconds=0)
    with pytest.raises(model_limits.ProviderLimitError, match="REQUEST_RATE"):
        first.claim(config, 30)
    now[0] += timedelta(seconds=61)
    assert first.claim(config, 30).scope == "same-account"


def test_uncertain_lease_and_cooldown_hold_across_instances(limiter):
    now, first, second = limiter
    config = ProviderLimit(scope="shared", max_in_flight=1, requests_per_minute=10)
    slot = first.claim(config, 30)
    first.release(slot, attempted=True, uncertain=True, cooldown_seconds=3)
    with pytest.raises(model_limits.ProviderLimitError, match="COOLDOWN"):
        second.claim(config, 30)
    now[0] += timedelta(seconds=4)
    with pytest.raises(model_limits.ProviderLimitError, match="IN_FLIGHT"):
        second.claim(config, 30)
    now[0] += timedelta(seconds=27)
    assert second.claim(config, 30).id != slot.id


def test_postgres_two_workers_share_scope():
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from aftercare.config import load_settings
    from aftercare.db import guarded_engine

    if os.environ.get("AFTERCARE_INTELLIGENCE_PG") != "1":
        pytest.skip("Run serially only in the isolated migrated PG test profile")
    engine = guarded_engine(load_settings("test"), test_only=True)
    try:
        config = ProviderLimit(
            scope=f"pg-test-{uuid4().hex}", max_in_flight=1, requests_per_minute=2
        )
        ready = Barrier(2, timeout=5)

        def claim(_index):
            limiter = ProviderLimiter(engine)
            ready.wait()
            try:
                return limiter.claim(config, 30)
            except model_limits.ProviderLimitError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        assert sum(not isinstance(result, str) for result in results) == 1
        assert "PROVIDER_IN_FLIGHT_LIMIT" in results
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ProviderRateLease).where(ProviderRateLease.scope == config.scope))
            db.execute(delete(ProviderRateScope).where(ProviderRateScope.scope == config.scope))
        engine.dispose()
