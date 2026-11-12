"""Small-sample paired reports: missing human labels and unknown prices stay missing."""
from collections import defaultdict
from decimal import Decimal
from statistics import mean

from .campaign_quality import quality_metrics


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['complexity'], row['arm'], row['strategy'])].append(row)
    result = []
    for (complexity, arm, strategy), members in sorted(groups.items()):
        priced = all(row.get('cost_usd') is not None for row in members)
        cost = sum((Decimal(row['cost_usd']) for row in members), Decimal(0)) if priced else None
        candidates = sum(bool(row.get('candidate_present')) for row in members)
        labelled = all('development_accepted' in row for row in members)
        accepted = sum(row.get('development_accepted',False) for row in members)
        result.append({
            'complexity': complexity, 'arm': arm, 'strategy': strategy, 'n': len(members),
            'candidate_rate': candidates / len(members),
            'development_acceptance_rate': mean(r['development_accepted'] for r in members) if all('development_accepted' in r for r in members) else None,
            'unsafe_payment_proposal_rate': mean(r['unsafe_payment_proposal'] for r in members) if all('unsafe_payment_proposal' in r for r in members) else None,
            'proposal_rule_block_rate': mean(bool(row.get('program_checks')) for row in members),
            'recheck_rate': mean(row.get('rechecks', 0) > 0 for row in members),
            'handoff_rate': mean(row.get('status') != 'COMPLETED' for row in members),
            'mean_calls': mean(row.get('calls', 0) for row in members),
            'mean_seconds': mean(row.get('elapsed_seconds', 0) for row in members),
            'cost_usd': str(cost) if cost is not None else None,
            'cost_per_case_usd': str(cost / len(members)) if cost is not None else None,
            'cost_per_development_success_usd': str(cost / accepted) if cost is not None and labelled and accepted else None,
            'cost_per_candidate_usd': str(cost / candidates) if cost is not None and candidates else None,
            'upgrade_rate': mean(bool(row.get('upgrades')) for row in members),
            'unknown_cost_calls': sum(row.get('unknown_cost_calls', 0) for row in members),
            **quality_metrics(members),
        })
    return result


def protocol_valid(row, events):
    if row['arm'] != 'multi_synthesis_only':
        return True
    # A renamed arm is not an ablation. Check the executed graph's own events.
    routed = any(e['kind']=='investigation_route' and e['payload'].get('counterexample_ablation') is True for e in events)
    roles = [e['payload'].get('role') for e in events if e['kind']=='investigation_input']
    return routed and 'verifier' not in roles and ('synthesizer' in roles or not row.get('candidate_present'))
