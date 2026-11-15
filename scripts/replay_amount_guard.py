#!/usr/bin/env python3
"""Replay the v3 amount rule on frozen E2 messages; no model or network imports."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from aftercare.agent_kernel.amount_arithmetic import amount_findings, previous_assistant_amounts, cents
from aftercare.agent_kernel.contracts import StepProposal, VERSION
from aftercare.agent_kernel.retail_guards import RetailGuards, facts

ARMS = ('S1', 'S2', 'S1c', 'M1', 'M2')


def public(message):
    row = {'role':message['role'], 'content':message.get('content')}
    if message['role'] == 'tool':
        row.update(tool_call_id=message['id'], error=message.get('error', False))
    if message.get('tool_calls'):
        row['tool_calls'] = [{'id':c['id'], 'function':{'name':c['name'], 'arguments':c['arguments']}}
                             for c in message['tool_calls']]
    return row


def replay(root=ROOT):
    source = root / 'evidence/architecture_refactor/e2_final.json'
    summary = json.loads(source.read_bytes())
    assert len(summary['per_task']) == 40, 'Expected the frozen 40-task E2 population'
    guard = RetailGuards()
    rows, sources, counts = [], [], {}
    for arm in ARMS:
        triggered, succeeded = set(), set()
        events = Counter()
        for task, outcomes in summary['per_task'].items():
            key = f'{task}-{arm}'
            folder = 'kernel-e2-20261113.repair' if key in summary['repair']['attempts'] else 'kernel-e2-20261113'
            path = root / '.runtime' / folder / 'official' / f'{key}.json'
            raw = path.read_bytes()
            source_name = str(path.relative_to(root))
            sources.append({'task':task, 'arm':arm, 'path':source_name, 'sha256':sha256(raw).hexdigest()})
            context = []
            for index, message in enumerate(json.loads(raw)['messages']):
                text = message.get('content')
                if message['role'] == 'assistant' and text and not message.get('tool_calls'):
                    checked = guard.check(context, StepProposal(text=text), ())
                    if 'amount_arithmetic' in checked.rule_ids:
                        finding = amount_findings(context, text, facts(context).orders)
                        assert finding is not None
                        previous = previous_assistant_amounts(context)
                        first = {value:cents(value) not in previous for value in finding['uncheckable_amounts']}
                        assert all(first.values()), 'Repeated assistant amounts must not trigger'
                        rows.append({'task':task, 'arm':arm, 'outcome':outcomes[arm],
                                     'message_index':index, **finding, 'first_occurrence':first,
                                     'text':text, 'source':source_name})
                        triggered.add(task)
                        events['triggers'] += 1
                        if outcomes[arm] == 'success':
                            succeeded.add(task)
                            events['successful_triggers'] += 1
                context.append(public(message))
        counts[arm] = {'tasks':40, 'triggered_tasks':len(triggered),
                       'successful_triggered_tasks':len(succeeded),
                       'triggers':events['triggers'], 'successful_triggers':events['successful_triggers']}
    example = [r for r in rows if r['task'] == '108' and r['arm'] == 'M2']
    assert any(r['message_index'] == 14 and '347.93' in r['uncheckable_amounts'] for r in example), \
        '108-M2 must flag the first 347.93 at message 14'
    assert not any(r['message_index'] in (16, 20) for r in example), '108-M2 repeats must not trigger'
    return {'mode':'offline recorded-message replay; no network or model calls',
            'kernel_version':VERSION, 'amount_rule':'price-like sources, customer numbers, first occurrence, three rounds',
            'first_occurrence_scope':'No earlier assistant message contains this currency-marked amount (H).',
            'summary_sha256':sha256(source.read_bytes()).hexdigest(),
            'limitations':'Triggers mean uncheckable by this bounded rule, not proven arithmetic errors. '
                          'Official outcomes remain frozen; this replay does not measure v3 success rates.',
            'counts':counts, 'rows':rows, 'sources':sources}


def main():
    report = replay()
    target = ROOT / 'evidence/architecture_refactor/amount_guard_replay.json'
    # This derived replay report is explicitly refreshable; frozen sources remain read-only.
    with target.open('w') as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['counts'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
