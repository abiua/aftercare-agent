import json,time
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
users=json.loads((ROOT/'.runtime/users-test.json').read_text());token=next(u['token'] for u in users if u['role']=='tester')
with httpx.Client(base_url='http://127.0.0.1:8022',headers={'Authorization':'Bearer '+token},timeout=15) as c:
    cases=c.get('/api/evaluation/cases?dataset=v0.2&split=dev').json()['items']
    ids=[next(x['id'] for x in cases if x['intent']==intent) for intent in ['damaged','wrong_item','refund_status']]
    body={'name':'三意图离线集成检查','dataset_id':'v0.2','split':'dev','mode':'multi','strategy':'routed','repetitions':1,'concurrency':1,'max_calls':36,'budget_usd':0,'source':'fixture','case_ids':ids}
    r=c.post('/api/evaluation/experiments',json=body,headers={'Idempotency-Key':str(uuid4())})
    assert r.status_code==201,(r.status_code,r.text)
    exp=r.json(); print('created',exp['id'],flush=True)
    (ROOT/'reports/evaluation-smoke-id.txt').write_text(exp['id'])
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        exp=c.get('/api/evaluation/experiments/'+exp['id']).json()
        if exp['status'] not in ('QUEUED','RUNNING'):break
        time.sleep(1)
    (ROOT/'reports/evaluation-smoke.json').write_text(json.dumps(exp,ensure_ascii=False,indent=2))
    print(json.dumps({'status':exp['status'],'counts':exp.get('counts'),'results':[{k:r.get(k) for k in ['case_id','status','failure_reason']} for r in exp.get('results',[])],'gate':exp.get('gate')},ensure_ascii=False))
    assert exp['status'] not in ('QUEUED','RUNNING'), 'WORKER_DID_NOT_FINISH'
