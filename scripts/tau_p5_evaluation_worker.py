"""Local P5 offline evaluation worker. Keeps model calls disabled; uses existing job leases."""
import fcntl
import time

from tau_p5_runtime import RUNTIME, configure


def main():
    configure()
    from aftercare.business.storage import S3ObjectStore
    from aftercare.evaluation.runner import EvaluationRunner
    from aftercare.evaluation.service import EvaluationService
    from aftercare.evaluation.tables import Experiment
    from aftercare.runtime import open_runtime
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with (RUNTIME / 'evaluation-worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open_runtime('test', dispatch=lambda _: None) as (settings, engine, _business, agent):
            assert agent.model is None and not settings.model_enabled
            service = EvaluationService(engine, settings, model=None)
            assert settings.object_endpoint == 'http://127.0.0.1:18340'
            assert settings.object_bucket == 'tau-chat-p5-private'
            runner = EvaluationRunner(engine, settings, agent.checkpointer, model=None,
                                      storage=S3ObjectStore(settings))
            print('P5 offline evaluation worker ready', flush=True)
            while True:
                with Session(engine) as db:
                    jobs = db.scalars(select(Experiment).where(Experiment.status == 'QUEUED').order_by(Experiment.created_at)).all()
                    ids = [job.id for job in jobs if job.config.get('source') in {'fixture', 'fault', 'load'} and job.config.get('execution_driver', 'queued') == 'queued']
                for ident in ids:
                    try:
                        service.process(ident, runner)
                        print(f'processed {ident}', flush=True)
                    except Exception as exc:
                        print(f'worker error {ident}: {type(exc).__name__}', flush=True)
                        raise
                time.sleep(2)


if __name__ == '__main__':
    main()
