#!/usr/bin/env python3
"""Offline v3.1 scheduling and saved checklist replay; never reconstruct missing model text."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from aftercare.agent_kernel.contracts import VERSION, StepProposal
from aftercare.agent_kernel.dependencies import conflict
from aftercare.agent_kernel.review import public_view, validate_requests
from replay_amount_guard import public
from replay_expert_staleness import statistics


def load(path, sources, root):
    raw = path.read_bytes()
    sources[str(path.relative_to(root))] = sha256(raw).hexdigest()
    return json.loads(raw)


def revalidate(output, context, proposal):
    sources = {m['id']: m for m in public_view(context)}
    checked, dropped = validate_requests(output['requests'], sources)
    historical = output.get('dropped_requests', [])
    return {
        'saved_entries': len(output['requests']), 'revalidated_entries': len(checked),
        'saved_entries_dropped': dropped, 'historical_dropped_entries': historical,
        'full_inventory_dropped_after': None if historical else len(dropped),
        'missing_raw_entries': len(historical),
        'dependency_before': output.get('blocking_reason'),
        'dependency_after_on_saved_entries': conflict(proposal, checked),
    }


def development(root, sources):
    directory = root / '.runtime/kernel-v3-dev/run-v1'
    manifest = load(directory / 'manifest.json', sources, root)
    rows = []
    for sample in manifest['schedule']:
        task = sample['task_id']
        key = f'{task}-M2'
        trajectory = load(directory / 'official' / f'{key}.json', sources, root)
        receipt = load(directory / 'rows' / f'{key}.json', sources, root)
        messages = trajectory['messages']
        experts = [e for e in trajectory['private_trace']
                   if e['stage'] in {'expert_fact_expert', 'expert_policy_expert'}]
        assert all(e['output']['reads'] == 0 for e in experts)
        attempted = sum(receipt['calls_by_actor'].get(role, 0) for role in ('fact_expert', 'policy_expert'))
        # The incomplete expert call on 54 ended before an assistant message was produced.
        interrupted = attempted - len(experts)
        assert interrupted in (0, 1)
        if interrupted:
            assert receipt['status'] == 'error' and receipt['error_type'] == 'RuntimeError'
        scheduling_messages = [*messages, *([{'role': 'assistant', 'content': None}] if interrupted else [])]
        before = statistics(scheduling_messages, task=task, arm='M2', rebase_writes=False)
        after = statistics(scheduling_messages, task=task, arm='M2')
        reviews = []
        proposal = None
        for index, event in enumerate(trajectory['private_trace']):
            if event['stage'] in {'executor_step', 'executor_revision', 'single_tool_normalization'}:
                proposal = StepProposal.model_validate(event['output'])
            if event['stage'] != 'proposal_review':
                continue
            assert proposal and proposal.calls, 'Expected a recorded write review in development traces'
            indices = [i for i, m in enumerate(messages) if any(
                c['id'] == proposal.calls[0].id for c in m.get('tool_calls') or [])]
            assert len(indices) == 1, 'Cannot align saved review with its exact public prefix'
            context = [public(m) for m in messages[:indices[0]]]
            reviews.append({'private_trace_index': index, 'message_index': indices[0],
                            **revalidate(event['output'], context, proposal)})
        missing = sum(r['missing_raw_entries'] for r in reviews)
        retained_drops = sum(len(r['saved_entries_dropped']) for r in reviews)
        rows.append({'task': task, 'recorded_consults': attempted,
                     'v3_replay_consults': before['new_consults'], 'v3_1_replay_consults': after['new_consults'],
                     'interrupted_consult_boundary_appended': bool(interrupted),
                     'replay': after, 'reviews': reviews,
                     'historical_dropped_entries': missing, 'saved_entries_dropped_after': retained_drops,
                     'full_inventory_dropped_after': None if missing else retained_drops})
    return rows


def minitest(root, sources):
    directory = root / '.runtime/review-v2-minitest/run-v1'
    manifest = load(directory / 'manifest.json', sources, root)
    rows = []
    for sample in manifest['schedule']:
        trajectory = load(root / sample['source'], sources, root)
        assert sources[sample['source']] == sample['source_sha256']
        saved = load(directory / (sample['id'] + '.json'), sources, root)
        assert saved['status'] == 'complete'
        context = [public(m) for m in trajectory['messages'][:sample['message_index']]]
        proposal = StepProposal.model_validate(sample['proposal'])
        rows.append({'id': sample['id'], 'group': sample['group'],
                     **revalidate(saved['trace_output'], context, proposal)})
    false = [r for r in rows if r['group'] == 'false-block']
    control = [r for r in rows if r['group'] == 'block']
    return {'samples': len(rows), 'success_path_samples': len(false),
            'success_path_conflicts_before': sum(r['dependency_before'] == 'order_dependency_conflict' for r in false),
            'success_path_conflicts_after_on_saved_entries': sum(
                r['dependency_after_on_saved_entries'] == 'order_dependency_conflict' for r in false),
            'task_97_control_blocked_before': all(r['dependency_before'] == 'order_dependency_conflict' for r in control),
            'task_97_control_blocked_after_on_saved_entries': bool(control) and all(
                r['dependency_after_on_saved_entries'] == 'order_dependency_conflict' for r in control),
            'missing_raw_entries': sum(r['missing_raw_entries'] for r in rows),
            'saved_entries_dropped_after': sum(len(r['saved_entries_dropped']) for r in rows), 'rows': rows}


def replay(root=ROOT):
    sources = {}
    dev = development(root, sources)
    mini = minitest(root, sources)
    return {'mode': 'offline; no model, network or tool execution', 'kernel_version': VERSION,
            'development': dev, 'review_minitest': mini, 'sources': sources,
            'limitations': [
                'Only recorded messages are replayed. No new answers, reads, actions or rewards are predicted.',
                'Task 54 includes the interrupted consultation boundary; no continuation is invented.',
                'Frozen files contain validated review outputs and drop diagnostics, not raw model reviews. '
                'Dropped entry bodies are absent. Only retained entries can be revalidated; '
                'full_inventory_dropped_after is null where originals are missing. '
                'Do not interpret zero retained-entry drops as zero drops on the original complete inventory.',
                'The 34-review conflict regression applies to saved surviving inventories; '
                '29 historically dropped entries cannot be restored from these files.',
                'The six-candidate instruction and specialist reads remain unverified in live operation.']}


def main():
    report = replay()
    target = ROOT / 'evidence/architecture_refactor/kernel_v3_1_dev_replay.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'development': [{k: v for k, v in r.items() if k not in {'reviews', 'replay'}}
                                      for r in report['development']],
                      'review_minitest': {k: v for k, v in report['review_minitest'].items() if k != 'rows'}},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
