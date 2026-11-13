"""Real first-round handoffs reviewed once each (M1 and S2); own $2 ceiling, no retries.

Supplement to E1: samples are the 20 real_abandon_challenges already listed in the frozen
candidate file. Labels come from official gold actions and never enter the model payload.
"""
import argparse
import fcntl
from hashlib import sha256
import json
from aftercare.agent_kernel.contracts import architectures
from aftercare.agent_kernel.review import ProposalReviewer, REVIEW_INSTRUCTION
from tau_official_budget import Budget, RowTransport, AGENT
from refactor_baseline import ROOT
from run_review_benchmark import DATA, Port, claim_sample, dump, request_data

TASKS = ROOT / '.runtime/tau-official-source/data/tau2/domains/retail/tasks.json'
WRITES = {'cancel_pending_order', 'exchange_delivered_order_items', 'modify_pending_order_address',
          'modify_pending_order_items', 'modify_pending_order_payment', 'modify_user_address',
          'return_delivered_order_items'}


def samples():
    data = json.loads(DATA.read_text())
    tasks = {t['id']: t for t in json.loads(TASKS.read_text())}
    result = []
    for c in data['real_abandon_challenges']:
        messages = json.loads((ROOT / c['source']).read_text())['messages']
        handoff = messages[c['message_index']]
        call = next(x for x in handoff['tool_calls'] if x['name'] == 'transfer_to_human_agents')
        history = messages[:c['message_index']]
        done = [json.dumps([x['name'], x['arguments']], sort_keys=True)
                for m in history for x in m.get('tool_calls') or [] if x['name'] in WRITES]
        criteria = tasks[c['task_id']]['evaluation_criteria']
        remaining = [a for a in criteria.get('actions') or [] if a['name'] in WRITES
                     and json.dumps([a['name'], a['arguments']], sort_keys=True) not in done]
        key = c['task_id'] + '-' + c['architecture']
        result.append({'id': key, 'task_id': c['task_id'], 'architecture': c['architecture'],
                       'messages': history, 'proposal': {k: call[k] for k in ('id', 'name', 'arguments')},
                       # Label only: unfinished gold work means the handoff gave up too early.
                       'expected': 'revise', 'gold_remaining_writes': remaining,
                       'gold_communicate_info': criteria.get('communicate_info') or []})
    # Controls: every officially successful handoff from the frozen candidates (expected allow).
    for s in data['samples']:
        if s['kind'] == 'correct_abandon':
            result.append({'id': 'control-' + s['task_id'] + '-' + s['source_architecture'], 'task_id': s['task_id'],
                           'architecture': s['source_architecture'], 'messages': s['messages'],
                           'proposal': s['proposal'], 'expected': 'allow', 'gold_remaining_writes': [],
                           'gold_communicate_info': []})
    return data['policy'], result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--run', default='challenge-v1')
    parser.add_argument('--modes', default='M1,S2')
    parser.add_argument('--controls', action='store_true')
    args = parser.parse_args()
    RUN = ROOT / '.runtime/e1-review' / args.run
    policy, rows = samples()
    if not args.controls: rows = [s for s in rows if s['expected'] == 'revise']
    schedule = [(s, mode) for s in rows for mode in args.modes.split(',')]
    manifest = {'dataset_sha256': sha256(DATA.read_bytes()).hexdigest(),
                'prompt_sha256': sha256(REVIEW_INSTRUCTION.encode()).hexdigest(), 'model': AGENT,
                'max_usd': '2', 'max_calls': len(schedule), 'no_retries': True,
                'schedule': [(s['id'], m) for s, m in schedule],
                'labels': {s['id']: {'remaining_writes': len(s['gold_remaining_writes']),
                                     'communicate_info': s['gold_communicate_info']} for s in rows}}
    if not args.live:
        print(json.dumps(manifest, indent=1)); return
    RUN.mkdir(exist_ok=True)
    with (RUN / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        freeze = RUN / 'manifest.json'
        if freeze.exists() and json.loads(freeze.read_text()) != json.loads(json.dumps(manifest)):
            raise SystemExit('Frozen manifest mismatch')
        if not freeze.exists(): dump(freeze, manifest)
        journal = RUN / 'calls.jsonl'
        if journal.exists() and journal.stat().st_size:
            raise SystemExit('Journal already has calls; this supplement runs once')
        budget = Budget(len(schedule), '2', journal)
        from openai import OpenAI
        secret = json.loads((ROOT / '.runtime/model-secret.json').read_text())['api_key']
        limits = {'max_agent_calls_per_run': 1, 'max_user_calls_per_run': 0, 'max_review_calls_per_run': 0,
                  'max_output_tokens_per_call': 1000}
        with OpenAI(api_key=secret, base_url='https://api.openai.com/v1', max_retries=0, timeout=60) as client:
            for sample, mode in schedule:
                key = sample['id'] + '-' + mode; target = RUN / (key + '.json')
                if not claim_sample(target, sample['id'], mode): continue
                row = RowTransport(key, limits, budget, client)
                reviewer = ProposalReviewer(Port(row))
                messages, proposal, guard, executor = request_data(sample, policy)
                result = reviewer.review(architectures()[mode], proposal, messages, policy, guard, executor)
                dump(target, {'id': sample['id'], 'mode': mode, 'task_id': sample['task_id'],
                              'architecture': sample['architecture'], 'status': 'complete',
                              'result': result.model_dump(mode='json'),
                              'gold_remaining_writes': sample['gold_remaining_writes'],
                              'gold_communicate_info': sample['gold_communicate_info'], 'expected': sample['expected'],
                              'metrics': row.metrics(), 'trace': reviewer.trace[-1].model_dump(mode='json')})
                print(key, result.verdict, result.fallback_reason or '', flush=True)
                if result.fallback_reason == 'BudgetExhausted': break


if __name__ == '__main__': main()
