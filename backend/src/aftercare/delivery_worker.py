"""Lease heartbeat wrapper shared by graph and evaluation queue tasks."""
from threading import Event, Thread


def run_claimed(journal, job_id, operation, is_active):
    """Run once under a renewable lease; operation receives a fencing check.

    A database outage closes admission immediately. The original exception is
    propagated so the broker/monitor can see failure, while durable retries are
    scheduled only by the journal rather than implicit Celery retries.
    """
    owner = journal.claim(job_id)
    if owner is None:
        return False
    stop, lost = Event(), Event()

    def admitted(_session=None):
        if lost.is_set():
            return False
        try:
            return journal.owned(job_id, owner, session=_session)
        except Exception:  # noqa: BLE001 - admission fails closed on any journal failure
            lost.set()
            return False

    def heartbeat():
        while not stop.wait(max(0.01, journal.lease_seconds / 3)):
            try:
                if not journal.renew(job_id, owner):
                    lost.set()
                    return
            except Exception:  # noqa: BLE001 - a failed heartbeat must fence this worker
                lost.set()
                return

    thread = Thread(target=heartbeat, name='aftercare-delivery-lease', daemon=True)
    thread.start()
    try:
        if not admitted():
            return False
        operation(admitted)
        if admitted():
            journal.finish(job_id, owner, retry=is_active())
        return True
    except Exception:
        if admitted():
            journal.finish(job_id, owner, failed=True)
        raise
    finally:
        stop.set()
        thread.join(timeout=5)
