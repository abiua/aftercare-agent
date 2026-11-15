"""Review-only v2 lock-order minitest. Default: manifest only; never executes tools.

--live permits one immutable schedule in a new run directory, with only the
reviewer's built-in retry. --summarize reads that directory without changing it.
Rates retain the frozen schedule denominator; missing reviews are listed separately.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import fcntl
from hashlib import sha256
import json
from pathlib import Path

from aftercare.agent_kernel.contracts import ReviewResult, StepProposal, ToolCall, VERSION, architectures
from aftercare.agent_kernel.dependencies import ITEMS, fulfills
from aftercare.agent_kernel.retail_guards import RetailGuards
from aftercare.agent_kernel.review import ProposalReviewer, REVIEW_INSTRUCTION
from analyze_kernel_campaign import wilson
from refactor_baseline import ROOT
from replay_review_v2 import public
from run_review_benchmark import Port, claim_sample, dump
from tau_official_budget import Budget, RowTransport
from tau_official_summary import call_receipts

DATA = ROOT / 'evidence/architecture_refactor/review_v2_replay.json'
REPORT = ROOT / 'evidence/architecture_refactor/review_v2_minitest.json'
MAX_USD = '1.5'
# RowTransport buckets the reviewer actor under agent, not the evaluator's review bucket.
LIMITS = {'max_agent_calls_per_run': 2, 'max_user_calls_per_run': 0,
          'max_review_calls_per_run': 0, 'max_output_tokens_per_call': 4000}


def samples(data_path=None, root=None):
    root = ROOT if root is None else Path(root)
    data_path = DATA if data_path is None else Path(data_path)
    rows = json.loads(data_path.read_bytes())['rows']
    selected = [(r, 'false-block', r['steps']) for r in rows
                if r['arm'] == 'M1' and r['outcome'] == 'success']
    controls = [r for r in rows if str(r['task']) == '97' and r['arm'] == 'M2']
    if len(controls) != 1:
        raise ValueError('Expected exactly one 97-M2 source row')
    control = controls[0]
    item_steps = [s for s in control['steps'] if s['tool'] == ITEMS]
    if not item_steps:
        raise ValueError('97-M2 has no item modification step')
    selected.append((control, 'block', [min(item_steps, key=lambda s: s['message_index'])]))
    result = []
    for row, group, steps in selected:
        source = (root / row['source']).read_bytes()
        # Hash the exact bytes subsequently parsed, before interpreting any trajectory data.
        if sha256(source).hexdigest() != row['source_sha256']:
            raise ValueError(f"Source SHA256 mismatch: {row['source']}")
        trajectory = json.loads(source)['messages']
        if group == 'block':
            first = next(i for i, m in enumerate(trajectory)
                         if any(c['name'] == ITEMS for c in m.get('tool_calls') or []))
            if steps[0]['message_index'] != first:
                raise ValueError('97-M2 inventory does not identify the first item modification')
        for step in steps:
            index = step['message_index']
            calls = [c for c in trajectory[index].get('tool_calls') or []
                     if c['name'] == step['tool']]
            if len(calls) != 1:
                raise ValueError(f"Ambiguous or missing call: {row['task']}-{row['arm']}-m{index}")
            proposal = StepProposal(calls=(ToolCall(**{k: calls[0][k]
                                                     for k in ('id', 'name', 'arguments')}),))
            result.append({'id': f"{row['task']}-{row['arm']}-m{index}", 'group': group,
                           'expectation': {'blocking_reason': 'order_dependency_conflict'
                                           if group == 'block' else None},
                           'source': row['source'], 'source_sha256': row['source_sha256'],
                           'message_index': index, 'messages': public(trajectory[:index]),
                           'proposal': proposal.model_dump(mode='json')})
    if (Counter(s['group'] for s in result) != {'false-block': 33, 'block': 1}
            or len({s['id'] for s in result}) != 34):
        raise ValueError('Expected 34 unique samples: 33 false-block and 1 block')
    return result


def official_inputs():
    # Same pinned retail source, schemas and domain policy as the official E2 runner.
    # No agent, orchestrator or tool execution is constructed.
    from run_tau_official_campaign import load_official_source
    load_official_source()
    from tau2.runner import build_environment
    from tau2.runner.build import agent_domain_policy
    from tau_official_agents import definitions
    environment = build_environment('retail')
    return agent_domain_policy(environment), definitions(environment.get_tools())


def manifest_for(schedule, policy, *, live=False):
    architecture = architectures()['M1']
    if (live and VERSION != 'aftercare-kernel-v2') or architecture.reviewer.model != 'gpt-6.1-sol':
        raise ValueError('This minitest requires kernel v2 and the M1 gpt-6.1-sol reviewer')
    return {'sample_ids': [s['id'] for s in schedule], 'sample_count': len(schedule),
            'group_counts': dict(Counter(s['group'] for s in schedule)),
            'max_calls': 2 * len(schedule), 'max_usd': MAX_USD,
            'max_review_calls_per_sample': 2, 'limits': LIMITS,
            'sample_retries': 0, 'built_in_review_retries': 1,
            'architecture': architecture.model_dump(mode='json'), 'kernel_version': VERSION,
            'prompt_sha256': sha256(REVIEW_INSTRUCTION.encode()).hexdigest(),
            'policy_sha256': sha256(policy.encode()).hexdigest(),
            'dataset_sha256': sha256(DATA.read_bytes()).hexdigest(),
            'source_sha256': {s['source']: s['source_sha256'] for s in schedule},
            'schedule': [{k: v for k, v in s.items() if k != 'messages'} for s in schedule]}


def review_sample(sample, policy, tools, row):
    proposal = StepProposal.model_validate(sample['proposal'])
    guard = RetailGuards().check(sample['messages'], proposal, tools)
    record = {k: sample[k] for k in ('id', 'group', 'expectation', 'proposal')}
    record.update(guard_result=guard.model_dump(mode='json'), result=None,
                  trace_output=None, used_retry=False)
    if not guard.allowed:
        return {**record, 'status': 'guard_not_allowed', 'metrics': row.metrics()}
    reviewer = ProposalReviewer(Port(row))
    try:
        result = reviewer.review(architectures()['M1'], proposal, sample['messages'],
                                 policy, guard, [])
        output = reviewer.trace[-1].output
        record.update(status='complete', result=result.model_dump(mode='json'), trace_output=output)
        record['used_retry'] = (row.metrics()['agent_calls'] > 1
                                or any(f['attempt'] == 2 for f in output['failures']))
    except Exception as exc:
        # Preserve unexpected transport/program failures too. Never retry the sample.
        record.update(status='error', error_type=type(exc).__name__)
        if reviewer.trace:
            record['trace_output'] = reviewer.trace[-1].output
        record['used_retry'] = row.metrics()['agent_calls'] > 1
    return {**record, 'metrics': row.metrics()}


def run_once(directory, manifest, schedule, policy, tools, client_factory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        freeze = directory / 'manifest.json'
        if freeze.exists() and json.loads(freeze.read_text()) != manifest:
            raise ValueError('Frozen manifest mismatch')
        journal = directory / 'calls.jsonl'
        # Even an empty journal means the single attempt started (e.g. all guards skipped).
        if journal.exists() or any((directory / (s['id'] + '.json')).exists() for s in schedule):
            raise ValueError('Journal or sample claims already exist; this minitest runs once')
        if not freeze.exists():
            dump(freeze, manifest)
        with journal.open('x'):
            pass
        budget = Budget(manifest['max_calls'], MAX_USD, journal)
        with client_factory() as client:
            for sample in schedule:
                target = directory / (sample['id'] + '.json')
                if not claim_sample(target, sample['id'], 'M1'):
                    raise ValueError(f"Duplicate sample claim: {sample['id']}")
                row = RowTransport(sample['id'], LIMITS, budget, client)
                record = review_sample(sample, policy, tools, row)
                dump(target, record)
                print(sample['id'], record['status'], record['result'], flush=True)
                result = record['result'] or {}
                if (result.get('fallback_reason') == 'BudgetExhausted'
                        or row.failure_status == 'budget_exhausted'
                        or budget.calls >= budget.max_calls or budget.committed >= budget.max_usd):
                    break


def summarize(directory):
    """Read only frozen inputs and receipts; never re-sample or repair an interrupted run."""
    manifest = json.loads((directory / 'manifest.json').read_text())
    qualified, conflicts, failures, model_revisions, retries, skipped, missing = [], [], [], [], [], [], []
    failure_stages = Counter()
    failure_events = Counter()
    reviewed = 0
    reviewed_false = 0
    block_hit = False
    for sample in manifest['schedule']:
        path = directory / (sample['id'] + '.json')
        record = json.loads(path.read_text()) if path.exists() else {'status': 'not_run'}
        if record.get('status') == 'guard_not_allowed':
            skipped.append({'id': sample['id'], 'guard_result': record['guard_result']})
            continue
        if record.get('used_retry'):
            retries.append(sample['id'])
        if not record.get('result'):
            missing.append({'id': sample['id'], 'status': record['status'],
                            'error_type': record.get('error_type')})
            continue
        result = ReviewResult.model_validate(record['result'])
        call = StepProposal.model_validate(sample['proposal']).calls[0]
        reviewed += 1
        reviewed_false += sample['group'] == 'false-block'
        if any(fulfills(request, call) for request in result.requests):
            qualified.append(sample['id'])
        details = {'id': sample['id'], 'requests': record['result']['requests'], 'issue': result.issue}
        if result.blocking_reason == 'order_dependency_conflict':
            if sample['group'] == 'false-block':
                conflicts.append(details)
            else:
                block_hit = True
        if result.fallback_reason:
            events = (record.get('trace_output') or {}).get('failures', [])
            stages = [e['phase'] for e in events] or ['unknown']
            failure_stages.update(set(stages))
            failure_events.update(stages)
            failures.append({'id': sample['id'], 'fallback_reason': result.fallback_reason,
                             'failures': events})
        elif result.verdict == 'revise' and not result.blocking_reason:
            model_revisions.append(details)
    reservations, settlements = call_receipts(directory / 'calls.jsonl')
    if set(settlements) - set(reservations):
        raise ValueError('Settlement without reservation')
    journal = directory / 'calls.jsonl'
    partial_tail = journal.exists() and bool(journal.stat().st_size) and not journal.read_bytes().endswith(b'\n')
    scheduled = len(manifest['schedule'])
    false_n = sum(s['group'] == 'false-block' for s in manifest['schedule'])
    actual = sum((Decimal(s['actual_usd']) for s in settlements.values()), Decimal(0))
    return {'run': directory.name, 'manifest': manifest,
            'denominator_note': 'Rates use all scheduled samples; skipped, failed and missing reviews remain visible. '
                                'An incomplete run is not a completed false-conflict measurement.',
            'sample_count': scheduled, 'reviewed_count': reviewed,
            'complete': not missing and not skipped and reviewed == scheduled,
            'checklist_qualified_count': len(qualified), 'checklist_qualified_ids': qualified,
            'checklist_qualified_rate': len(qualified) / scheduled if scheduled else None,
            'false_conflict_count': len(conflicts), 'false_conflicts': conflicts,
            'false_conflict_denominator': false_n, 'false_block_reviewed_count': reviewed_false,
            'false_conflict_rate': len(conflicts) / false_n if false_n else None,
            'false_conflict_wilson_95': wilson(len(conflicts), false_n),
            'review_failure_count': len(failures), 'review_failures': failures,
            'review_failure_samples_by_stage': dict(failure_stages),
            'review_failure_events_by_stage': dict(failure_events),
            'retry_count': len(retries), 'retry_ids': retries,
            'model_revise_count': len(model_revisions), 'model_revisions': model_revisions,
            'block_group_97_hit': block_hit, 'guard_not_allowed': skipped,
            'missing_or_failed_samples': missing, 'total_actual_usd': str(actual),
            'attempted_calls': len(reservations), 'unsettled_calls': len(set(reservations) - set(settlements)),
            'journal_incomplete_tail': partial_tail}


def live_client():
    from openai import OpenAI
    key = json.loads((ROOT / '.runtime/model-secret.json').read_text())['api_key']
    return OpenAI(api_key=key, base_url='https://api.openai.com/v1', max_retries=0, timeout=60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--live', action='store_true')
    modes.add_argument('--summarize', action='store_true')
    parser.add_argument('--run', default='run-v1')
    args = parser.parse_args(argv)
    if not args.run or Path(args.run).name != args.run or args.run in {'.', '..'}:
        parser.error('--run must be a single directory name')
    directory = ROOT / '.runtime/review-v2-minitest' / args.run
    if args.summarize:
        report = summarize(directory)
        dump(REPORT, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    schedule = samples()
    policy, tools = official_inputs()
    manifest = manifest_for(schedule, policy, live=args.live)
    if not args.live:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    run_once(directory, manifest, schedule, policy, tools, live_client)


if __name__ == '__main__':
    main()
