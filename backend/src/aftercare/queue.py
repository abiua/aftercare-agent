"""At-least-once queue backed by the transactionally committed run ledger.

QUEUED/RUNNING rows are the outbox. Broker delivery is a wake-up hint; the
periodic recovery task republishes those rows after a lost publish or crash.
The AgentService PostgreSQL lock and attempt ledger own duplicate suppression.
"""
import os

from celery import Celery
from kombu.exceptions import OperationalError

from aftercare.config import load_settings


def make_queue(profile):
    settings = load_settings(profile)
    app = Celery(f"aftercare-{profile}", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_default_queue=f"aftercare-{profile}",
        task_serializer="json", accept_content=["json"],
        result_serializer="json", task_ignore_result=True, result_expires=300,
        task_acks_late=True, task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
        broker_connection_timeout=3, task_publish_retry=False,
        broker_transport_options={"visibility_timeout": 180, "socket_timeout": 3,
                                  "socket_connect_timeout": 3},
        beat_schedule={"recover-durable-runs": {
            "task": "aftercare.recover", "schedule": 15.0,
        }},
    )

    @app.task(name="aftercare.process", shared=False)
    def process(run_id, job_id=None):
        from aftercare.queue_delivery import process_target
        process_target(profile, 'agent', run_id, job_id)

    @app.task(name="aftercare.evaluate", shared=False)
    def evaluate(experiment_id, job_id=None):
        from aftercare.queue_delivery import process_target
        process_target(profile, 'evaluation', experiment_id, job_id)

    @app.task(name="aftercare.recover", shared=False)
    def recover():
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from aftercare.db import guarded_engine
        from aftercare.intelligence.tables import AgentRun
        engine = guarded_engine(settings)
        try:
            from aftercare.dispatch import DeliveryJournal
            from aftercare.queue_delivery import publish, reconcile_failed
            journal = DeliveryJournal(engine)
            with Session(engine) as session:
                rows = list(session.execute(select(AgentRun.id, AgentRun.cycle).where(
                    AgentRun.status.in_(["QUEUED", "RUNNING"])
                ).order_by(AgentRun.updated_at).limit(100)))
            for run_id, cycle in rows:
                journal.ensure('agent', run_id, cycle)
            from aftercare.evaluation.service import EvaluationService
            if profile == 'test':
                for experiment_id in EvaluationService(engine, settings).pending():
                    journal.ensure('evaluation', experiment_id)
            for job_id in journal.due():
                publish(app, journal, job_id)
            reconcile_failed(engine, settings)
        finally:
            engine.dispose()

    return app


def publisher(profile, task_name='aftercare.process'):
    app = make_queue(profile)

    def dispatch(run_id):
        from sqlalchemy.exc import SQLAlchemyError

        from aftercare.db import guarded_engine
        from aftercare.dispatch import DeliveryJournal
        from aftercare.queue_delivery import publish, target_generation
        engine = None
        try:
            engine = guarded_engine(load_settings(profile))
            kind = 'evaluation' if task_name == 'aftercare.evaluate' else 'agent'
            generation = target_generation(engine, kind, run_id)
            if generation is not None:
                journal = DeliveryJournal(engine)
                publish(app, journal, journal.ensure(kind, run_id, generation))
        except (OperationalError, OSError, SQLAlchemyError):
            # The run row already committed; beat will publish after recovery.
            return
        finally:
            if engine is not None:
                engine.dispose()

    return dispatch


def worker_app():
    return make_queue(os.environ.get("AFTERCARE_PROFILE", "app"))
