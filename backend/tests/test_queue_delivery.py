from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftercare.dispatch import DeliveryJournal, DispatchEvent, DispatchJob
from aftercare.evaluation.tables import Experiment
from aftercare.queue_delivery import publish


def test_publication_failure_is_recorded_for_recovery():
    engine = create_engine('sqlite://')
    Experiment.__table__.create(engine)
    DispatchJob.__table__.create(engine)
    DispatchEvent.__table__.create(engine)
    journal = DeliveryJournal(engine)
    job = journal.ensure('agent', str(uuid4()))

    class Broker:
        def send_task(self, *args, **kwargs):
            raise OSError('unavailable')

    publish(Broker(), journal, job)
    with Session(engine) as db:
        row = db.get(DispatchJob, job)
        assert row.state == 'READY'
        assert row.failures == 1
        assert row.error_code == 'BROKER_PUBLISH_FAILED'
    engine.dispose()


def test_fast_worker_claim_survives_publication_acknowledgement():
    engine = create_engine('sqlite://')
    Experiment.__table__.create(engine)
    DispatchJob.__table__.create(engine)
    DispatchEvent.__table__.create(engine)
    journal = DeliveryJournal(engine)
    target = str(uuid4())
    job = journal.ensure('agent', target)

    class Broker:
        def send_task(self, task, args, kwargs):
            assert task == 'aftercare.process'
            assert args == [target] and kwargs == {'job_id': job}
            assert journal.claim(job)

    publish(Broker(), journal, job)
    with Session(engine) as db:
        assert db.get(DispatchJob, job).state == 'RUNNING'
    engine.dispose()
