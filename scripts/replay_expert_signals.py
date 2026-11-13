"""Offline specialist trigger rate on first-round conversations; no model calls."""
from collections import Counter
import json
from refactor_baseline import ROOT
from aftercare.agent_kernel.experts import signals


def rows(messages):
    for m in messages:
        row = {'role': m['role'], 'content': m.get('content')}
        if m['role'] == 'tool': row.update(tool_call_id=m.get('id'), error=m.get('error', False))
        if m.get('tool_calls'):
            row['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {
                'name': c['name'], 'arguments': json.dumps(c['arguments'])}} for c in m['tool_calls']]
        yield row


def main():
    report = {}
    for arch in ('strong_single', 'staged_single', 'multi_agent'):
        hit = Counter(); consults = []; tasks = 0
        for path in sorted((ROOT / '.runtime/tau-official-full/official').glob(f'*-{arch}.json')):
            data = json.loads(path.read_text())
            if 'messages' not in data: continue
            tasks += 1; seen = []; keys = set(); names = set()
            for row in rows(data['messages']):
                seen.append(row)
                if row['role'] in {'user', 'tool'}:  # points where the executor would act next
                    for name, _, key, _ in signals(seen): keys.add(key); names.add(name)
            hit.update(names); hit['any'] += bool(names); consults.append(min(len(keys), 3))
        report[arch] = {'tasks': tasks, 'tasks_with_signal': dict(hit), 'consults_capped_at_3': sum(consults),
                        'consults_per_task': dict(sorted(Counter(consults).items()))}
    print(json.dumps(report, indent=1))


if __name__ == '__main__': main()
