#!/usr/bin/env python3
"""Read frozen E3/E4 traces; replay guards and recovery with a deterministic fake model.

No tau/model client imports, network, tool execution or writes to source trajectories.
The sole output is created exclusively; existing evidence is never overwritten.
"""
from __future__ import annotations

import ast
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from aftercare.agent_kernel.contracts import VERSION, StepProposal, ToolCall, ToolDefinition, ToolKind, architectures
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected
from aftercare.agent_kernel.retail_guards import AUTH, RetailGuards

DIRECTORIES = tuple(f'.runtime/{experiment}/{run}' for experiment in ('kernel-e3', 'kernel-e4')
                    for run in ('run-v1', 'run-v1.repair'))
TARGET = Path('evidence/architecture_refactor/guard_recovery_replay.json')
FALLBACK = 'I cannot complete this step right now. Please check the order or item information and clarify your request.'


def load(path, sources, root):
    raw = path.read_bytes()
    sources[str(path.relative_to(root))] = sha256(raw).hexdigest()
    return json.loads(raw)


def tool_definitions(root, sources):
    """Parse public tool declarations, without importing the official SDK or its clients."""
    path = root / '.runtime/tau-official-source/src/tau2/domains/retail/tools.py'
    raw = path.read_bytes()
    sources[str(path.relative_to(root))] = sha256(raw).hexdigest()
    tools = []
    for node in ast.walk(ast.parse(raw)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                    and decorator.func.id=='is_tool'):
                declared = decorator.args[0].attr
                kind = (ToolKind.AUTH if node.name in AUTH else ToolKind.HANDOFF
                        if node.name=='transfer_to_human_agents' else ToolKind.WRITE
                        if declared=='WRITE' else ToolKind.READ)
                tools.append(ToolDefinition(name=node.name, kind=kind))
    if not tools or len({t.name for t in tools})!=len(tools):
        raise ValueError('Cannot recover a unique retail tool catalog')
    return tuple(tools)


def public(message):
    # Exact public_messages projection used by tau_official_agents, including JSON argument strings.
    row = {'role':message['role'], 'content':message.get('content')}
    if message['role']=='tool':
        row.update(tool_call_id=message['id'], error=message.get('error', False))
    if message.get('tool_calls'):
        row['tool_calls'] = [{'id':c['id'], 'type':'function', 'function':{
            'name':c['name'], 'arguments':json.dumps(c['arguments'])}} for c in message['tool_calls']]
    return row


def public_proposal(message):
    return StepProposal(text=message.get('content') or '', calls=tuple(
        ToolCall(id=c['id'], name=c['name'], arguments=c['arguments']) for c in message.get('tool_calls') or []))


def group_steps(trace):
    """Recover v3.1 step boundaries from its control flow, including private retries.

    A rejected guard followed immediately by executor_step is the one retry.
    Soft acceptance, review and its revisions stay in the same outer step. Expert
    events do not produce public messages and do not change a guard's prefix.
    """
    groups = []
    group = None
    retry = False
    proposal = None
    for index, event in enumerate(trace):
        stage = event['stage']
        if stage=='executor_step':
            if not retry:
                group = {'start':index, 'checks':[], 'executor_attempts':0}
                groups.append(group)
            if group is None or group['executor_attempts']>=2:
                raise ValueError(f'Unexpected retry boundary at trace {index}')
            group['executor_attempts'] += 1
            proposal = StepProposal.model_validate(event['output'])
            retry = False
        elif stage in {'executor_revision', 'single_tool_normalization'}:
            if group is None:
                raise ValueError(f'Proposal without outer step at trace {index}')
            proposal = StepProposal.model_validate(event['output'])
        elif stage in {'guard', 'post_review_guard'}:
            if group is None or proposal is None:
                raise ValueError(f'Guard without proposal at trace {index}')
            # v3.1 resolve_review normalizes without a separate trace event.
            if stage=='post_review_guard':
                proposal = RetailGuards().normalize(proposal)
            group['checks'].append({'trace_index':index, 'stage':stage,
                                    'proposal':proposal, 'old':event['output']})
            retry = stage=='guard' and not event['output']['allowed']
        elif stage=='guard_soft_unresolved':
            retry = False
        if group is not None and proposal is not None:
            group['proposal'] = proposal
    return groups


def align(trajectory):
    """Require every emitted assistant action to match its private final proposal.

    The initial ungenerated greeting is retained in prefixes. A single final
    unreturned step uses the complete saved trajectory (no rejected action is
    added). On any ambiguity the whole trajectory is marked uncovered.
    """
    messages = trajectory['messages']
    groups = group_steps(trajectory.get('private_trace', []))
    first_incoming = next((i for i, m in enumerate(messages) if m['role'] in {'user','tool'}), len(messages))
    emitted = [i for i, m in enumerate(messages) if i>first_incoming and m['role']=='assistant']
    if len(groups) not in {len(emitted), len(emitted)+1}:
        raise ValueError(f'{len(groups)} private steps vs {len(emitted)} public replies')
    for offset, group in enumerate(groups):
        boundary = emitted[offset] if offset<len(emitted) else len(messages)
        if offset<len(emitted) and group['proposal']!=public_proposal(messages[boundary]):
            raise ValueError(f'Final proposal mismatch at step {offset}, message {boundary}')
        group['message_index'] = boundary
        group['messages'] = [public(m) for m in messages[:boundary]]
    return groups


def recover(check, messages, tools):
    proposal = check['proposal']
    checked = RetailGuards().check(messages, proposal, tools)
    stages = []
    def generate(**kwargs):
        stages.append(kwargs['stage'])
        return StepProposal(text=FALLBACK) if kwargs['stage']=='executor_guard_fallback' else proposal
    # Isolate guard recovery. No invented reviewer or specialist decisions.
    kernel = ExecutionKernel(architectures()['S1'], SimpleNamespace(generate=generate), guard=RetailGuards())
    result = {'original_proposal':proposal.model_dump(mode='json'),
              'new_guard':checked.model_dump(mode='json')}
    try:
        reply = kernel.step(messages, tools, '')
        reads = [e for e in kernel.trace if e.stage=='guard_prerequisite_read']
        if reads:
            category = 'prerequisite_read'
            result['prerequisite'] = reads[0].output['prerequisite']
        elif 'executor_guard_fallback' in stages:
            category = 'text_fallback'
        else:
            # Authentication follow-up fix accepts the original text; no fallback was called.
            category = 'original_reply_allowed'
        result.update(category=category, returned_proposal=reply.model_dump(mode='json'))
    except GuardRejected as exc:
        result.update(category='still_terminated', error=str(exc))
    result['specific_feedback'] = (checked.feedback if not checked.allowed and checked.prerequisite is None
                                   and checked.feedback!=check['old'].get('feedback') else None)
    result['model_stages'] = stages
    result['fallback_count'] = kernel.guard_fallback_count
    return result


def replay(root=ROOT):
    sources = {}
    tools = tool_definitions(root, sources)
    recoveries, mismatches, uncovered, audits = [], [], [], []
    totals = Counter()
    for directory in DIRECTORIES:
        base = root / directory
        for row_path in sorted((base / 'rows').glob('*.json')):
            row = load(row_path, sources, root)
            key = row_path.stem
            path = base / 'official' / row_path.name
            rejected = row.get('error_type')=='GuardRejected'
            if not path.exists():
                uncovered.append({'directory':directory, 'key':key, 'reason':'Missing official trajectory',
                                  'guard_rejected':rejected})
                if rejected:
                    recoveries.append({'directory':directory, 'key':key, 'category':'still_terminated',
                                       'reason':'No exact prefix available; not replayed'})
                continue
            trajectory = load(path, sources, root)
            traces = trajectory.get('private_trace', [])
            count = sum(e['stage'] in {'guard','post_review_guard'} for e in traces)
            totals['trajectories'] += 1
            totals['recorded_judgments'] += count
            totals['recorded_allowed'] += sum(e['output']['allowed'] for e in traces
                                             if e['stage'] in {'guard','post_review_guard'})
            try:
                groups = align(trajectory)
            except ValueError as exc:
                uncovered.append({'directory':directory, 'key':key, 'reason':str(exc), 'judgments':count})
                if rejected:
                    recoveries.append({'directory':directory, 'key':key, 'category':'still_terminated',
                                       'reason':'No exact prefix available; not replayed'})
                continue
            for group in groups:
                for check in group['checks']:
                    new = RetailGuards().check(group['messages'], check['proposal'], tools)
                    old = check['old']
                    same = new.allowed==old['allowed']
                    totals['replayed_judgments'] += 1
                    totals['unchanged'] += same
                    totals['changed'] += not same
                    if old['allowed']:
                        totals['replayed_original_allowed'] += 1
                        totals['original_allowed_unchanged'] += same
                        totals['original_allowed_changed'] += not same
                        totals['original_allowed_to_hard_reject'] += not same and not new.soft
                        totals['original_allowed_to_soft_hint'] += not same and new.soft
                    entry = {'directory':directory, 'key':key, 'trace_index':check['trace_index'],
                             'stage':check['stage'], 'message_index':group['message_index'],
                             'prefix_sha256':sha256(json.dumps(group['messages'], sort_keys=True,
                                                              ensure_ascii=False).encode()).hexdigest(),
                             'old_allowed':old['allowed'], 'new_allowed':new.allowed,
                             'new_soft':new.soft,
                             'old_rules':old.get('rule_ids', []), 'new_rules':list(new.rule_ids),
                             'prerequisite':new.prerequisite.model_dump(mode='json') if new.prerequisite else None}
                    audits.append(entry)
                    if not same:
                        mismatches.append({**entry, 'new_feedback':new.feedback,
                                           'proposal':check['proposal'].model_dump(mode='json')})
            if rejected:
                if not groups or not groups[-1]['checks'] or groups[-1]['message_index']!=len(trajectory['messages']):
                    raise ValueError(f'{directory}/{key}: rejected terminal step not reconstructed')
                group = groups[-1]
                check = group['checks'][-1]
                if check['stage']!='guard' or check['old']['allowed'] or check['old'].get('soft'):
                    raise ValueError(f'{directory}/{key}: not an ordinary hard guard termination')
                recoveries.append({'directory':directory, 'key':key, 'trace_index':check['trace_index'],
                                   'message_index':group['message_index'], 'original_rules':check['old']['rule_ids'],
                                   **recover(check, group['messages'], tools)})
    categories = Counter(r['category'] for r in recoveries)
    totals['coverage'] = totals['replayed_judgments']/totals['recorded_judgments'] if totals['recorded_judgments'] else 0
    summary = {'guard_terminations':len(recoveries), 'categories':{k:categories[k] for k in (
        'prerequisite_read','text_fallback','still_terminated','original_reply_allowed')},
        'specific_feedback':sum(bool(r.get('specific_feedback')) for r in recoveries),
        'original_rules':dict(Counter(rule for r in recoveries for rule in r.get('original_rules', [])))}
    return {'kernel_version':VERSION, 'mode':'offline; fixed fake model; no network or tool execution',
            'summary':summary, 'judgments':dict(totals), 'mismatches':mismatches, 'uncovered':uncovered,
            'recoveries':recoveries, 'judgment_audit':audits, 'sources':sources,
            'method':[
                'Group private executor_step retries and executor_revision events using v3.1 control flow.',
                'Match every group final proposal exactly to the next public assistant message, including call IDs/arguments.',
                'Recheck all guard/post_review_guard events against the prefix before that public reply; retries share it.',
                'The unreturned terminal group uses the entire recorded public trajectory. Any alignment failure is uncovered.',
                'Tool names/kinds are parsed from public retail tool declarations with the official adapter AUTH/HANDOFF mapping.',
                'Recovery uses S1 to isolate the guard branch; specialists/reviewers are not replayed or fabricated.',
                'specific_feedback overlaps text_fallback; original_reply_allowed records the authentication false-positive fix '
                'without falsely claiming a fallback model call.'],
            'limitations':[
                'All 114 retail tasks have been seen. This is development-only verification, not an independent evaluation.',
                'No new prerequisite read is executed; no continuation, customer response, business success or score is predicted.',
                'The fixed English fallback is a mechanical fixture, not validation of model language/reason quality.',
                'Change 5 can turn a previously allowed authentication reply into a soft hint when an earlier lookup '
                'tried a different name combination; this conflicts with an absolute all-original-allows-unchanged target.',
                'confirm/ablate reject v3.2; reproduce frozen E3/E4 analysis on commit cab5c2a.']}


def write_report(report, target):
    with target.open('x') as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


def main():
    target = ROOT / TARGET
    if target.exists():
        raise SystemExit(f'Refusing to overwrite existing evidence: {target}')
    report = replay()
    write_report(report, target)
    print(json.dumps({'summary':report['summary'], 'judgments':report['judgments'],
                      'uncovered':report['uncovered']}, ensure_ascii=False, indent=2))


if __name__=='__main__':
    main()
