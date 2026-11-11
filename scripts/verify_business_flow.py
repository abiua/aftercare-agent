"""Repeatable real HTTP/S3/PG flow on the isolated test profile."""
import io
import json
import time
from uuid import uuid4
from PIL import Image
from fastapi.testclient import TestClient
from aftercare.api import create_app
from aftercare.config import ROOT, load_settings


def main():
    if load_settings("test").model_enabled:
        raise RuntimeError("This offline integration script requires model_enabled=false")
    users=json.loads((ROOT/'.runtime/users-test.json').read_text())
    headers={u['role']:{'Authorization':'Bearer '+u['token']} for u in users}
    report={'source':'real PostgreSQL/S3/Celery; simulated payment; rule-only Agent','steps':[]}
    with TestClient(create_app('test')) as c:
        def get(path,role='support'):
            r=c.get('/api'+path,headers=headers[role]);assert r.status_code==200,(r.status_code,r.text)
            return r.json()
        used={x['order_id'] for x in get('/cases?limit=300')['items']}
        order=next(x for x in get('/orders?limit=300')['items'] if x['id'] not in used and x['status']=='DELIVERED' and x['payment_status']=='CONFIRMED' and x['refunded_cents']==0)
        def post(path,body,role='support',key=None):
            r=c.post('/api'+path,headers={**headers[role],'Idempotency-Key':key or str(uuid4())},json=body)
            assert r.status_code in (200,201,202),(path,r.status_code,r.text)
            return r.json()
        case=post('/cases',{'order_id':order['id'],'intent':'damaged','description':'合成验收：商品到货破损，申请退款。','expected_version':order['version']})
        base='/cases/'+case['id'];report['case_id']=case['id']
        def version():return get(base)['case']['version']
        run=post(base+'/runs',{'expected_version':version(),'mode':'multi','strategy':'routed'})
        def wait():
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                result=get(base+'/runs/'+run['id'])
                if result['status'] not in ('QUEUED','RUNNING'):return result
                time.sleep(.25)
            raise AssertionError('WORKER_TIMEOUT')
        assert wait()['status']=='WAITING_INPUT';report['steps'].append('missing_material_pause')
        for kind,color in [('ITEM','red'),('PARCEL','blue')]:
            blob=io.BytesIO();Image.new('RGB',(32,32),color).save(blob,format='PNG')
            r=c.post('/api'+base+'/materials',headers={**headers['support'],'Idempotency-Key':str(uuid4())},data={'kind':kind,'expected_version':str(version())},files={'file':('synthetic.png',blob.getvalue(),'image/png')})
            assert r.status_code==201,r.text
        post(base+'/material-reviews',{'expected_version':version(),'decision':'APPROVED','notes':'合成测试材料，人工角色确认用于验证流程。'})
        post(base+'/runs/'+run['id']+'/resume',{'expected_version':version()})
        resumed=wait(); report['agent_terminal']=resumed['status'];report['same_run_id']=resumed['id']==run['id'];report['same_thread_id']=resumed['thread_id']==run['thread_id']
        assert resumed['status'] not in ('WAITING_INPUT','FAILED','QUEUED','RUNNING'),resumed
        report['steps'].append('same_thread_resume_after_review')
        lines=[{'line_id':x['line_id'],'amount_cents':x['paid_cents']} for x in order['items']]
        proposal=post(base+'/proposals',{'expected_version':version(),'action':'REFUND','lines':lines,'notes':'全额模拟退款方案，仅用于隔离测试。','policy_ids':['POL14']})
        blocked=c.post('/api'+base+'/execute',headers={**headers['support'],'Idempotency-Key':str(uuid4())},json={'expected_version':version(),'proposal_id':proposal['id']})
        assert blocked.status_code==409,blocked.text
        report['steps'].append('unapproved_execution_denied')
        post(base+'/approvals',{'expected_version':version(),'proposal_id':proposal['id'],'decision':'APPROVED','notes':'独立审批角色批准本次隔离测试退款。'},role='approver')
        body={'expected_version':version(),'proposal_id':proposal['id']};key=str(uuid4())
        execution=post(base+'/execute',body,key=key)
        duplicate=post(base+'/execute',body,key=key)
        assert execution['id']==duplicate['id']
        final=get(base);report['execution']=execution;report['ledger_count']=len(final['ledger'])
        assert len(final['ledger'])==1
        assert final['order']['refunded_cents']==order['paid_cents'],final['order']
        report['steps'].append('approved_once_only_refund')
        report['status']='PASSED'
        (ROOT/'reports/business-full-flow.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'status':report['status'],'steps':report['steps'],'agent_terminal':report['agent_terminal']},ensure_ascii=False))
if __name__=='__main__':main()
