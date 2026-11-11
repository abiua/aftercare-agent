"""Persisted wake-ups and fenced execution for Celery deliveries."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.delivery_worker import run_claimed
from aftercare.dispatch import DeliveryJournal, DispatchJob, now
from aftercare.intelligence.tables import AgentRun

TASKS = {'agent': 'aftercare.process', 'evaluation': 'aftercare.evaluate'}


def target_generation(engine, kind, target_id):
    with Session(engine) as db:
        if kind == 'agent':
            row = db.get(AgentRun, target_id)
            return row.cycle if row and row.status in {'QUEUED', 'RUNNING'} else None
        from aftercare.evaluation.tables import EvaluationResult, Experiment, ordinary_experiment
        row = db.scalar(select(Experiment).where(Experiment.id == target_id, ordinary_experiment()))
        if row and row.config.get('execution_driver', 'queued') != 'queued':
            return None
        if row and row.status in {'QUEUED', 'RUNNING'}:
            return 0
        if row and row.status == 'CANCELLED' and db.scalar(select(EvaluationResult.id).where(
            EvaluationResult.experiment_id == target_id,
            EvaluationResult.status == 'RUNNING',
        ).limit(1)):
            return 0
        return None


def publish(app, journal, job_id):
    from kombu.exceptions import OperationalError
    delivery = journal.reserve_publish(job_id)
    if delivery is None:
        return
    try:
        app.send_task(TASKS[delivery['kind']], args=[delivery['target_id']],
                      kwargs={'job_id': job_id})
    except (OperationalError, OSError):
        journal.publish_failed(job_id, delivery['publication'])
    else:
        journal.published(job_id, delivery['publication'])


def process_target(profile, kind, target_id, job_id=None):
    from aftercare.runtime import open_runtime
    with open_runtime(profile) as (settings, engine, _, agent):
        journal = DeliveryJournal(engine)
        if job_id:
            with Session(engine) as db:
                matches = db.scalar(select(DispatchJob.id).where(
                    DispatchJob.id == job_id,
                    DispatchJob.kind == kind,
                    DispatchJob.target_id == target_id,
                ))
            if matches is None:
                return
        generation = target_generation(engine, kind, target_id)
        if generation is None:
            # A stale broker hint is not permission to reopen a terminal target.
            if job_id:
                run_claimed(journal, job_id, lambda _: None, lambda: False)
            return
        current_job = journal.ensure(kind, target_id, generation)
        if job_id and job_id != current_job:
            run_claimed(journal, job_id, lambda _: None, lambda: False)
            return

        def operation(admitted):
            if kind == 'agent':
                agent.queue_admission = admitted
                agent.process(target_id, expected_cycle=generation)
            else:
                from aftercare.evaluation.runner import EvaluationRunner
                from aftercare.evaluation.service import EvaluationService
                runner = EvaluationRunner(engine, settings, agent.checkpointer, model=agent.model)
                EvaluationService(engine, settings, model=agent.model,
                                  attempt_admission=admitted).process(target_id, runner)

        def active():
            return target_generation(engine, kind, target_id) == generation

        run_claimed(journal, current_job, operation, active)


def reconcile_failed(engine, settings):
    """Make exhausted delivery visible on its still-active business target."""
    from aftercare.evaluation.runner import EvaluationRunner
    from aftercare.evaluation.service import EvaluationService
    from aftercare.evaluation.tables import EvaluationResult, Experiment, ordinary_experiment
    from aftercare.intelligence.service import _mark
    audit_reader = EvaluationRunner(engine, settings, None)
    with Session(engine) as db, db.begin():
        failed = db.scalars(select(DispatchJob).where(DispatchJob.state == 'FAILED').with_for_update())
        for job in failed:
            if job.kind == 'agent':
                row = db.get(AgentRun, job.target_id, with_for_update=True)
                if row and row.cycle == job.generation and row.status in {'QUEUED', 'RUNNING'}:
                    _mark(db, row, 'FAILED', 'QUEUE_DELIVERY_EXHAUSTED')
            else:
                row = db.scalar(select(Experiment).where(
                    Experiment.id == job.target_id, ordinary_experiment(),
                ).with_for_update())
                if row and row.config.get('execution_driver', 'queued') != 'queued':
                    continue
                if row and row.status == 'CANCELLED' and not db.scalar(select(EvaluationResult.id).where(
                    EvaluationResult.experiment_id == row.id,
                    EvaluationResult.status.in_(['QUEUED', 'RUNNING']),
                ).limit(1)):
                    continue
                if row and row.status in {'QUEUED', 'RUNNING', 'CANCELLED'}:
                    if row.status != 'CANCELLED':
                        row.status = 'FAILED'
                    row.updated_at = now()
                    row.version += 1
                    for result in db.scalars(select(EvaluationResult).where(
                        EvaluationResult.experiment_id == row.id,
                        EvaluationResult.status.in_(['QUEUED', 'RUNNING']),
                    )):
                        if result.status == 'RUNNING':
                            actual, trace = EvaluationService._recover_audit(audit_reader, result.id)
                            if actual is not None:
                                result.actual, result.trace = actual, trace
                            result.status = 'ERROR'
                            result.finished_at = now()
                        else:
                            result.status = 'NOT_RUN'
                        result.failure_reason = 'QUEUE_DELIVERY_EXHAUSTED'
                        result.version += 1
                    # Preserve unknown costs/outcomes; queue failure never
                    # invents success or retries a payment/model call.
