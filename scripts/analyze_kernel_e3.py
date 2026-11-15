"""Read-only E3 analysis. The output is evidence, never a model or evaluation run."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from analyze_kernel_campaign import arm_report, load, mcnemar, merge_repair
from kernel_e3_protocol import ARMS, N, ROOT, RULES, population, validate_manifest
from tau_official_summary import call_receipts


def attempt_billing(directory, manifest, rows):
    """Keep absent/truncated ledgers and row/receipt discrepancies out of cost claims."""
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


def decisions(arms, pair):
    m2, s1, s1c = (arms[name] for name in ('M2', 'S1', 'S1c'))
    lift = m2['success'] > s1c['success'] and pair['mcnemar_p'] < 0.05
    ready = all(a['cost_complete'] and not a['unsettled_calls'] for a in arms.values())
    ready = ready and all(a['total_cost_per_success_usd'] is not None for a in (m2, s1))
    near = m2['success'] >= s1['success'] - 3
    cheaper = (Decimal(str(m2['total_cost_usd'])) * s1['success']
               < Decimal(str(s1['total_cost_usd'])) * m2['success']) if ready else None
    cost = near and cheaper if ready else None
    conclusion = ('lift_and_cost_advantage_shown' if lift and cost else
                  'lift_advantage_shown' if lift else 'cost_advantage_shown' if cost else
                  'undetermined' if cost is None else 'no_advantage_shown')
    return {
        'H_lift': {'holds': lift, 'status': 'determined', 'difference': m2['success'] - s1c['success'],
                   'mcnemar_p': pair['mcnemar_p']},
        'H_cost': {'holds': cost, 'status': 'determined' if ready else 'undetermined',
                   'within_three_of_S1': near, 'cheaper_per_success': cheaper},
    }, conclusion


def m2_metrics(rows, directories):
    counts = dict.fromkeys(('expert_consultations', 'expert_reads', 'self_write_updates',
                           'amount_rule_triggers', 'review_revise', 'review_fallback',
                           'incomplete_response'), 0)
    for (name, _), entry in rows.items():
        if name != 'M2':
            continue
        for event in entry['private']:
            stage, out = event['stage'], event['output']
            if stage in {'expert_fact_expert', 'expert_policy_expert', 'expert_vision_expert'}:
                counts['expert_consultations'] += 1
                counts['expert_reads'] += sum(r['status'] == 'executed' for r in out.get('read_log', []))
            elif stage == 'expert_self_write_rebased':
                counts['self_write_updates'] += 1
            elif stage in {'guard', 'post_review_guard'}:
                counts['amount_rule_triggers'] += 'amount_arithmetic' in out.get('rule_ids', [])
            elif stage == 'proposal_review':
                counts['review_revise'] += out['verdict'] == 'revise'
                counts['review_fallback'] += bool(out.get('fallback_reason'))
    for directory in directories:
        journal = directory / 'calls.jsonl'
        reservations, _ = call_receipts(journal)
        failures = set()
        for line in journal.read_text().splitlines(keepends=True) if journal.is_file() else []:
            if not line.endswith('\n'):
                continue
            event = json.loads(line)
            if (event.get('failure_type') == 'IncompleteResponse'
                    and reservations.get(event['call_id'], {}).get('row', '').endswith('-M2')):
                failures.add(event['call_id'])
        counts['incomplete_response'] += len(failures)
    return counts


def analyse(directory):
    directory = directory.resolve()
    tasks, excluded = population()
    manifest, rows = load(directory)
    validate_manifest(manifest, tasks, excluded)
    issues = attempt_billing(directory, manifest, rows)
    directories = [directory]
    repair_dir = directory.with_name(directory.name + '.repair')
    if repair_dir.exists():
        repaired_manifest, repaired_rows = load(repair_dir)
        if repaired_manifest.get('split') != 'train' or repaired_manifest.get('repair_of') is None:
            raise ValueError('E3 repair must preserve the train split')
        extra = attempt_billing(repair_dir, repaired_manifest, repaired_rows)
        for name in ARMS:
            issues[name].extend(extra[name])
        directories.append(repair_dir)
    repair = merge_repair(directory, manifest, rows)
    arms = {name: arm_report(name, tasks, rows) for name in ARMS}
    for name, arm in arms.items():
        consultations = [e for (a, _), entry in rows.items() if a == name for e in entry['private']
                         if e['stage'] in {'expert_fact_expert', 'expert_policy_expert', 'expert_vision_expert'}]
        # E3 adds self-write/stale events; they are not new consultations.
        arm['experts'] = {'consulted': len(consultations),
                          'unavailable': sum(e['output'].get('stop_reason') == 'unavailable' for e in consultations)}
        arm['billing_issues'] = issues[name]
        arm['cost_complete'] = arm['cost_complete'] and not issues[name]
        if not arm['cost_complete']:
            for key in ('agent_cost_usd', 'total_cost_usd', 'agent_cost_per_success_usd', 'total_cost_per_success_usd'):
                arm[key] = None
        arm['known_cost_per_task_usd'] = arm['known_total_cost_usd'] / N
        arm['cost_per_task_usd'] = arm['total_cost_usd'] / N if arm['cost_complete'] else None
    first = [t for t in tasks if rows['M2', t]['outcome'] == 'success' != rows['S1c', t]['outcome']]
    second = [t for t in tasks if rows['S1c', t]['outcome'] == 'success' != rows['M2', t]['outcome']]
    pair = {'M2_only': first, 'S1c_only': second, 'mcnemar_p': mcnemar(len(first), len(second))}
    hypotheses, conclusion = decisions(arms, pair)
    per_task = {}
    for task in tasks:
        per_task[task] = {}
        for name in ARMS:
            entry = rows[name, task]
            known = sum((Decimal(s['actual_usd']) for _, s in entry['calls'] if s is not None), Decimal(0))
            complete = arms[name]['cost_complete']  # Conservative when any receipt in the arm is incomplete.
            per_task[task][name] = {'outcome': entry['outcome'], 'known_total_cost_usd': float(known),
                                    'total_cost_usd': float(known) if complete else None}
    return {
        'experiment': 'E3', 'directory': str(directory), 'split': 'train', 'tasks': N,
        'task_ids': tasks, 'excluded_development_task_ids': excluded, 'rules': RULES,
        'arms': arms, 'pairs': {'M2_vs_S1c': pair}, 'hypotheses': hypotheses,
        'conclusion': conclusion, 'M2_descriptive': m2_metrics(rows, directories),
        'descriptive_note': '事件计数为已记录事件；缺失轨迹不视作已验证的零事件。IncompleteResponse 按账本 call_id 去重，补跑按独立尝试累加。',
        'missing_trace_rows': [f'{d.name}/{i["task_id"]}-{i["architecture"]}' for d in directories
                               for i in json.loads((d / 'manifest.json').read_text())['schedule']
                               if not (d / 'official' / f'{i["task_id"]}-{i["architecture"]}.json').is_file()],
        'repair': repair, 'per_task': per_task,
        'complete': all(a['outcomes']['unfinished'] == 0 and a['cost_complete'] for a in arms.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / '.runtime/kernel-e3/run-v1')
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/architecture_refactor/kernel_e3_results.json')
    args = parser.parse_args()
    report = analyse(args.directory)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    args.output.write_text(text)
    print(text)


if __name__ == '__main__':
    main()
