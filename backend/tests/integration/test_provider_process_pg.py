"""Two independent OS processes contend for one provider slot in test PG."""
import multiprocessing
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.intelligence.model_limits import ProviderRateLease, ProviderRateScope

pytestmark = [pytest.mark.pg, pytest.mark.skipif(
    os.environ.get('AFTERCARE_PROVIDER_PROCESS_PG') != '1',
    reason='explicit isolated provider process acceptance',
)]


def _contender(scope, start, messages):
    from aftercare.intelligence.model_limits import ProviderLimiter, ProviderLimitError
    from aftercare.intelligence.models import ProviderLimit
    engine = None
    try:
        engine = guarded_engine(load_settings('test'), test_only=True)
        limit = ProviderLimit(scope=scope, max_in_flight=1, requests_per_minute=2)
        messages.put(('ready', os.getpid()))
        if not start.wait(15):
            messages.put(('error', 'START_TIMEOUT'))
            return
        try:
            ProviderLimiter(engine).claim(limit, 30)
            messages.put(('result', 'ALLOWED'))
        except ProviderLimitError as error:
            messages.put(('result', error.code))
    except Exception as error:  # noqa: BLE001 - return safe diagnostic without connection secrets
        messages.put(('error', type(error).__name__))
    finally:
        if engine is not None:
            engine.dispose()


def test_separate_processes_share_one_atomic_provider_quota():
    engine = guarded_engine(load_settings('test'), test_only=True)
    scope = 'process-acceptance-' + uuid4().hex
    context = multiprocessing.get_context('spawn')
    start, messages = context.Event(), context.Queue()
    children = [context.Process(target=_contender, args=(scope, start, messages)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        ready = [messages.get(timeout=20) for _ in children]
        assert all(kind == 'ready' for kind, _ in ready), ready
        assert len({pid for _, pid in ready}) == 2
        assert os.getpid() not in {pid for _, pid in ready}
        start.set()
        results = [messages.get(timeout=15) for _ in children]
        assert sorted(results) == [('result', 'ALLOWED'), ('result', 'PROVIDER_IN_FLIGHT_LIMIT')]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
    finally:
        start.set()
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        messages.close()
        with engine.begin() as conn:
            conn.execute(delete(ProviderRateLease).where(ProviderRateLease.scope == scope))
            conn.execute(delete(ProviderRateScope).where(ProviderRateScope.scope == scope))
        engine.dispose()
