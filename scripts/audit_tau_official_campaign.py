"""Read-only consistency audit and paired comparison of an official campaign."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
from statistics import median

from tau_official_summary import ARCHITECTURES, call_receipts, row_key, summarize, write_json


def audit(directory):
    summary = summarize(directory)
    reservations, settlements = call_receipts(directory / 'calls.jsonl')
    problems = []
    private_reads = Counter()
    public_tools = Counter()
    for row in summary['rows']:
        key = row_key(row)
        path = directory / 'official' / f'{key}.json'
        if not path.exists():
            if row['official_reward'] is not None:
                problems.append(f'{key}: missing official scoring artifact')
            continue
        raw = json.loads(path.read_text())
        reward = raw.get('reward_info')
        if reward and row['official_reward'] != reward['reward']:
            problems.append(f'{key}: official reward differs from summary')
        for event in raw.get('private_trace', []):
            if event['event'] != 'private_tool' or event['status'] != 'executed':
                continue
            if event['tool'] not in {
                'get_user_details', 'get_order_details', 'get_product_details',
                'get_item_details', 'list_all_product_types',
            }:
                problems.append(f'{key}: non-read private tool')
            private_reads[row['architecture']] += 1
        public_tools[row['architecture']] += sum(
            len(message.get('tool_calls') or []) for message in raw.get('messages', [])
        )
        for role in ('agent', 'user', 'review'):
            ids = [ident for ident, event in reservations.items()
                   if event['row'] == key and event['role'] == role]
            if row[f'{role}_calls'] != len(ids):
                problems.append(f'{key}: {role} call count differs from journal')
            if row[f'{role}_cost_usd'] is not None and all(i in settlements for i in ids):
                paid = sum((Decimal(settlements[i]['actual_usd']) for i in ids), Decimal(0))
                if abs(float(paid) - float(row[f'{role}_cost_usd'])) > 0.00000001:
                    problems.append(f'{key}: {role} cost differs from journal')
    comparisons = []
    for arm in ARCHITECTURES:
        group = [r for r in summary['rows'] if r['architecture'] == arm]
        scores = [r for r in group if r['official_reward'] is not None]
        passed = sum(r['official_reward'] == 1 for r in scores)
        costs = [r[f'{role}_cost_usd'] for r in group for role in ('agent', 'user', 'review')]
        total = sum(costs) if all(cost is not None for cost in costs) else None
        elapsed = [r['duration_seconds'] for r in group if r['duration_seconds'] is not None]
        comparisons.append({
            'architecture': arm, 'planned': len(group), 'scored': len(scores),
            'passed': passed, 'pass_rate': passed / len(group) if group else None,
            'total_cost_usd': total,
            'cost_per_task_usd': total / len(group) if total is not None and group else None,
            'cost_per_success_usd': total / passed if total is not None and passed else None,
            'median_seconds': median(elapsed) if elapsed else None,
            'agent_calls': sum(r['agent_calls'] for r in group),
            'public_tool_calls': public_tools[arm], 'private_read_calls': private_reads[arm],
            'statuses': dict(Counter(r['status'] for r in group)),
        })
    by_key = {(r['task_id'], r['architecture']): r for r in summary['rows']}
    paired = Counter()
    for task in summary['task_ids']:
        if (task, 'strong_single') not in by_key or (task, 'multi_agent') not in by_key:
            continue
        single = by_key[task, 'strong_single']['official_reward']
        multi = by_key[task, 'multi_agent']['official_reward']
        label = ('unscored_pair' if single is None or multi is None else
                 'both_pass' if single == multi == 1 else
                 'multi_only' if multi == 1 else
                 'single_only' if single == 1 else 'both_fail')
        paired[label] += 1
    return {'complete': summary['complete'], 'all_scored': summary['all_scored'],
            'consistency_errors': problems, 'architectures': comparisons,
            'multi_vs_strong_paired': dict(paired), 'billing': summary['billing']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = audit(args.directory)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(report['consistency_errors']))
