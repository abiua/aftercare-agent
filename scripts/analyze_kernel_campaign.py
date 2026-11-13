"""Analysis for the five-arm kernel campaign. Reads finished records only; never calls a model.

Every arm keeps the full schedule as its denominator: rows that are missing, unfinished or out
of budget are counted, not dropped. S0 is the first-round strong single agent, quoted from its
own frozen run and labelled as a separate run in every comparison.
"""
from __future__ import annotations

from hashlib import sha256
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from math import comb, sqrt
from pathlib import Path
from statistics import median

from refactor_baseline import ROOT, baseline_rows, classify
from tau_official_summary import call_receipts

OUTCOMES = ('success', 'wrong_write', 'handoff', 'authentication_failed', 'inaction', 'unfinished')
# Primary definition from the original plan; authentication failures are separate.
GIVE_UP = {'handoff', 'inaction'}
NO_WRITE_FAILURE = GIVE_UP | {'authentication_failed'}
NEW_ARMS = ('S1', 'S2', 'S1c', 'M1', 'M2')
PAIRS = (('M1', 'S1'), ('M1', 'S2'), ('M2', 'S1'), ('M2', 'S1c'), ('S1', 'S0'), ('M1', 'S0'))


def wilson(success, n):
    if not n:
        return None
    z = 1.95996398454
    p = success / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0, mid - half), 4), round(min(1, mid + half), 4)]


def mcnemar(first_only, second_only):
    """Exact two-sided McNemar on discordant pairs."""
    n = first_only + second_only
    if not n:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(first_only, second_only) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def load(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    reservations, settlements = call_receipts(directory / 'calls.jsonl')
    by_row = defaultdict(list)
    for call_id, event in reservations.items():
        by_row[event['row']].append((event, settlements.get(call_id)))
    rows = {}
    for item in manifest['schedule']:
        key = f"{item['task_id']}-{item['architecture']}"
        path = directory / 'rows' / f'{key}.json'
        row = json.loads(path.read_text()) if path.is_file() else {**item, 'status': 'not_run', 'official_reward': None}
        trace_path = directory / 'official' / f'{key}.json'
        trace = json.loads(trace_path.read_text()) if trace_path.is_file() else {}
        rows[item['architecture'], item['task_id']] = {
            'row': row, 'outcome': classify(row, trace.get('messages', [])),
            'private': trace.get('private_trace', []), 'calls': by_row.get(key, [])}
    return manifest, rows


def merge_repair(directory, manifest, rows):
    repair = directory.with_name(directory.name + '.repair')
    if not repair.exists():
        return None
    repaired_manifest, repaired = load(repair)
    if repaired_manifest.get('repair_of', {}).get('manifest_sha256') != sha256((directory / 'manifest.json').read_bytes()).hexdigest():
        raise ValueError('Repair source manifest mismatch')
    original_schedule = {(r['architecture'], r['task_id']): r for r in manifest['schedule']}
    seen = set()
    provenance = {}
    for item in repaired_manifest['schedule']:
        key = (item['architecture'], item['task_id'])
        if key in seen or key not in original_schedule or item != original_schedule[key]:
            raise ValueError('Repair schedule mismatch')
        seen.add(key)
        original = rows[key]
        filename = f"{item['task_id']}-{item['architecture']}"
        path = directory / 'rows' / f'{filename}.json'
        expected = repaired_manifest.get('original_attempts', {}).get(filename, {}).get('row_sha256')
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Original row changed since repair')
        if original['row']['status'] not in {'unknown', 'error'} or original['row'].get('official_reward') is not None:
            raise ValueError('Repair attempted a scored or ineligible row')
        latest = repaired[key]
        if any(latest['row'].get(k) != item[k] for k in ('task_id', 'architecture', 'seed')):
            raise ValueError('Repair row identity mismatch')
        provenance[filename] = {'original_status': original['row']['status'],
            'original_outcome': original['outcome'], 'repair_status': latest['row']['status'],
            'score_source': 'repair' if latest['row'].get('official_reward') is not None else 'unscored'}
        # Scores belong to the repair, but every attempt contributes cost, time and role calls.
        durations = [e['row'].get('duration_seconds') for e in (original, latest)]
        rows[key] = {**latest, 'row': {**latest['row'],
            'duration_seconds': sum(v for v in durations if v is not None) if any(v is not None for v in durations) else None},
            'calls': original['calls'] + latest['calls'], 'private': original['private'] + latest['private']}
    return {'directory': repair.name, 'attempts': provenance}


def arm_report(name, tasks, rows):
    outcomes = Counter()
    actor_calls = Counter()
    actor_cost = defaultdict(Decimal)
    unsettled = 0
    durations = []
    reviews = Counter()
    experts = Counter()
    for task in tasks:
        entry = rows.get((name, task))
        if entry is None:
            outcomes['unfinished'] += 1
            continue
        outcomes[entry['outcome']] += 1
        if entry['row'].get('duration_seconds') is not None:
            durations.append(entry['row']['duration_seconds'])
        for event, settlement in entry['calls']:
            actor_calls[event['actor']] += 1
            if settlement is None:
                unsettled += 1  # Unknown, never treated as zero.
            else:
                actor_cost[event['actor']] += Decimal(settlement['actual_usd'])
        for event in entry['private']:
            if event['stage'] == 'proposal_review':
                reviews['triggered'] += 1
                reviews[event['output']['verdict']] += 1
                reviews['fallback'] += bool(event['output'].get('fallback_reason'))
            elif event['stage'].startswith('expert_'):
                experts['consulted'] += 1
                experts['unavailable'] += event['output'].get('stop_reason') == 'unavailable'
    n = len(tasks)
    success = outcomes['success']
    total = sum(actor_cost.values(), Decimal(0))
    agent = sum((v for k, v in actor_cost.items() if k not in {'user', 'judge'}), Decimal(0))
    return {
        'tasks': n, 'outcomes': {k: outcomes[k] for k in OUTCOMES},
        'success': success, 'success_rate': round(success / n, 4) if n else None, 'success_ci95': wilson(success, n),
        'give_up_failures': sum(outcomes[k] for k in GIVE_UP),
        'no_write_failures': sum(outcomes[k] for k in NO_WRITE_FAILURE), 'wrong_writes': outcomes['wrong_write'],
        'calls_by_actor': dict(sorted(actor_calls.items())),
        'cost_usd_by_actor': {k: float(v) for k, v in sorted(actor_cost.items())},
        'known_agent_cost_usd': float(agent), 'known_total_cost_usd': float(total),
        'agent_cost_usd': float(agent) if not unsettled else None,
        'total_cost_usd': float(total) if not unsettled else None, 'unsettled_calls': unsettled,
        'cost_complete': unsettled == 0,
        'agent_cost_per_success_usd': float(agent / success) if success and not unsettled else None,
        'total_cost_per_success_usd': float(total / success) if success and not unsettled else None,
        'median_duration_seconds': median(durations) if durations else None,
        'review': dict(reviews), 'experts': dict(experts),
    }


def baseline_s0():
    outcomes = {}
    directories = set()
    for row, path, trace in baseline_rows():
        if row['architecture'] != 'strong_single':
            continue
        outcomes[row['task_id']] = classify(row, trace['messages'])
        directories.add(path.parent.parent)
    # Include paid calls from the original attempt as well as any technical repair.
    directories.add(ROOT / '.runtime/tau-official-full')
    repair = ROOT / '.runtime/tau-official-full.repair'
    if repair.is_dir():
        directories.add(repair)
    costs = Decimal(0)
    unsettled = 0
    for directory in directories:
        reservations, settlements = call_receipts(directory / 'calls.jsonl')
        for call_id, event in reservations.items():
            if not event['row'].endswith('-strong_single'):
                continue
            if call_id in settlements:
                costs += Decimal(settlements[call_id]['actual_usd'])
            else:
                unsettled += 1
    return outcomes, costs, unsettled


def analyse(directory, *, include_s0=True, include_repair=False):
    manifest, rows = load(directory)
    repair_info = merge_repair(directory, manifest, rows) if include_repair else None
    tasks = manifest['task_ids']
    arms = {name: arm_report(name, tasks, rows) for name in manifest.get('architectures', NEW_ARMS)}
    outcome = {(name, task): rows[name, task]['outcome'] if (name, task) in rows else 'unfinished'
               for name in arms for task in tasks}
    s0_note = None
    if include_s0 and manifest['split'] == 'test':
        s0, s0_cost, s0_unsettled = baseline_s0()
        if set(s0) == set(tasks):
            counts = Counter(s0.values())
            arms['S0'] = {'tasks': len(tasks), 'outcomes': {k: counts[k] for k in OUTCOMES},
                          'success': counts['success'], 'success_rate': round(counts['success'] / len(tasks), 4),
                          'success_ci95': wilson(counts['success'], len(tasks)),
                          'give_up_failures': sum(counts[k] for k in GIVE_UP),
                          'no_write_failures': sum(counts[k] for k in NO_WRITE_FAILURE), 'wrong_writes': counts['wrong_write'],
                          'known_total_cost_usd': float(s0_cost), 'unsettled_calls': s0_unsettled,
                          'cost_complete': not s0_unsettled,
                          'total_cost_usd': float(s0_cost) if not s0_unsettled else None,
                          'total_cost_per_success_usd': float(s0_cost / counts['success'])
                          if counts['success'] and not s0_unsettled else None,
                          'source': 'first-round run, not rerun'}
            outcome.update({('S0', task): value for task, value in s0.items()})
            s0_note = 'S0 is a different run (first round, older prompt and adapter); comparisons with it are not same-run.'
    pairs = {}
    for first, second in PAIRS:
        if first not in arms or second not in arms:
            continue
        first_only = [t for t in tasks if outcome[first, t] == 'success' != outcome[second, t]]
        second_only = [t for t in tasks if outcome[second, t] == 'success' != outcome[first, t]]
        pairs[f'{first}_vs_{second}'] = {
            'difference': arms[first]['success'] - arms[second]['success'],
            f'{first}_only': first_only, f'{second}_only': second_only,
            'mcnemar_p': mcnemar(len(first_only), len(second_only)),
            'same_run': 'S0' not in (first, second)}
    return {'directory': directory.name, 'split': manifest['split'], 'tasks': len(tasks), 'arms': arms,
            'pairs': pairs, 'hypotheses': hypotheses(arms, pairs), 's0_note': s0_note, 'repair': repair_info,
            'per_task': {task: {name: outcome[name, task] for name in arms} for task in tasks},
            'complete': all(arms[n]['outcomes']['unfinished'] == 0 and not arms[n].get('unsettled_calls') for n in arms)}


def hypotheses(arms, pairs):
    """Fixed rules from pre-registration v2. 'undetermined' when an arm is absent."""
    result = {}
    have = lambda *names: all(n in arms for n in names)
    if have('M1', 'S1'):
        m1, s1 = arms['M1'], arms['S1']
        result['H1'] = {'holds': m1['give_up_failures'] < s1['give_up_failures'] and m1['wrong_writes'] <= s1['wrong_writes'] + 1,
                        'M1_give_up': m1['give_up_failures'], 'S1_give_up': s1['give_up_failures'],
                        'M1_wrong_writes': m1['wrong_writes'], 'S1_wrong_writes': s1['wrong_writes']}
        pair = pairs['M1_vs_S1']
        result['H2'] = {'holds': m1['success'] > s1['success'], 'difference': pair['difference'],
                        'mcnemar_p': pair['mcnemar_p'], 'significant_at_0.05': pair['mcnemar_p'] < 0.05}
    if have('M2', 'S1', 'S1c'):
        m2, s1, s1c = arms['M2'], arms['S1'], arms['S1c']
        cost_ready = all(not arm.get('unsettled_calls') and arm.get('cost_complete', True)
                         and arm['total_cost_per_success_usd'] is not None for arm in (m2, s1))
        cheaper = (m2['total_cost_per_success_usd'] < s1['total_cost_per_success_usd']) if cost_ready else None
        near = m2['success'] >= s1['success'] - 2
        lift = m2['success'] >= s1c['success'] + 3
        cost_branch = cheaper if near else False
        holds = True if lift else cost_branch
        result['H3'] = {'holds': holds, 'status': 'undetermined' if holds is None else 'determined',
                        'cost_comparison_status': 'determined' if cost_ready else 'undetermined',
                        'within_two_of_S1_and_cheaper_per_success': cost_branch,
                        'at_least_three_more_than_S1c': lift, 'M2_success': m2['success'], 'S1_success': s1['success'],
                        'S1c_success': s1c['success']}
    if have('M1', 'S2'):
        pair = pairs['M1_vs_S2']
        result['H5_end_to_end'] = {'exploratory': True, 'difference': pair['difference'], 'mcnemar_p': pair['mcnemar_p']}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--include-repair', action='store_true')
    args = parser.parse_args()
    report = analyse(args.directory.resolve(), include_repair=args.include_repair)
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == '__main__':
    main()
