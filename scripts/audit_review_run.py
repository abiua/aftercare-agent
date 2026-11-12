"""Reconcile immutable E1 manifests, sample outputs and provider receipts."""
from decimal import Decimal
from hashlib import sha256
import json
from run_review_benchmark import ROOT,DATA,RUN
from tau_official_summary import call_receipts


def audit(directory=RUN):
    planned={};hashes=[]
    for split in ('development','holdout'):
        path=directory/(split+'-manifest.json')
        if not path.exists():continue
        manifest=json.loads(path.read_text())
        assert sha256(DATA.read_bytes()).hexdigest()==manifest['dataset_sha256'],'Dataset changed'
        for name,expected in manifest['code_sha256'].items():
            assert sha256((ROOT/name).read_bytes()).hexdigest()==expected,f'Frozen code changed: {name}'
        hashes.append({'split':split,'manifest_sha256':sha256(path.read_bytes()).hexdigest()})
        for ident,mode in manifest['schedule']:
            key=ident+'-'+mode
            assert key not in planned,'Duplicate planned sample'
            planned[key]=(ident,mode,split)
    reservations,settlements=call_receipts(directory/'calls.jsonl')
    assert not(set(settlements)-set(reservations)),'Unreserved settlement'
    by_row={}
    for call_id,event in reservations.items():
        key=event['row'];assert key in planned,'Unscheduled paid call'
        assert key not in by_row,'Repeated paid call for same sample'
        by_row[key]=call_id
        mode=planned[key][1]
        assert event['actor']==('reviewer' if mode=='M1' else 'self_check')
        assert event['role']=='agent' and event['model']=='gpt-6.1-sol'
    counts={'complete':0,'started':0,'missing':0};fallbacks={}
    for key,(ident,mode,split) in planned.items():
        path=directory/(key+'.json')
        if not path.exists():counts['missing']+=1;continue
        row=json.loads(path.read_text());assert row['id']==ident and row['mode']==mode
        counts[row['status']]+=1
        if row['status']!='complete':continue
        assert row['split']==split
        reason=row['result'].get('fallback_reason')
        if reason:fallbacks[reason]=fallbacks.get(reason,0)+1
        call_id=by_row.get(key)
        if call_id is None:
            assert reason=='BudgetExhausted','Completed sample missing provider reservation'
            continue
        if call_id in settlements:
            recorded=row['metrics']['agent_cost_usd']
            assert recorded is not None
            assert abs(Decimal(str(recorded))-Decimal(settlements[call_id]['actual_usd']))<Decimal('0.0000000001')
        else:
            assert row['metrics']['agent_cost_usd'] is None,'Unknown cost represented as known'
    known=sum((Decimal(x['actual_usd']) for x in settlements.values()),Decimal(0))
    held=sum((Decimal(e['reserve_usd']) for i,e in reservations.items() if i not in settlements),Decimal(0))
    return {'manifests':hashes,'planned':len(planned),'counts':counts,'calls':len(reservations),
        'settled':len(settlements),'known_usd':str(known),'held_usd':str(held),'fallbacks':fallbacks,
        'complete':counts['complete']==len(planned),'scope':'receipt integrity, not label correctness or model superiority'}


if __name__=='__main__':
    result=audit()
    target=ROOT/'evidence/architecture_refactor/e1_receipt_audit.json'
    target.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
