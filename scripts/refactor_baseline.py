"""Read-only outcome classification and immutable fixture inventory (no model inputs)."""
from __future__ import annotations
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = {'find_user_id_by_email', 'find_user_id_by_name_zip'}
WRITES = {'cancel_pending_order','exchange_delivered_order_items','modify_pending_order_address',
          'modify_pending_order_items','modify_pending_order_payment','modify_user_address','return_delivered_order_items'}
TRANSFER = 'transfer_to_human_agents'


def classify(row, messages):
    if row.get('official_reward') == 1:
        return 'success'
    if row.get('status') != 'completed' or row.get('official_reward') is None:
        return 'unfinished'
    calls = {c['id']: c['name'] for m in messages for c in (m.get('tool_calls') or [])}
    executed = [calls.get(m.get('id')) for m in messages if m['role']=='tool'
                and m.get('execution_status')=='executed']
    if any(name in WRITES for name in executed):
        return 'wrong_write'
    if TRANSFER in executed:
        return 'handoff'
    authenticated = any(m['role']=='tool' and calls.get(m.get('id')) in AUTH
                        and not m.get('error') and not (m.get('content') or '').lower().startswith('error')
                        for m in messages)
    return 'inaction' if authenticated else 'authentication_failed'


def baseline_rows():
    primary = ROOT/'.runtime/tau-official-full'
    summary = json.loads((primary/'summary-with-repair.json').read_text())
    for row in summary['rows']:
        folder = ROOT/'.runtime'/summary['repair_directory'] if row.get('score_source')=='repair' else primary
        path = folder/'official'/f"{row['task_id']}-{row['architecture']}.json"
        trace = json.loads(path.read_text()) if path.exists() else {'messages':[]}
        yield row, path, trace


def inventory():
    paths = []
    for name in ('tau-official-full','tau-official-full.repair'):
        paths.extend(p for p in (ROOT/'.runtime'/name).rglob('*') if p.is_file())
    paths += list((ROOT/'evidence/tau_official').glob('*.json'))
    return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--freeze',action='store_true');args=parser.parse_args()
    target=ROOT/'evidence/architecture_refactor/baseline.json'
    rows=[]; counts={}
    for row,path,trace in baseline_rows():
        outcome=classify(row,trace['messages'])
        counts.setdefault(row['architecture'],Counter())[outcome]+=1
        rows.append({'task_id':row['task_id'],'architecture':row['architecture'],'outcome':outcome,
                     'source':str(path.relative_to(ROOT)), 'official_reward':row['official_reward']})
    result={'baseline_tag':'aftercare-pre-kernel-20261113','counts':counts,'rows':rows,'sha256':inventory()}
    if args.freeze:
        with target.open('x') as stream: json.dump(result,stream,indent=2,ensure_ascii=False)
    else:
        frozen=json.loads(target.read_text())
        assert frozen==result,'Baseline files or classification changed'
    print(json.dumps(counts,indent=2))

if __name__=='__main__':main()
