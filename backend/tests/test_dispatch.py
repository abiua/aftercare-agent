from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare import dispatch as module
from aftercare.evaluation.tables import Experiment


@pytest.fixture
def journal():
    engine=create_engine('sqlite://')
    module.DispatchJob.__table__.create(engine)
    module.DispatchEvent.__table__.create(engine)
    Experiment.__table__.create(engine)
    stamp=[datetime(2026,11,10,tzinfo=UTC)]
    instance=module.DeliveryJournal(engine,clock=lambda:stamp[0],lease_seconds=10,max_failures=2)
    yield instance,stamp
    engine.dispose()

def test_lost_publish_recovered_and_no_duplicate_claim(journal):
    j,stamp=journal; target=str(uuid4());job=j.ensure('agent',target)
    assert job==j.ensure('agent',target)
    first=j.reserve_publish(job)
    assert first and not j.reserve_publish(job)
    stamp[0]+=timedelta(seconds=31)
    second=j.reserve_publish(job)
    assert second['publication']==2
    owner=j.claim(job)
    j.published(job,2)
    assert not j.claim(job)
    assert j.owned(job,owner)
    assert j.finish(job,owner)
    assert not j.claim(job)
    assert j.ensure('agent',target,1)!=job

def test_old_worker_cannot_publish_after_lease_expires(journal):
    j,stamp=journal;job=j.ensure('evaluation',str(uuid4()));old=j.claim(job)
    stamp[0]+=timedelta(seconds=11)
    assert not j.renew(job,old)
    assert not j.finish(job,old)
    assert job in j.due()
    j.reserve_publish(job);new=j.claim(job)
    assert new!=old and not j.owned(job,old)
    assert not j.finish(job,old)
    assert j.finish(job,new)

def test_broker_errors_are_bounded_and_traced(journal):
    j,stamp=journal;job=j.ensure('agent',str(uuid4()))
    for _ in range(2):
        publish=j.reserve_publish(job);j.publish_failed(job,publish['publication'])
        stamp[0]+=timedelta(seconds=61)
    assert job not in j.due() and j.claim(job) is None
    with Session(j.engine) as db:
        row=db.get(module.DispatchJob,job)
        assert row.state=='FAILED' and row.failures==2
        assert row.error_code=='BROKER_PUBLISH_FAILED'
        assert db.query(module.DispatchEvent).filter_by(job_id=job).count()==4

def test_live_lease_renewal_and_busy_target_defer(journal):
    j,stamp=journal;job=j.ensure('agent',str(uuid4()));owner=j.claim(job)
    stamp[0]+=timedelta(seconds=5)
    assert j.renew(job,owner)
    stamp[0]+=timedelta(seconds=6)
    assert not j.reserve_publish(job)
    assert j.finish(job,owner,retry=True)
    assert j.claim(job) is None
    stamp[0]+=timedelta(seconds=6)
    assert job in j.due()

def test_delayed_delivery_respects_failure_backoff(journal):
    j,stamp=journal;job=j.ensure('agent',str(uuid4()));owner=j.claim(job)
    assert j.finish(job,owner,failed=True)
    assert j.claim(job) is None
    stamp[0]+=timedelta(seconds=3)
    assert j.claim(job) is not None

def test_repeated_worker_crashes_eventually_stop(journal):
    j,stamp=journal;job=j.ensure('agent',str(uuid4()))
    for _ in range(2):
        assert j.claim(job)
        stamp[0]+=timedelta(seconds=11)
        j.reserve_publish(job)
    assert not j.claim(job)
    assert job not in j.due()
    with Session(j.engine) as db:
        assert db.get(module.DispatchJob,job).error_code=='WORKER_LEASE_EXPIRED'

def test_acknowledged_but_never_claimed_delivery_is_bounded(journal):
    j,stamp=journal;job=j.ensure('agent',str(uuid4()))
    for _ in range(2):
        delivery=j.reserve_publish(job)
        assert delivery
        j.published(job,delivery['publication'])
        stamp[0]+=timedelta(seconds=31)
    assert j.reserve_publish(job) is None
    assert job not in j.due()
