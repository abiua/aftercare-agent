"""Controlled fault on a NEW synthetic order; original messages survive worker replay."""
import json
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tau_p5_cases import p5_cases
from tau_p5_runtime import API_PORT, RUNTIME, configure
from verify_tau_p5_live import analyze, stage

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'reports/p6-handoff-http-20261112.json'


def main():
    configure()
    from aftercare.business.models import Execution
    from aftercare.business.seed import initialize
    from aftercare.config import load_settings
    from aftercare.conversations.decision_delivery import pending, project
    from aftercare.conversations.investigation_delivery import process
    from aftercare.conversations.tables import InvestigationRequest
    from aftercare.db import guarded_engine
    from aftercare.intelligence.tables import AgentRun, RunEvent
    from aftercare.runtime import open_runtime
    assert not OUTPUT.exists()
    fixture=p5_cases()[0]
    old=fixture['order']['id'];new='O9971-P6'
    fixture={**fixture,'order':{**fixture['order'],'id':new},'customer_message':fixture['customer_message'].replace(old,new)}
    engine=guarded_engine(load_settings('test'))
    initialize(engine,{'customers':[],'products':[],'orders':[fixture['order']],'shipments':[],'policies':[]})
    engine.dispose()
    report={'status':'STAGING','request_ids':{},'mutations':{},'fault':'CONTROLLED_TIMEOUT_NO_PROVIDER_CALL'}
    credentials=json.loads((RUNTIME/'.runtime/users-test.json').read_text())
    with ExitStack() as stack:
        clients={row['role']:stack.enter_context(httpx.Client(base_url=f'http://127.0.0.1:{API_PORT}/api/',headers={'Authorization':f"Bearer {row['token']}"},timeout=20,trust_env=False)) for row in credentials}
        stage(clients,report,OUTPUT,fixture)
        analyze(report,OUTPUT)
        with open_runtime('test',dispatch=lambda _:None) as (_,engine,business,agent):
            process(business,agent,report['paid_turn_id'])
            with Session(engine) as db,db.begin():
                run_id=db.get_one(InvestigationRequest,report['paid_turn_id']).run_id
                run=db.get_one(AgentRun,run_id)
                assert run.status=='QUEUED'
                run.status,run.reason='FAILED','MODEL_TIMEOUT'
                run.event_seq+=1
                db.add(RunEvent(run_id=run_id,seq=run.event_seq,kind='evaluation_fault',payload={'scenario':'timeout','provider_calls':0},created_at=business.clock()))
            path=f"conversations/{report['conversation_id']}"
            before=clients['customer'].get(path).json()
            assert run_id in pending(engine)
            project(business,run_id)
            # Recreate worker/service context and deliver again, as after a process restart.
        with open_runtime('test',dispatch=lambda _:None) as (_,engine,business,_):
            project(business,run_id)
            assert run_id not in pending(engine)
            with Session(engine) as db:
                assert not list(db.scalars(select(Execution.id).where(Execution.case_id==report['case_id'])))
        after=clients['customer'].get(path).json()
        assert after['conversation']['status']=='HUMAN_HANDOFF'
        assert after['messages'][:-1]==before['messages']
        assert '超时' in after['messages'][-1]['text']
        other=clients['customer'].post('conversations',json={},headers={'Idempotency-Key':str(uuid4())}).json()
        unfinished=clients['customer'].get('conversations/unfinished')
        assert unfinished.status_code==200
        assert report['conversation_id'] in [c['id'] for c in unfinished.json()['items']]
        report.update(status='PASSED',run_id=run_id,original_messages=before['messages'],after_messages=after['messages'],
                      handoff_message=after['messages'][-1]['text'],new_conversation_id=other['id'],
                      duplicate_notices=0,case_executions=0,unfinished_found=True,
                      restart_scope='WORKER_CONTEXT_REOPEN; HTTP process restart verified separately')
        OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:report[k] for k in ('status','conversation_id','case_id','handoff_message')}))


if __name__=='__main__':main()
