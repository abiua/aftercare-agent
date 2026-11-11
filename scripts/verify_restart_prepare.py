"""Prepare a missing-material pause for real process crash acceptance."""
import json,time
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
users=json.loads((ROOT/'.runtime/users-test.json').read_text());token=next(u['token'] for u in users if u['role']=='support')
with httpx.Client(base_url='http://127.0.0.1:8022',headers={'Authorization':'Bearer '+token},timeout=15) as c:
    def read(path):
        r=c.get('/api'+path);r.raise_for_status();return r.json()
    used={x['order_id'] for x in read('/cases?limit=300')['items']}
    order=next(x for x in read('/orders?limit=300')['items'] if x['id'] not in used and x['status']=='DELIVERED' and x['payment_status']=='CONFIRMED' and not x['refunded_cents'])
    r=c.post('/api/cases',headers={'Idempotency-Key':str(uuid4())},json={'order_id':order['id'],'intent':'damaged','description':'进程中断恢复验收：已签收商品破损，稍后补交照片。','expected_version':order['version']});r.raise_for_status();case=r.json()
    base='/cases/'+case['id']+'/runs'
    r=c.post('/api'+base,headers={'Idempotency-Key':str(uuid4())},json={'expected_version':case['version'],'mode':'multi','strategy':'routed'});r.raise_for_status();run=r.json()
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        run=read(base+'/'+run['id'])
        if run['status']=='WAITING_INPUT':break
        time.sleep(.25)
    assert run['status']=='WAITING_INPUT',run
    events=read(base+'/'+run['id']+'/events')
    assert not any(e['kind']=='attempt' for e in events)
    report={'case_id':case['id'],'run_id':run['id'],'thread_id':run['thread_id'],'before_status':run['status'],'before_events':len(events),'provider_calls_before':0}
    (ROOT/'reports/process-restart.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
