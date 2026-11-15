#!/usr/bin/env python3
"""Replay specialist scheduling on frozen E2 evidence, without models or network."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from aftercare.agent_kernel.contracts import VERSION, ToolDefinition, ToolKind
from aftercare.agent_kernel.dependencies import ORDER_WRITES
from aftercare.agent_kernel.experts import (
    CONSTRAINT, NEEDS, ORDER_ID, changed_records,
    evidence_seen, signals, text, rebase_self_writes,
)
from aftercare.agent_kernel.retail_guards import facts
from replay_amount_guard import public

ARMS = ('S1', 'S2', 'S1c', 'M1', 'M2')
EXPERT_ARMS = ('M1', 'M2')
SIGNAL_NAMES = ('constrained_choice', 'multi_order', 'status_conflict')
# These order writes match tau_official_agents.WRITE_TOOLS; account writes return no order.
ORDER_TOOLS = tuple(ToolDefinition(name=name, kind=ToolKind.WRITE) for name in ORDER_WRITES)


def match_excerpt(index, utterance, match):
    return {'customer_message_index': index, 'match': match.group(),
            'match_start': match.start(), 'match_end': match.end(),
            'utterance_snippet': utterance[max(0, match.start() - 60):match.end() + 60]}


def hit_record(context, signal, key, message_index, task, arm):
    """Attach the exact customer regex evidence; never infer intent for the replay."""
    users = [(i, text(m)) for i, m in enumerate(context) if text(m)]
    state = facts(context)
    matches = []
    details = {}
    if signal == 'constrained_choice':
        i = key[1]
        utterance = text(context[i])
        matches.append(match_excerpt(i, utterance, CONSTRAINT.search(utterance)))
    elif signal == 'multi_order':
        # Preserve one supporting mention for every named, loaded order.
        named = {}
        for i, utterance in users:
            for match in ORDER_ID.finditer(utterance):
                if match.group() in state.orders:
                    named[match.group()] = match_excerpt(i, utterance, match)
        matches = list(named.values())
    elif signal == 'status_conflict':
        order_id, status = key[1:]
        for required, pattern in NEEDS:
            eligible = [(i, t, pattern.search(t)) for i, t in users if order_id in t]
            eligible = [(i, t, m) for i, t, m in eligible if m]
            if status != required and eligible:
                i, utterance, match = eligible[-1]
                matches.append(match_excerpt(i, utterance, match))
                break
        details.update(order_id=order_id)
    assert matches, f'No customer evidence for {signal}'
    return {'task': task, 'arm': arm, 'signal': signal, 'key': list(key),
            'first_hit_message_index': message_index,
            'customer_utterance_snippet': max(matches, key=lambda m: m['customer_message_index'])['utterance_snippet'],
            'customer_matches': matches, **details}


def statistics(messages, *, task=None, arm=None, simulate_consults=True, rebase_writes=True):
    """Inspect each recorded assistant turn's public prefix, before its action.

    Refreshes have no hypothetical private reads: their seen set is the current
    public evidence. No generated answer or changed executor action is simulated.
    Trigger hits are collected even after the consultation budget is exhausted.
    """
    context, events, hits = [], [], []
    hit_keys, dropped = set(), set()
    cache = {}
    new_consults = dropped_count = 0
    for index, message in enumerate(messages):
        if message['role'] == 'assistant':
            active = signals(context)
            for signal, role, key, question in active:
                if key not in hit_keys:
                    hit_keys.add(key)
                    hits.append(hit_record(context, signal, key, index, task, arm))
            if simulate_consults:
                current = evidence_seen(context)
                state = facts(context)
                pending = {key: (entry['signal'], entry['role'], key, entry['question'])
                           for key, entry in cache.items()}
                pending.update({s[2]: s for s in active})
                for signal, role, key, question in pending.values():
                    previous = cache.get(key)
                    if previous and rebase_writes:
                        rebased = rebase_self_writes(previous['seen'], current, state, ORDER_TOOLS)
                        if rebased:
                            events.append({'message_index': index, 'event': 'expert_self_write_rebased',
                                           'signal': signal, 'key': list(key), 'records': rebased, 'changed': []})
                    changed = changed_records(previous['seen'], current, key) if previous else []
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
                    cache[key] = {'signal': signal, 'role': role, 'question': question, 'seen': dict(current)}
                    dropped.discard(key)
        context.append(public(message))
    result = {'has_signal': bool(hits), 'hits': hits}
    if simulate_consults:
        changed = sorted({key for event in events for key in event['changed']})
        result.update(new_consults=new_consults, stale_dropped=dropped_count, changed=changed, events=events,
                      self_write_rebases=sum(e['event'] == 'expert_self_write_rebased' for e in events))
    return result


def group_counts(rows, *, simulate_consults):
    counts = {'tasks': len(rows), 'signal_tasks': sum(row['has_signal'] for row in rows),
              'signal_counts': {name: sum(any(hit['signal'] == name for hit in row['hits']) for row in rows)
                                for name in SIGNAL_NAMES}}
    if simulate_consults:
        counts.update({field: sum(row[field] for row in rows)
                       for field in ('new_consults', 'recorded_consults', 'stale_dropped', 'self_write_rebases')})
        counts['stale_task_count'] = sum(bool(row['changed']) for row in rows)
        counts['stale_tasks'] = [{'task': row['task'], 'changed': row['changed']} for row in rows if row['changed']]
    return counts


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
            row = {'task': task, 'arm': arm, **statistics(trajectory['messages'], task=task, arm=arm,
                                                        simulate_consults=arm in EXPERT_ARMS)}
            if arm in EXPERT_ARMS:
                experts = [event for event in trajectory['private_trace']
                           if event['stage'] in ('expert_fact_expert', 'expert_policy_expert')]
                assert all(event['output']['reads'] == 0 for event in experts), 'Replay assumes no historical private reads'
                row.update(v3_consults=statistics(trajectory['messages'], rebase_writes=False)['new_consults'],
                           recorded_consults=len(experts),
                           recorded_input_sha256=[event['input_sha256'] for event in experts])
            rows.append(row)
            arm_rows.append(row)
        counts[arm] = group_counts(arm_rows, simulate_consults=arm in EXPERT_ARMS)
        if arm in EXPERT_ARMS:
            counts[arm]['v3_consults'] = sum(row['v3_consults'] for row in arm_rows)
    return {'mode': 'offline recorded-message replay; no network or model calls', 'kernel_version': VERSION,
            'summary_sha256': sha256(summary_raw).hexdigest(),
            'limits': {'new_consults': 5},
            'hit_scope': 'First hit per signal cache key; group signal counts count distinct questions, not hits. '
                         'Indices are zero-based; snippets retain at most 60 characters on either side of each match.',
            'limitations': 'Triggers are inspected before each recorded assistant turn in all five groups; '
                          'single-agent behavior is unchanged. Consultation scheduling is simulated only for M1/M2. '
                          'Historical private reads are verified to be zero; refreshed seen sets use only public records. '
                          'No new specialist answers, private reads or changed executor actions are simulated. '
                          'recorded_consults is the historical baseline, not a replay with the expanded signals. '
                          'Frozen outcomes are unchanged; these counts do not measure v3 success rates.',
            'counts': counts, 'details_36_38_M2': [row for row in rows if row['arm'] == 'M2' and row['task'] in ('36', '38')],
            'details_60_97_27': [row for row in rows if row['task'] in ('60', '97', '27')],
            'rows': rows, 'sources': sources}


def main():
    report = replay()
    target = ROOT / 'evidence/architecture_refactor/expert_v3_1_replay.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['counts'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
