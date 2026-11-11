import json, os, time
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1]
accounts=json.loads((root/'.runtime/users-test.json').read_text())
token=next(r['token'] for r in accounts if r['role']=='tester')
state=root/'.runtime/progress-http-state.json'
with httpx.Client(base_url='http://127.0.0.1:8022',headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30) as c:
 cap=c.get('/api/evaluation/capabilities');cap.raise_for_status();assert cap.json()['eval_progress_events']
 if state.exists():
  saved=json.loads(state.read_text())
 else:
  saved={'key':'root-progress-smoke-20261110','body':{'name':'评测进度共享环境验收','dataset_id':'v0.2','split':'dev','mode':'single','strategy':'routed','repetitions':1,'concurrency':1,'max_calls':1,'budget_usd':0,'source':'fixture','case_ids':['AC02-C001','AC02-C041','AC02-C081']}}
  fd=os.open(state,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  with os.fdopen(fd,'w') as f:json.dump(saved,f)
 if 'id' not in saved:
  r=c.post('/api/evaluation/experiments',json=saved['body'],headers={'Idempotency-Key':saved['key']});r.raise_for_status();saved['id']=r.json()['id'];state.write_text(json.dumps(saved))
 path='/api/evaluation/experiments/'+saved['id']
 deadline=time.monotonic()+120
 while True:
  r=c.get(path+'/progress');r.raise_for_status();p=r.json()
  if p['terminal']:break
  if time.monotonic()>deadline:raise RuntimeError('EXPERIMENT_STILL_ACTIVE; reuse saved id')
  time.sleep(1)
 events=p['events'];assert events and p['latest_sequence']==len(events)
 with c.stream('GET',path+'/progress/stream',headers={'Last-Event-ID':'2'}) as r:
  r.raise_for_status();data=[json.loads(line[6:]) for line in r.iter_lines() if line.startswith('data: ')]
 assert data[:-1]==events[2:]
 detail=c.get(path).json()
 legacy=c.get('/api/evaluation/experiments/3139c3ce-9f15-4f9e-895a-1c481bdc5f40/progress').json()
 assert legacy['history']=='HISTORICAL_SNAPSHOT' and legacy['events']==[]
 report={'status':'PASS_SHARED_HTTP_CELERY_PROGRESS','experiment_id':saved['id'],'result_statuses':[r['machine_status'] if 'machine_status'in r else r['status'] for r in detail['results']],'experiment_status':p['status'],'events':events,'resume_after':2,'resumed_events':len(data)-1,'legacy_history':legacy['history'],'source':'fixture','budget_usd':0,'scope':'Shared test API, Celery worker and PostgreSQL; real HTTP stream resume after completion. Active stream revocation covered separately in isolated HTTP test.'}
 assert p['status']=='COMPLETED'
 (root/'reports/evaluation-progress-shared-http.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in report.items() if k!='events'},ensure_ascii=False))
