"""Real PostgreSQL concurrency using only a throwaway queue schema."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.dispatch import DeliveryJournal, DispatchEvent, DispatchJob
from aftercare.evaluation.tables import Experiment

pytestmark = [pytest.mark.pg, pytest.mark.skipif(
    os.environ.get('AFTERCARE_DISPATCH_PG') != '1', reason='explicit isolated PG acceptance'
)]


def test_concurrent_claim_lease_expiry_and_stale_owner_fencing():
    engine = guarded_engine(load_settings('test'), test_only=True)
    schema = 'queue_acceptance_' + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        Experiment.__table__.create(scoped)
        DispatchJob.__table__.create(scoped)
        DispatchEvent.__table__.create(scoped)
        stamp = [datetime.now(UTC)]
        journal = DeliveryJournal(scoped, clock=lambda: stamp[0], lease_seconds=10)
        job = journal.ensure('agent', str(uuid4()))
        with ThreadPoolExecutor(max_workers=8) as pool:
            owners = list(pool.map(lambda _: journal.claim(job), range(8)))
        admitted = [owner for owner in owners if owner is not None]
        assert len(admitted) == 1
        old = admitted[0]
        stamp[0] += timedelta(seconds=11)
        assert not journal.renew(job, old)
        publication = journal.reserve_publish(job)
        assert publication
        new = journal.claim(job)
        assert new and new != old
        assert not journal.finish(job, old)
        assert journal.finish(job, new)
        assert journal.claim(job) is None
        assert journal.due() == []
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_public_transaction_fence_blocks_takeover_until_commit():
    engine = guarded_engine(load_settings('test'), test_only=True)
    stamp = [datetime.now(UTC)]
    journal = DeliveryJournal(engine, clock=lambda: stamp[0], lease_seconds=10)
    job = journal.ensure('agent', str(uuid4()))
    try:
        owner = journal.claim(job)
        with Session(engine) as session, session.begin():
            assert journal.owned(job, owner, session=session)
            stamp[0] += timedelta(seconds=11)
            with pytest.raises(OperationalError) as failure, engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '100ms'"))
                conn.execute(text('UPDATE public.dispatch_jobs SET owner=NULL WHERE id=:id'), {'id':job})
            assert failure.value.orig.sqlstate == '55P03'
        assert journal.reserve_publish(job)
        replacement = journal.claim(job)
        assert replacement and replacement != owner
        assert not journal.finish(job, owner)
    finally:
        with engine.begin() as conn:
            conn.execute(delete(DispatchEvent).where(DispatchEvent.job_id == job))
            conn.execute(delete(DispatchJob).where(DispatchJob.id == job))
        engine.dispose()
