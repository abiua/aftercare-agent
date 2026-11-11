"""Persistent broker delivery, retry and worker-lease journal.

Business/graph transactions remain the authority for effects. This journal
explains delivery and fences stale workers; it never grants refund authority.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, exists, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from aftercare.db import Base

NAMESPACE = UUID('03960592-9892-47c7-b1eb-8d338a92d2c7')


def utc(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


def now():
    return datetime.now(UTC)


class DispatchJob(Base):
    __tablename__ = 'dispatch_jobs'
    __table_args__ = (UniqueConstraint('kind', 'target_id', 'generation'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), index=True)
    publications: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    claims: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DispatchEvent(Base):
    __tablename__ = 'dispatch_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(24))
    owner: Mapped[str | None] = mapped_column(String(36))
    code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _ordinary_delivery():
    from aftercare.evaluation.tables import Experiment, ordinary_experiment

    return ~exists(select(Experiment.id).where(
        DispatchJob.kind == 'evaluation',
        Experiment.id == DispatchJob.target_id,
        ~ordinary_experiment(),
    ).correlate(DispatchJob))


class DeliveryJournal:
    def __init__(self, engine, *, clock=now, lease_seconds=60, max_failures=8):
        self.engine, self.clock = engine, clock
        self.lease_seconds, self.max_failures = lease_seconds, max_failures

    def _record(self, db, row, state, code=None):
        row.updated_at = self.clock()
        db.add(DispatchEvent(id=str(uuid4()), job_id=row.id, state=state,
                            owner=row.owner, code=code, created_at=row.updated_at))

    def _expired(self, db, row):
        row.failures += 1
        row.error_code = 'WORKER_LEASE_EXPIRED'
        self._record(db, row, 'LEASE_EXPIRED', row.error_code)
        if row.failures >= self.max_failures:
            row.state, row.owner, row.lease_until = 'FAILED', None, None
            self._record(db, row, 'FAILED', row.error_code)
            return True
        return False

    def ensure(self, kind, target_id, generation=0):
        if kind not in {'agent', 'evaluation'} or generation < 0:
            raise ValueError('INVALID_DISPATCH_TARGET')
        UUID(target_id)
        job_id = str(uuid5(NAMESPACE, f'{kind}:{target_id}:{generation}'))
        stamp = self.clock()
        values = {'id': job_id, 'kind': kind, 'target_id': target_id, 'generation': generation,
                  'state': 'READY', 'publications': 0, 'failures': 0, 'claims': 0, 'next_at': stamp,
                  'owner': None, 'lease_until': None, 'error_code': None,
                  'created_at': stamp, 'updated_at': stamp}
        with self.engine.begin() as conn:
            if self.engine.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            conn.execute(insert(DispatchJob).values(**values).on_conflict_do_nothing())
        return job_id

    def due(self, limit=100):
        stamp = self.clock()
        with Session(self.engine) as db:
            rows = db.scalars(select(DispatchJob).where(
                _ordinary_delivery(),
                DispatchJob.state.in_(['READY', 'PUBLISHING', 'PUBLISHED', 'RUNNING']),
                DispatchJob.next_at <= stamp,
            ).order_by(DispatchJob.next_at, DispatchJob.id).limit(limit))
            return [r.id for r in rows if r.state != 'RUNNING' or
                    r.lease_until is None or utc(r.lease_until) <= stamp]

    def reserve_publish(self, job_id):
        with Session(self.engine) as db, db.begin():
            row = db.scalar(select(DispatchJob).where(
                DispatchJob.id == job_id, _ordinary_delivery(),
            ).with_for_update())
            stamp = self.clock()
            if row is None or row.state in {'DONE', 'FAILED', 'CANCELLED'}:
                return None
            # Delayed/duplicate broker deliveries cannot bypass retry backoff.
            if row.state == 'READY' and utc(row.next_at) > stamp:
                return None
            if row.state == 'RUNNING' and row.lease_until and utc(row.lease_until) > stamp:
                return None
            if utc(row.next_at) > stamp:
                return None
            if row.state in {'PUBLISHING', 'PUBLISHED'}:
                row.failures += 1
                row.error_code = 'WORKER_NOT_CLAIMED'
                self._record(db, row, 'DELIVERY_EXPIRED', row.error_code)
                if row.failures >= self.max_failures:
                    row.state = 'FAILED'
                    self._record(db, row, 'FAILED', row.error_code)
                    return None
            if row.state == 'RUNNING' and self._expired(db, row):
                return None
            row.owner, row.lease_until = None, None
            row.state = 'PUBLISHING'
            row.publications += 1
            row.next_at = stamp + timedelta(seconds=30)
            self._record(db, row, 'PUBLISHING')
            return {'id': row.id, 'kind': row.kind, 'target_id': row.target_id,
                    'generation': row.generation, 'publication': row.publications}

    def published(self, job_id, publication):
        with Session(self.engine) as db, db.begin():
            row = db.get(DispatchJob, job_id, with_for_update=True)
            # A very fast worker may claim before the broker acknowledgement.
            if row and row.publications == publication and row.state == 'PUBLISHING':
                row.state = 'PUBLISHED'
                self._record(db, row, 'PUBLISHED')

    def publish_failed(self, job_id, publication):
        with Session(self.engine) as db, db.begin():
            row = db.get(DispatchJob, job_id, with_for_update=True)
            if row and row.publications == publication and row.state == 'PUBLISHING':
                self._retry(db, row, 'BROKER_PUBLISH_FAILED')

    def _retry(self, db, row, code):
        row.failures += 1
        row.state = 'FAILED' if row.failures >= self.max_failures else 'READY'
        row.error_code = code
        row.owner, row.lease_until = None, None
        row.next_at = self.clock() + timedelta(seconds=min(60, 2 ** row.failures))
        self._record(db, row, row.state, code)

    def claim(self, job_id):
        with Session(self.engine) as db, db.begin():
            row = db.scalar(select(DispatchJob).where(
                DispatchJob.id == job_id, _ordinary_delivery(),
            ).with_for_update())
            stamp = self.clock()
            if row is None or row.state in {'DONE', 'FAILED', 'CANCELLED'}:
                return None
            if row.state == 'READY' and utc(row.next_at) > stamp:
                return None
            if row.state == 'RUNNING' and row.lease_until and utc(row.lease_until) > stamp:
                return None
            if row.state == 'RUNNING' and self._expired(db, row):
                return None
            row.state, row.owner = 'RUNNING', str(uuid4())
            row.claims += 1
            row.lease_until = stamp + timedelta(seconds=self.lease_seconds)
            row.next_at = row.lease_until
            self._record(db, row, 'RUNNING')
            return row.owner

    def renew(self, job_id, owner):
        with Session(self.engine) as db, db.begin():
            row = db.get(DispatchJob, job_id, with_for_update=True)
            if not row or row.state != 'RUNNING' or row.owner != owner:
                return False
            # An expired lease cannot be revived by a late heartbeat.
            if not row.lease_until or utc(row.lease_until) <= self.clock():
                return False
            row.lease_until = self.clock() + timedelta(seconds=self.lease_seconds)
            row.next_at = row.lease_until
            row.updated_at = self.clock()
            return True

    def owned(self, job_id, owner, *, session=None):
        if session is not None:
            # Lock in the caller's short transaction, before any target rows.
            # Text qualification prevents an evaluation schema from shadowing
            # the public delivery journal. The lock lasts until caller commit.
            if self.engine.dialect.name == 'postgresql':
                return bool(session.scalar(text(
                    "SELECT 1 FROM public.dispatch_jobs WHERE id=:id AND owner=:owner "
                    "AND state='RUNNING' AND lease_until>:stamp FOR SHARE"
                ), {'id': job_id, 'owner': owner, 'stamp': self.clock()}))
            row = session.get(DispatchJob, job_id, with_for_update={'read': True})
            return bool(row and row.state == 'RUNNING' and row.owner == owner and
                        row.lease_until and utc(row.lease_until) > self.clock())
        with Session(self.engine) as db:
            row = db.get(DispatchJob, job_id)
            return bool(row and row.state == 'RUNNING' and row.owner == owner and
                        row.lease_until and utc(row.lease_until) > self.clock())

    def finish(self, job_id, owner, *, retry=False, failed=False):
        with Session(self.engine) as db, db.begin():
            row = db.get(DispatchJob, job_id, with_for_update=True)
            if not row or row.state != 'RUNNING' or row.owner != owner:
                return False
            if not row.lease_until or utc(row.lease_until) <= self.clock():
                return False
            if failed:
                self._retry(db, row, 'WORKER_FAILED')
            elif retry:
                row.state, row.owner, row.lease_until = 'READY', None, None
                row.next_at = self.clock() + timedelta(seconds=5)
                self._record(db, row, 'DEFERRED', 'TARGET_STILL_ACTIVE')
            else:
                row.state, row.lease_until = 'DONE', None
                self._record(db, row, 'DONE')
            return True
