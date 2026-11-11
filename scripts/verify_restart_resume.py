import io,json,time,subprocess
from pathlib import Path
from uuid import uuid4
import httpx
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
p=ROOT/'reports/process-restart.json';report=json.loads(p.read_text());cid=report['case_id'];rid=report['run_id'];base=f'/api/cases/{cid}'
users=json.loads((ROOT/'.runtime/users-test.json').read_text());token=next(u['token'] for u in users if u['role']=='support')
with httpx.Client(base_url='http://127.0.0.1:8022',headers={'Authorization':'Bearer '+token},timeout=15) as c:
    def read(path):
        r=c.get(path);r.raise_for_status();return r.json()
    run=read(base+'/runs/'+rid);events=read(base+'/runs/'+rid+'/events')
    assert run['status']=='WAITING_INPUT' and run['thread_id']==report['thread_id']
    assert len(events)==report['before_events']
    report['restart_preserved_state_without_new_events']=True
    image=io.BytesIO();Image.new('RGB',(24,24),'green').save(image,format='PNG')
    version=read(base)['case']['version']
    r=c.post(base+'/materials',headers={'Idempotency-Key':str(uuid4())},data={'expected_version':str(version),'kind':'ITEM'},files={'file':('synthetic.png',image.getvalue(),'image/png')});r.raise_for_status()
    version=read(base)['case']['version'];key=str(uuid4());body={'expected_version':version}
    r=c.post(base+'/runs/'+rid+'/resume',headers={'Idempotency-Key':key},json=body);r.raise_for_status()
    duplicate=c.post(base+'/runs/'+rid+'/resume',headers={'Idempotency-Key':key},json=body);duplicate.raise_for_status();assert duplicate.json()['id']==rid
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        run=read(base+'/runs/'+rid)
        if run['status'] not in ('QUEUED','RUNNING'):break
        time.sleep(.5)
    assert run['status']=='WAITING_INPUT',run
    assert run['thread_id']==report['thread_id'] and run['cycle']==1
    assert run['output']['missing_materials']==['PARCEL'],run
    events=read(base+'/runs/'+rid+'/events')
    attempts=[e for e in events if e['kind']=='attempt' and e['payload']['status']!='STARTED']
    report.update(status='PASSED',after_status=run['status'],after_cycle=run['cycle'],missing_materials=run['output']['missing_materials'],same_thread=True,duplicate_resume_same_run=True,completed_attempts_total=len(attempts),attempts_after=[e['payload'] for e in attempts],new_pids={'api':93252,'worker':None})
    p.write_text(json.dumps(report,ensure_ascii=False,indent=2));print({k:report[k] for k in ['status','same_thread','after_cycle','missing_materials','completed_attempts_total']})
