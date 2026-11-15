"""Offline fixtures only; no provider or campaign execution."""
from hashlib import sha256
import json
import socket
import subprocess
import sys

import pytest

import analyze_kernel_v3_dev as analysis
import select_kernel_v3_dev_tasks as selection
from aftercare.agent_kernel.contracts import ExpertResult, GuardResult, ReviewResult, StepProposal, TraceEvent


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)



def runtime_snapshot():
    directory = selection.ROOT / '.runtime/kernel-v3-dev'
    return {str(p.relative_to(directory)): sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def task(ident, constrained=True, names=None):
    return {'id': str(ident), 'user_scenario': {'instructions': 'SCENARIO_SECRET ' + ('cheapest option' if constrained else 'please help')},
            'evaluation_criteria': {'actions': [{'name': name, 'arguments': {'secret': 'REFERENCE_SECRET'}} for name in
                (names or ['cancel_pending_order', 'modify_user_address'])]}}


def test_selection_numeric_order_disjoint_and_deterministic():
    tasks = [task(n) for n in (100, 44, 20, 3, 2, 10)] + [task(n, False) for n in (90, 11, 4)]
    tasks += [task(1, False, ['cancel_pending_order'] * 2), task(5, True, ['get_user_details'])]
    tasks += [task(0)]  # Not in train.
    split = {'train': [t['id'] for t in reversed(tasks) if t['id'] != '0']}
    first = selection.select(tasks, split, {})
    assert first == selection.select(list(reversed(tasks)), split, {})
    assert first['task_ids'] == ['2', '3', '10', '20', '4', '11']
    assert [t['group'] for t in first['selected']] == ['A'] * 4 + ['B'] * 2
    assert len(set(first['task_ids'])) == 6 and '44' not in first['task_ids']
    assert set(first['task_ids']) <= set(split['train'])
    assert first['group_candidate_counts'] == {'A': 5, 'B': 3}
    assert all(bool(t['matched_conditions']) == (t['group'] == 'A') for t in first['selected'])
    assert 'SCENARIO_SECRET' not in json.dumps(first)
    assert 'REFERENCE_SECRET' not in json.dumps(first)
    assert first['remaining_available_train_tasks'] == len(split['train']) - 7


def test_real_selection_hashes_and_no_runtime_writes(tmp_path, monkeypatch):
    frozen_before = runtime_snapshot()
    one = selection.load_selection()
    assert one == selection.load_selection()
    assert one['task_ids'] == ['29', '41', '54', '96', '16', '22']
    assert one['group_candidate_counts'] == {'A': 6, 'B': 15}
    assert one['candidate_count'] == 73
    assert one['remaining_available_train_tasks'] == 67
    for name, digest in one['dataset_sha256'].items():
        assert sha256((selection.SOURCE / 'data/tau2/domains/retail' / name).read_bytes()).hexdigest() == digest
    output = tmp_path / 'evidence/tasks.json'
    monkeypatch.setattr(selection, 'OUTPUT', output)
    monkeypatch.setattr(sys, 'argv', ['select_kernel_v3_dev_tasks.py'])
    selection.main()
    assert json.loads(output.read_text()) == one
    assert runtime_snapshot() == frozen_before
    assert not (tmp_path / '.runtime').exists()


def test_hash_mismatch_rejected_before_json_parse(tmp_path, monkeypatch):
    retail = tmp_path / 'data/tau2/domains/retail'
    retail.mkdir(parents=True)
    (retail / 'tasks.json').write_text('not JSON')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'revision': 'pinned', 'files': {'tasks.json': {'sha256': 'incorrect'}}}))
    monkeypatch.setattr(selection.subprocess, 'check_output', lambda args, **kw: 'pinned' if 'rev-parse' in args else '')
    with pytest.raises(ValueError, match='hash mismatch'):
        selection.load_selection(tmp_path, manifest)


def event(stage, output, reason=None):
    if hasattr(output, 'model_dump'):
        output = output.model_dump(mode='json')
    role = 'fact_expert' if stage == 'expert_fact_expert' else 'reviewer' if stage == 'proposal_review' else 'executor'
    return TraceEvent(role=role, stage=stage, input_sha256='0' * 64, output=output, reason=reason).model_dump(mode='json')


def propose(text='hello', stage='executor_step'):
    return event(stage, StepProposal(text=text))


def guard(rule=None, *, stage='guard', soft=True):
    feedback = ('Uncheckable dollar amount(s): 99.00. Verifiable amounts listed in this reply (distinct): '
                '10.00; their sum is 10.00. Use the calculate tool to recompute the amount(s), or correct the reply.') if rule == 'amount_arithmetic' else 'Please authenticate' if rule else ''
    return event(stage, GuardResult(allowed=not rule, soft=bool(rule) and soft, rule_ids=[rule] if rule else [], feedback=feedback))


def soft_event(rule):
    return event('guard_soft_unresolved', {'rule_ids': [rule], 'source_stage': 'guard',
                 'feedback': 'unresolved', 'guard_result': GuardResult(allowed=True).model_dump(mode='json')})


def review(**kw):
    return event('proposal_review', {**ReviewResult(**{'verdict': 'allow', **kw}).model_dump(mode='json'), 'failures': [], 'dropped_requests': []})


def expert(refresh=False):
    output = {**ExpertResult().model_dump(mode='json'), 'reads': 1,
              'read_log': [{'tool': 'get_product_details', 'arguments': {'product_id': 'p'},
                           'status': s, 'result': 'fixture'} for s in ('executed', 'rejected', 'error', 'limit')]}
    if refresh:
        output['refresh_of'] = '0' * 64
        output['changed'] = ['product:p']
    return event('expert_fact_expert', output, 'dropped_ungrounded:2;price_corrected:3')


def fixture_run(directory, *, exceptional=True):
    (directory / 'rows').mkdir(parents=True)
    (directory / 'official').mkdir()
    schedule = [{'task_id': str(n), 'architecture': 'M2', 'seed': 42 + n} for n in range(6)]
    (directory / 'manifest.json').write_text(json.dumps({'schedule': schedule}))
    traces = [
        [expert(), expert(True), event('expert_stale_dropped', {'signal': 'constrained_choice', 'changed': ['product:p']}),
         propose('Total $99'), guard('amount_arithmetic'), propose('Total $10'), guard()],
        [propose(), guard(), review(fallback_reason='ValueError'),
         event('review_resolution', {'status': 'review_unavailable'})],
        [propose(), guard('authentication', soft=False)],
        [propose('Total $99'), guard('amount_arithmetic'), propose('Total $99'), guard('amount_arithmetic'), soft_event('amount_arithmetic')],
        [propose(), guard('authentication_username'), propose('Looking up your account'), guard()],
        [propose(), guard(), review(verdict='revise', issue='conflict', next_step='clarify', blocking_reason='order_dependency_conflict'),
         propose('clarify', 'executor_revision'), guard(stage='post_review_guard'),
         event('review_resolution', {'status': 'proposal_changed_not_semantically_verified'})],
    ]
    traces[1][2]['output']['failures'] = [{'attempt': 1, 'phase': 'parse', 'type': 'ValueError', 'detail': 'bad JSON'}]
    traces[1][2]['output']['dropped_requests'] = [{'attempt': 1, 'reason': 'unsupported'}]
    journal = []
    for n, item in enumerate(schedule):
        key = f'{n}-M2'
        row = {**item, 'status': 'completed', 'official_reward': 0.5, 'termination_reason': 'user_stop', 'duration_seconds': 10}
        if n == 2 and exceptional:
            row.update(status='error', error_type='GuardRejected', official_reward=None)
            del row['termination_reason']
        (directory / 'rows' / f'{key}.json').write_text(json.dumps(row))
        messages = [{'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'a', 'name': 'find_user_id_by_email', 'arguments': {'email': 'fixture@example.test'}}]},
            {'role': 'tool', 'id': 'a', 'content': 'fixture_user', 'error': False}]
        (directory / 'official' / f'{key}.json').write_text(json.dumps({'messages': messages, 'private_trace': traces[n]}))
        for k, actor in enumerate(('executor', 'reviewer', 'fact_expert', 'user', 'judge')):
            ident = n * 5 + k + 1
            journal.extend([{'call_id': ident, 'row': key, 'actor': actor, 'reserve_usd': '.10'},
                            {'call_id': ident, 'actual_usd': '.01'}])
    (directory / 'calls.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in journal))
    return directory


def test_analysis_counts_checks_and_no_runtime_writes(tmp_path, monkeypatch):
    frozen_before = runtime_snapshot()
    directory = fixture_run(tmp_path / 'run')
    report = analysis.analyse(directory)
    totals, checks = report['totals'], report['checks']
    assert set(checks) == {'all_finished', 'expert_reads_used', 'expert_consults_bounded', 'amount_rule', 'program_blocks', 'review_fallback', 'auth_reminder'}
    assert {k: v['passed'] for k, v in checks.items()} == {
        'all_finished': False, 'expert_reads_used': True, 'expert_consults_bounded': True,
        'amount_rule': True, 'program_blocks': None, 'review_fallback': True, 'auth_reminder': True}
    assert totals['experts']['consults'] == 2
    assert totals['experts']['refreshes'] == totals['experts']['stale_dropped'] == 1
    assert totals['experts']['read_statuses'] == dict.fromkeys(analysis.READ_STATUSES, 2)
    assert totals['experts']['price_corrected'] == 6 and totals['experts']['dropped_ungrounded'] == 4
    assert totals['experts']['mean_consults_among_consulted_tasks'] == 2
    assert report['per_task'][0]['experts']['consultations'][0]['read_log_count'] == 4
    assert report['per_task'][0]['experts']['consultations'][0]['signal'] == 'unknown'
    assert totals['amount_rule'] == {'triggered': 3, 'corrected': 1, 'soft_unresolved': 2, 'other_or_unknown': 0}
    amount_event = report['per_task'][0]['amount_rule']['events'][0]
    assert amount_event['proposal_changed'] is True and amount_event['uncheckable_amounts'] == ['99.00']
    assert amount_event['reply_amounts'] == ['99.00']
    assert totals['review']['count'] == 2 and totals['review']['fallback_count'] == 1
    assert totals['review']['failures'] == totals['review']['dropped_requests'] == 1
    assert totals['review']['executor_revision'] == totals['review']['order_dependency_conflict'] == 1
    assert totals['auth_reminder']['corrected'] == 1
    assert totals['program_blocks']['status'] == 'needs_manual_review'
    assert len(totals['program_blocks']['events']) == 2
    conflict = totals['program_blocks']['events'][1]
    assert conflict['requests'] == [] and conflict['proposal']['text'] == 'hello'
    assert report['per_task'][2]['outcome']['category'] == 'guard_rejected'
    assert report['per_task'][2]['transfer_tool_calls'] == 0
    assert totals['billing']['calls'] == 30 and totals['billing']['unsettled_calls'] == 0
    assert totals['billing']['total_cost_usd'] == pytest.approx(.30)
    assert totals['billing']['calls_by_actor']['reviewer'] == 6
    assert totals['duration_seconds_sum'] == 60
    output = tmp_path / 'evidence/check.json'
    monkeypatch.setattr(analysis, 'OUTPUT', output)
    monkeypatch.setattr(sys, 'argv', ['analyze_kernel_v3_dev.py', '--directory', str(directory)])
    analysis.main()
    assert json.loads(output.read_text()) == report
    assert runtime_snapshot() == frozen_before
    assert not (tmp_path / '.runtime').exists()


def replace_json(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_checks_fail_and_unsettled_cost_is_not_zero(tmp_path):
    directory = fixture_run(tmp_path, exceptional=False)
    assert analysis.analyse(directory)['checks']['all_finished']['passed'] is True
    replace_json(directory / 'official/0-M2.json', lambda d: d.update(private_trace=[expert()] * 4))
    replace_json(directory / 'official/1-M2.json', lambda d: d['private_trace'].extend([review(fallback_reason='TimeoutError')]))
    replace_json(directory / 'official/4-M2.json', lambda d: d.update(messages=[]))
    journal = directory / 'calls.jsonl'
    journal.write_text('\n'.join(journal.read_text().splitlines()[:-1]) + '\n')
    report = analysis.analyse(directory)
    assert report['checks']['expert_consults_bounded']['passed'] is False
    assert report['checks']['review_fallback']['passed'] is False
    assert report['checks']['auth_reminder']['passed'] is False
    assert report['checks']['amount_rule']['passed'] is False
    assert report['checks']['all_finished']['passed'] is False
    assert report['totals']['billing']['unsettled_calls'] == 1
    assert report['totals']['billing']['total_cost_usd'] == 'unknown'
    assert report['totals']['billing']['known_total_cost_usd'] == pytest.approx(.29)


@pytest.mark.parametrize('field', ['private_trace', 'messages'])
def test_missing_trace_evidence_unknown(tmp_path, field):
    directory = fixture_run(tmp_path)
    replace_json(directory / 'official/0-M2.json', lambda d: d.pop(field))
    report = analysis.analyse(directory)
    assert report['per_task'][0]['unknown_fields']
    if field == 'private_trace':
        assert report['checks']['expert_reads_used']['passed'] == 'unknown'
        assert report['checks']['expert_consults_bounded']['passed'] == 'unknown'
    else:
        assert report['per_task'][0]['authentication_completed'] == 'unknown'


def test_missing_event_field_and_partial_billing_are_unknown(tmp_path):
    directory = fixture_run(tmp_path)
    replace_json(directory / 'official/0-M2.json', lambda d: d['private_trace'][0]['output'].pop('read_log'))
    report = analysis.analyse(directory)
    assert report['checks']['expert_reads_used']['passed'] == 'unknown'
    journal = directory / 'calls.jsonl'
    journal.write_text(journal.read_text().rstrip('\n'))
    report = analysis.analyse(directory)
    assert report['totals']['billing']['unsettled_calls'] == 'unknown'
    assert report['unknown_fields']


def test_no_cross_step_correction_and_post_review_soft_acceptance():
    events = [propose('$99'), guard('amount_arithmetic', stage='post_review_guard'), soft_event('amount_arithmetic'), propose('$10'), guard()]
    result = analysis.trace_metrics({'private_trace': events}, 'x', analysis.Evidence())
    assert result['amount_rule']['corrected'] == 0
    assert result['amount_rule']['soft_unresolved'] == 1


def test_auth_soft_unresolved_and_hard_check_after_soft_acceptance():
    soft = soft_event('authentication_username')
    soft['output']['guard_result'] = GuardResult(allowed=False, rule_ids=['single_user'], feedback='blocked').model_dump(mode='json')
    events = [propose(), guard('authentication_username'), propose(), guard('authentication_username'), soft]
    result = analysis.trace_metrics({'private_trace': events}, 'x', analysis.Evidence())
    assert result['auth_reminder']['triggered'] == result['auth_reminder']['soft_unresolved'] == 2
    assert result['auth_reminder']['corrected'] == 0
    assert result['program_blocks']['events'][0]['rule'] == 'single_user'


def test_missing_read_status_is_unknown(tmp_path):
    directory = fixture_run(tmp_path)
    def change(data):
        for event in data['private_trace'][:2]:
            event['output']['read_log'][0].pop('status')
    replace_json(directory / 'official/0-M2.json', change)
    assert analysis.analyse(directory)['checks']['expert_reads_used']['passed'] == 'unknown'


def test_missing_schedule_fails_without_creating_anything(tmp_path):
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError, match='population'):
        analysis.analyse(tmp_path)
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize('status,error,termination,category', [
    ('budget_exhausted', 'BudgetExhausted', None, 'budget_or_call_limit'),
    ('error', 'GuardRejected', None, 'guard_rejected'),
    ('unknown', 'ProviderResultUnknown', None, 'other_exception_or_limit'),
    ('completed', None, 'max_steps', 'other_exception_or_limit'),
    ('completed', None, 'user_stop', 'normal'),
    ('completed', None, None, 'unknown'),
])
def test_completion_categories(status, error, termination, category):
    row = {'status': status, 'error_type': error, 'termination_reason': termination,
           'official_reward': None, 'duration_seconds': 1}
    assert analysis.outcome(row, analysis.Evidence())['category'] == category


def test_wiring_dry_run_is_offline_and_records_version(tmp_path):
    frozen_before = runtime_snapshot()
    argv = [str(selection.ROOT / 'scripts/run_tau_official_campaign.py'), 'run', '--scope', 'wiring',
            '--architectures', 'M2', '--task-ids', ','.join(selection.load_selection()['task_ids']),
            '--directory', str(tmp_path / 'absent'), '--max-usd', '2', '--max-calls', '4506',
            '--agent-calls', '640', '--user-calls', '110', '--concurrency', '6',
            '--run-timeout', '900', '--request-timeout', '120']
    code = '''import os, runpy, socket, sys
def forbidden(*a, **kw): raise AssertionError('Network or runtime write prohibited')
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = forbidden
runtime = sys.argv[1]
def audit(event, args):
    if event == 'open' and isinstance(args[0], str) and os.path.realpath(args[0]).startswith(runtime + '/'):
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND): forbidden()
    if event == 'os.mkdir' and os.path.realpath(args[0]).startswith(runtime + '/'): forbidden()
sys.addaudithook(audit)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    process = subprocess.run([sys.executable, '-B', '-c', code, str(selection.ROOT / '.runtime'), *argv],
                             check=True, capture_output=True, text=True)
    manifest = json.loads(process.stdout)
    assert manifest['task_ids'] == selection.load_selection()['task_ids']
    assert len(manifest['schedule']) == 6
    assert {row['architecture'] for row in manifest['schedule']} == {'M2'}
    assert manifest['kernel_version'] == 'aftercare-kernel-v3.1'
    assert manifest['budget']['max_usd'] == 2
    assert manifest['kernel_sha256']
    assert not (tmp_path / 'absent').exists()
    assert runtime_snapshot() == frozen_before
