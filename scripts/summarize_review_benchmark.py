"""E1 metrics preserve missing rows and cluster paired uncertainty by source task."""
from collections import defaultdict
from math import comb,sqrt
import json
from pathlib import Path
import random
from run_review_benchmark import ROOT,DATA,RUN
from tau_official_summary import call_receipts

KINDS=('correct_write','wrong_write','correct_abandon','wrong_abandon')


def wilson(success,n):
    if not n:return None
    z=1.95996398454;p=success/n;d=1+z*z/n
    mid=(p+z*z/(2*n))/d
    half=z*sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [max(0,mid-half),min(1,mid+half)]


def paired(rows,kind):
    by_task=defaultdict(list);a_only=b_only=0
    for sample,a,b in rows:
        if sample['kind']!=kind or a is None or b is None:continue
        av=int(a['correct']);bv=int(b['correct'])
        by_task[sample['task_id']].append(av-bv)
        a_only+=av>bv;b_only+=bv>av
    if not by_task:return {'paired_samples':0,'tasks':0,'difference':None,'ci95':None,'mcnemar_p':None}
    values=list(by_task.values());rng=random.Random(42);boot=[]
    for _ in range(10000):
        resampled=[d for _ in values for d in rng.choice(values)]
        boot.append(sum(resampled)/len(resampled))
    boot.sort();discordant=a_only+b_only
    exact=min(1.,2*sum(comb(discordant,i) for i in range(min(a_only,b_only)+1))/(2**discordant)) if discordant else 1.
    all_values=[d for ds in values for d in ds]
    return {'paired_samples':len(all_values),'tasks':len(values),'difference':sum(all_values)/len(all_values),
            'ci95':[boot[249],boot[9749]],'mcnemar_p':exact,'independent_only_correct':a_only,'self_only_correct':b_only}


def summarize(data,directory):
    samples=[s for s in data['samples'] if s['split']=='holdout'];rows=[];models={}
    for sample in samples:
        values=[]
        for mode in ('M1','S2'):
            path=directory/(sample['id']+'-'+mode+'.json')
            row=json.loads(path.read_text()) if path.exists() else None
            if row and row.get('status')=='complete':
                if row.get('id')!=sample['id'] or row.get('expected')!=sample['expected'] or row.get('mode')!=mode:
                    raise ValueError('Result identity or frozen label mismatch')
                row={**row,'correct':row['result']['verdict']==sample['expected']}
            else:row=None
            values.append(row)
        rows.append((sample,*values))
    for mode,index in [('M1',1),('S2',2)]:
        stats={}
        for kind in KINDS:
            subset=[r for r in rows if r[0]['kind']==kind];completed=[r[index] for r in subset if r[index] is not None]
            correct=sum(r['correct'] for r in completed)
            errors=len(completed)-correct
            # Correct-case metric is false-positive rate; wrong-case metric is detection rate.
            numerator=errors if kind.startswith('correct') else correct
            stats[kind]={'planned':len(subset),'scored':len(completed),'missing':len(subset)-len(completed),
                'metric':'false_positive_rate' if kind.startswith('correct') else 'detection_rate',
                'numerator':numerator,'rate':numerator/len(completed) if completed else None,
                'ci95':wilson(numerator,len(completed)),
                'technical_fallbacks':sum(bool(r['result'].get('fallback_reason')) for r in completed)}
        models[mode]=stats
    comparisons={kind:paired(rows,kind) for kind in KINDS}
    complete=all(stats['planned']>0 and stats['missing']==0 for stats in models['M1'].values()) and all(stats['missing']==0 for stats in models['S2'].values())
    h4=complete and all(stats['rate']<=.1 if kind.startswith('correct') else stats['rate']>=.7 for kind,stats in models['M1'].items())
    h5=complete and any(comparisons[kind]['ci95'] is not None and comparisons[kind]['ci95'][0]>0 for kind in ('wrong_write','wrong_abandon'))
    reservations,settlements=call_receipts(directory/'calls.jsonl')
    return {'complete':complete,'models':models,'paired':comparisons,'H4':bool(h4),'H5':bool(h5),
            'gate':'pass' if h4 and h5 else 'not_passed' if complete else 'incomplete',
            'known_usd':sum(float(r['actual_usd']) for r in settlements.values()),
            'unsettled_reserved_usd':sum(float(r['reserve_usd']) for k,r in reservations.items() if k not in settlements),
            'scope':'E1 held-out proposals; not official task success or product acceptance',
            'limitations':data.get('limitations',[])}


if __name__=='__main__':
    report=summarize(json.loads(DATA.read_text()),RUN)
    target=ROOT/'evidence/architecture_refactor/e1_summary.json'
    target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'gate':report['gate'],'known_usd':report['known_usd']}))
