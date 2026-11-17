"""Read-only E4 inputs; emit descriptive evidence without running or judging models."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from analyze_kernel_campaign import OUTCOMES, arm_report, load, mcnemar, merge_repair
from analyze_kernel_e3 import m2_metrics
from kernel_e4_protocol import (
    ARMS, CELLS, DESCRIPTIVE_NOTE, E3_MANIFEST, E3_RESULTS, N, PREREGISTRATION, ROOT,
    RULES, population, schedule_sha256, validate_manifest,
)
from tau_official_summary import call_receipts

PAIRS = (('M2', 'M2r', 'review'), ('M2x', 'S1c', 'review'),
         ('M2', 'M2x', 'expert'), ('M2r', 'S1c', 'expert'))
EXPERT_STAGES = {'expert_fact_expert', 'expert_policy_expert', 'expert_vision_expert'}


def attempt_billing(directory, manifest, rows):
    """E3 billing checks for E4 arms; leave the frozen E3 implementation untouched."""
    issues = {name: [] for name in ARMS}
    journal = directory / 'calls.jsonl'
    data = journal.read_bytes() if journal.is_file() else None
    if data is None or (data and not data.endswith(b'\n')):
        for name in ARMS:
            issues[name].append(f'{directory.name}: missing or incomplete billing journal')
    reservations, settlements = call_receipts(journal)
    keys = {f"{i['task_id']}-{i['architecture']}" for i in manifest['schedule']}
    if (set(settlements) - set(reservations)
            or any(e['row'] not in keys for e in reservations.values())):
        for name in ARMS:
            issues[name].append(f'{directory.name}: orphan billing receipt')
    for (name, task), entry in rows.items():
        row, calls = entry['row'], entry['calls']
        reasons = []
        if row['status'] in {'running', 'not_run'}:
            reasons.append('unfinished row')
        counts = Counter(e['role'] for e, _ in calls)
        for role in ('agent', 'user', 'review'):
            if row.get(f'{role}_calls') != counts[role]:
                reasons.append(f'{role} call count missing or mismatched')
            costs = [Decimal(s['actual_usd']) for e, s in calls if e['role'] == role and s is not None]
            reported = row.get(f'{role}_cost_usd')
            if reported is None or not all(c.is_finite() and c >= 0 for c in costs):
                reasons.append(f'{role} cost missing or invalid')
            elif abs(Decimal(str(reported)) - sum(costs, Decimal(0))) > Decimal('0.000000001'):
                reasons.append(f'{role} row cost differs from receipts')
        if reasons:
            issues[name].append(f'{directory.name}/{task}-{name}: ' + '; '.join(reasons))
    return issues


def mechanism_counts(name, rows, directories):
    # Reuse E3's event counter by projecting this arm to its M2 key, without changing records.
    projected = {('M2', task): entry for (arm, task), entry in rows.items() if arm == name}
    counts = m2_metrics(projected, [])
    for directory in directories:
        journal = directory / 'calls.jsonl'
        reservations, _ = call_receipts(journal)
        failures = set()
        for line in journal.read_text().splitlines(keepends=True) if journal.is_file() else []:
            if not line.endswith('\n'):
                continue
            event = json.loads(line)
            if (event.get('failure_type') == 'IncompleteResponse'
                    and reservations.get(event['call_id'], {}).get('row', '').endswith('-' + name)):
                failures.add(event['call_id'])
        counts['incomplete_response'] += len(failures)
    return counts


def read_e3_results(path, preregistration, tasks, excluded):
    # Hash the exact bytes before parsing or using any E3 result values.
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != preregistration.get('kernel_e3_results_sha256'):
        raise ValueError('E3 results SHA256 mismatch with E4 preregistration')
    data = json.loads(raw)
    if (data.get('experiment') != 'E3' or data.get('split') != 'train'
            or data.get('tasks') != N or data.get('task_ids') != tasks
            or data.get('excluded_development_task_ids') != excluded
            or set(data.get('per_task', {})) != set(tasks)):
        raise ValueError('E3 results population differs from E4')
    for name in ('S1c', 'M2'):
        outcomes = Counter(data['per_task'][task][name]['outcome'] for task in tasks)
        arm = data['arms'][name]
        if (not set(outcomes) <= set(OUTCOMES) or arm['tasks'] != N
                or arm['success'] != outcomes['success']
                or arm['outcomes'] != {key: outcomes[key] for key in OUTCOMES}):
            raise ValueError(f'E3 aggregate/per-task mismatch: {name}')
    return data


def analyse(directory, *, e3_results=None, preregistration=None, e3_manifest_path=None):
    directory = directory.resolve()
    e3_results = e3_results or E3_RESULTS
    e3_manifest_path = e3_manifest_path or E3_MANIFEST
    registration = json.loads((preregistration or PREREGISTRATION).read_bytes())
    if sha256(e3_manifest_path.read_bytes()).hexdigest() != registration.get('e3_manifest_sha256'):
        raise ValueError('E3 manifest SHA256 mismatch with E4 preregistration')
    tasks, excluded = population()
    e3 = read_e3_results(e3_results, registration, tasks, excluded)
    manifest, rows = load(directory)
    validate_manifest(manifest, tasks, excluded, e3_manifest_path=e3_manifest_path)
    if schedule_sha256(manifest['schedule']) != registration.get('schedule_sha256'):
        raise ValueError('E4 schedule SHA256 mismatch with preregistration')
    issues = attempt_billing(directory, manifest, rows)
    directories = [directory]
    repair_dir = directory.with_name(directory.name + '.repair')
    if repair_dir.exists():
        repaired_manifest, repaired_rows = load(repair_dir)
        if repaired_manifest.get('split') != 'train' or repaired_manifest.get('repair_of') is None:
            raise ValueError('E4 repair must preserve the train split')
        extra = attempt_billing(repair_dir, repaired_manifest, repaired_rows)
        for name in ARMS:
            issues[name].extend(extra[name])
        directories.append(repair_dir)
    repair = merge_repair(directory, manifest, rows)
    arms = {name: {**deepcopy(e3['arms'][name]), 'source': 'E3'} for name in ('S1c', 'M2')}
    for name in ARMS:
        arm = arm_report(name, tasks, rows)
        consultations = [e for (a, _), entry in rows.items() if a == name for e in entry['private']
                         if e['stage'] in EXPERT_STAGES]
        arm['experts'] = {'consulted': len(consultations),
                          'unavailable': sum(e['output'].get('stop_reason') == 'unavailable' for e in consultations)}
        arm['billing_issues'] = issues[name]
        arm['cost_complete'] = arm['cost_complete'] and not issues[name]
        if not arm['cost_complete']:
            for key in ('agent_cost_usd', 'total_cost_usd', 'agent_cost_per_success_usd', 'total_cost_per_success_usd'):
                arm[key] = None
        arm['known_cost_per_task_usd'] = arm['known_total_cost_usd'] / N
        arm['cost_per_task_usd'] = arm['total_cost_usd'] / N if arm['cost_complete'] else None
        arms[name] = {**arm, 'source': 'E4'}
    arms = {name: arms[name] for name in CELLS}
    per_task = {}
    for task in tasks:
        per_task[task] = {}
        for name in CELLS:
            if name not in ARMS:
                per_task[task][name] = {**deepcopy(e3['per_task'][task][name]), 'source': 'E3'}
                continue
            entry = rows[name, task]
            known = sum((Decimal(s['actual_usd']) for _, s in entry['calls'] if s is not None), Decimal(0))
            per_task[task][name] = {
                'outcome': entry['outcome'], 'known_total_cost_usd': float(known),
                'total_cost_usd': float(known) if arms[name]['cost_complete'] else None, 'source': 'E4',
            }
    pairs = {}
    for first, second, effect in PAIRS:
        first_only = [t for t in tasks if per_task[t][first]['outcome'] == 'success' != per_task[t][second]['outcome']]
        second_only = [t for t in tasks if per_task[t][second]['outcome'] == 'success' != per_task[t][first]['outcome']]
        pairs[f'{first}_vs_{second}'] = {
            'effect': effect, 'difference': arms[first]['success'] - arms[second]['success'],
            f'{first}_only': first_only, f'{second}_only': second_only,
            f'{first}_only_count': len(first_only), f'{second}_only_count': len(second_only),
            'mcnemar_p': mcnemar(len(first_only), len(second_only)), 'same_run': False,
        }
    mechanisms = {name: mechanism_counts(name, rows, directories) for name in ARMS}
    anomalies = []
    disabled_counts = {
        'M2x': {key: mechanisms['M2x'][key] for key in ('expert_consultations', 'expert_reads', 'self_write_updates')},
        'M2r': {**{key: mechanisms['M2r'][key] for key in ('review_revise', 'review_fallback')},
                'review_triggered': arms['M2r']['review'].get('triggered', 0)},
    }
    for name, counts in disabled_counts.items():
        # Also catch billed roles when a private trace is missing.
        actors = ('fact_expert', 'policy_expert', 'vision_expert') if name == 'M2x' else ('reviewer',)
        counts.update({f'{actor}_calls': arms[name]['calls_by_actor'].get(actor, 0) for actor in actors})
        for metric, count in counts.items():
            if count:
                anomalies.append({'architecture': name, 'metric': metric, 'count': count, 'expected': 0})
    missing = {'rows': [], 'trace_rows': []}
    for d in directories:
        for item in json.loads((d / 'manifest.json').read_bytes())['schedule']:
            key = f"{item['task_id']}-{item['architecture']}"
            for kind, folder in (('rows', 'rows'), ('trace_rows', 'official')):
                if not (d / folder / f'{key}.json').is_file():
                    missing[kind].append(f'E4/{d.name}/{key}')
    for key in e3.get('missing_trace_rows', []):
        if key.endswith(('-S1c', '-M2')):
            missing['trace_rows'].append(f'E3/{key}')
    return {
        'experiment': 'E4', 'descriptive': True, 'descriptive_note': DESCRIPTIVE_NOTE,
        'directory': str(directory), 'split': 'train', 'tasks': N, 'task_ids': tasks,
        'excluded_development_task_ids': excluded, 'rules': RULES,
        'sources': {'E3': {'path': str(e3_results), 'sha256': registration['kernel_e3_results_sha256'],
                           'complete': e3['complete']},
                    'E4': {'directory': str(directory), 'preregistration_status': registration.get('status')}},
        'arms': arms, 'pairs': pairs,
        'interaction': {'formula': '(M2 - M2r) - (M2x - S1c)',
                        'success_count_difference': (arms['M2']['success'] - arms['M2r']['success'])
                                                    - (arms['M2x']['success'] - arms['S1c']['success'])},
        'mechanisms': mechanisms, 'anomalies': anomalies,
        'mechanism_note': '沿用 E3 M2_descriptive 口径，事件为已记录事件；缺失轨迹不视作已验证的零事件。IncompleteResponse 按每次尝试的 call_id 去重，补跑独立累计。官方裁判 judge 不算独立审查 reviewer。',
        'per_task': per_task, 'repair': repair,
        'missing_rows': missing['rows'], 'missing_trace_rows': missing['trace_rows'],
        'unfinished_rows': [f'{arms[name]["source"]}/{task}-{name}' for task in tasks for name in CELLS
                            if per_task[task][name]['outcome'] == 'unfinished'],
        'unsettled_calls': sum(a['unsettled_calls'] for a in arms.values()),
        'complete': (all(a['outcomes']['unfinished'] == 0 and a['cost_complete'] for a in arms.values())
                     and not missing['rows'] and not missing['trace_rows']),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / '.runtime/kernel-e4/run-v1')
    parser.add_argument('--e3-results', type=Path, default=E3_RESULTS)
    parser.add_argument('--preregistration', type=Path, default=PREREGISTRATION)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/architecture_refactor/kernel_e4_results.json')
    args = parser.parse_args()
    report = analyse(args.directory, e3_results=args.e3_results, preregistration=args.preregistration)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    # New evidence only; never overwrite an existing frozen result or an input.
    with args.output.open('x') as stream:
        stream.write(text)
    print(text)


if __name__ == '__main__':
    main()
