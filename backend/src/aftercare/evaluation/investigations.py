"""Read-only, tester-only view of actual business investigations and saved decisions."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Case
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.intelligence.tables import AgentRun, ModelAttempt


def authorize(user):
    if user.role != 'tester':
        raise BusinessError('EVALUATION_FORBIDDEN', '仅测试员可查看测试记录。', 403)


def usage(attempts):
    known = sum((Decimal(a.estimated_cost_usd) for a in attempts if a.estimated_cost_usd is not None), Decimal(0))
    unknown = [a for a in attempts if a.estimated_cost_usd is None]
    reservations = [a.metadata_json.get('reserved_cost_usd') for a in unknown]
    reserved = sum((Decimal(x) for x in reservations if x is not None), Decimal(0))
    return {'calls': len(attempts), 'known_usd': str(known), 'unpriced_calls': len(unknown),
            'reserved_usd': str(reserved), 'reserve_complete': all(x is not None for x in reservations),
            'estimated_usd': str(known) if not unknown else None,
            'failed_calls': sum(a.status not in {'SUCCEEDED', 'STARTED'} for a in attempts)}


def investigations(engine, user, *, order='', offset=0, limit=30):
    authorize(user)
    stmt = select(AgentRun, Case.order_id).join(Case, Case.id == AgentRun.case_id)
    if order:
        stmt = stmt.where(Case.order_id == order)
    stmt = stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).offset(offset).limit(limit + 1)
    with Session(engine) as db:
        records = db.execute(stmt).all()
        page = records[:limit]
        ids = [run.id for run, _ in page]
        attempts = db.scalars(select(ModelAttempt).where(ModelAttempt.run_id.in_(ids))).all() if ids else []
        snapshots = db.scalars(select(DecisionSnapshot).where(DecisionSnapshot.run_id.in_(ids), DecisionSnapshot.origin == 'AGENT')).all() if ids else []
        items = []
        for run, order_id in page:
            saved = sorted([s for s in snapshots if s.run_id == run.id], key=lambda s:s.version)
            snapshot = saved[-1] if saved else None
            calls = [a for a in attempts if a.run_id == run.id]
            items.append({'id':run.id,'case_id':run.case_id,'order_id':order_id,
                          'status':run.status,'reason':run.reason,'created_at':run.created_at.isoformat(),
                          'mode':run.mode,'strategy':run.strategy,
                          'duration_seconds': max(0, (run.updated_at-run.created_at).total_seconds()) if run.status not in {'QUEUED','RUNNING','WAITING_INPUT'} else None,
                          'snapshot_version':snapshot.version if snapshot else None, **usage(calls)})
        return {'items':items, 'next_offset':offset+limit if len(records)>limit else None}


def investigation(engine, user, run_id):
    authorize(user)
    with Session(engine) as db:
        run = db.get(AgentRun, run_id)
        if run is None:
            raise BusinessError('RUN_NOT_FOUND','没有找到这次测试记录。',404)
        snapshots = db.scalars(select(DecisionSnapshot).where(DecisionSnapshot.run_id==run.id, DecisionSnapshot.origin=='AGENT').order_by(DecisionSnapshot.version.desc())).all()
        attempts = db.scalars(select(ModelAttempt).where(ModelAttempt.run_id==run.id).order_by(ModelAttempt.started_at, ModelAttempt.logical_id, ModelAttempt.ordinal)).all()
        return {'id':run.id,'case_id':run.case_id,'status':run.status,'reason':run.reason,
                'snapshots':[{'id':s.id,'version':s.version,'issue_id':s.issue_id,'sha256':s.sha256,
                              'candidate':s.candidate,'checks':s.checks,'created_at':s.created_at.isoformat(),
                              'input_through_seq':s.input.get('input_through_seq')} for s in snapshots],
                'attempts':[{'step':a.logical_id,'attempt':a.ordinal,'status':a.status,'error':a.error_code,
                             'estimated_usd':a.estimated_cost_usd,
                             'seconds':max(0,(a.finished_at-a.started_at).total_seconds()) if a.finished_at else None} for a in attempts],
                **usage(attempts)}
