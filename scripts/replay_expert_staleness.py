#!/usr/bin/env python3
"""Replay specialist scheduling on frozen E2 evidence, without models or network."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from aftercare.agent_kernel.contracts import VERSION
from aftercare.agent_kernel.experts import changed_records, evidence_seen, signals
from replay_amount_guard import public

ARMS = ('M1', 'M2')


def statistics(messages):
    """Count consultations before each recorded assistant turn, using its public prefix.

    Refreshes have no hypothetical private reads: their seen set is the current
    public evidence. No generated answer or changed executor action is simulated.
    """
    context, events = [], []
    old_keys, dropped = set(), set()
    cache = {}
    old_consults = new_consults = dropped_count = 0
    has_signal = False
    for index, message in enumerate(messages):
        if message['role'] == 'assistant':
            active = signals(context)
            has_signal |= bool(active)
            for signal, role, key, question in active:
                if key not in old_keys and old_consults < 3:
                    old_keys.add(key)
                    old_consults += 1
            current = evidence_seen(context)
            pending = {key: (entry['signal'], entry['role'], key, entry['question'])
                       for key, entry in cache.items()}
            pending.update({s[2]: s for s in active})
            for signal, role, key, question in pending.values():
                previous = cache.get(key)
                changed = changed_records(previous['seen'], current) if previous else []
                if previous and not changed:
                    continue
                if new_consults >= 5:
                    if previous and key not in dropped:
                        dropped.add(key)
                        dropped_count += 1
                        events.append({'message_index': index, 'event': 'expert_stale_dropped',
                                       'signal': signal, 'key': list(key), 'changed': changed})
                    continue
                new_consults += 1
                events.append({'message_index': index, 'event': 'refresh' if previous else 'consult',
                               'signal': signal, 'key': list(key), 'changed': changed,
                               'consult_number': new_consults})
                cache[key] = {'signal': signal, 'role': role, 'question': question, 'seen': current}
                dropped.discard(key)
        context.append(public(message))
    changed = sorted({key for event in events for key in event['changed']})
    return {'has_signal': has_signal, 'old_consults': old_consults, 'new_consults': new_consults,
            'stale_dropped': dropped_count, 'changed': changed, 'events': events}


def replay(root=ROOT):
    source = root / 'evidence/architecture_refactor/e2_final.json'
    summary_raw = source.read_bytes()
    summary = json.loads(summary_raw)
    assert len(summary['per_task']) == 40, 'Expected the frozen 40-task E2 population'
    rows, sources, counts = [], [], {}
    for arm in ARMS:
        arm_rows = []
        for task in summary['per_task']:
            key = f'{task}-{arm}'
            folder = 'kernel-e2-20261113.repair' if key in summary['repair']['attempts'] else 'kernel-e2-20261113'
            path = root / '.runtime' / folder / 'official' / f'{key}.json'
            raw = path.read_bytes()
            source_name = str(path.relative_to(root))
            sources.append({'task': task, 'arm': arm, 'path': source_name, 'sha256': sha256(raw).hexdigest()})
            trajectory = json.loads(raw)
            private = trajectory['private_trace']
            experts = [event for event in private if event['stage'] in ('expert_fact_expert', 'expert_policy_expert')]
            assert all(event['output']['reads'] == 0 for event in experts), 'Replay assumes no historical private reads'
            row = {'task': task, 'arm': arm, **statistics(trajectory['messages']),
                   'recorded_consults': len(experts),
                   'recorded_input_sha256': [event['input_sha256'] for event in experts]}
            rows.append(row)
            arm_rows.append(row)
        counts[arm] = {'tasks': len(arm_rows), 'signal_tasks': sum(row['has_signal'] for row in arm_rows),
                      **{field: sum(row[field] for row in arm_rows)
                         for field in ('old_consults', 'new_consults', 'recorded_consults', 'stale_dropped')},
                      'stale_tasks': [{'task': row['task'], 'changed': row['changed']}
                                      for row in arm_rows if row['changed']]}
    return {'mode': 'offline recorded-message replay; no network or model calls', 'kernel_version': VERSION,
            'summary_sha256': sha256(summary_raw).hexdigest(),
            'limits': {'old_consults': 3, 'new_consults': 5},
            'limitations': 'Scheduling is replayed before each recorded assistant turn. Historical private reads '
                          'are verified to be zero; refreshed seen sets use only public records. '
                          'No new specialist answers, private reads or changed executor actions are simulated. '
                          'Frozen outcomes are unchanged; these counts do not measure v3 success rates.',
            'counts': counts, 'details_36_38_M2': [row for row in rows if row['arm'] == 'M2' and row['task'] in ('36', '38')],
            'rows': rows, 'sources': sources}


def main():
    report = replay()
    target = ROOT / 'evidence/architecture_refactor/expert_v3_replay.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'counts': report['counts'], 'details_36_38_M2': report['details_36_38_M2']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
