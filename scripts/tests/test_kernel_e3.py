"""E3 fixtures and CLI dry runs; no network, model run or runtime writes."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

import run_tau_official_campaign as runner
from analyze_kernel_campaign import wilson
from analyze_kernel_e3 import analyse
from kernel_e3_protocol import ARMS, EXCLUDED, LIMITS, population
from tau_official_summary import read_json, write_json


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('E3 tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def dry(directory, *, scope='confirm', names=','.join(ARMS), extra=()):
    argv = [str(runner.ROOT / 'scripts/run_tau_official_campaign.py'), 'run',
            '--scope', scope, '--directory', str(directory), '--architectures', names]
    for key, value in LIMITS.items():
        argv.extend(['--' + key.replace('_', '-'), str(value)])
    code = '''import runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('No network permitted')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    return subprocess.run([sys.executable, '-B', '-c', code, *argv, *extra],
                          capture_output=True, text=True)


@pytest.fixture(scope='module')
def manifest(tmp_path_factory):
    directory = tmp_path_factory.mktemp('e3-dry') / 'absent'
    process = dry(directory)
    assert process.returncode == 0, process.stderr
    again = dry(directory)
    assert again.returncode == 0, again.stderr
    assert process.stdout == again.stdout
    assert not directory.exists()
    return json.loads(process.stdout)


def test_confirm_population_and_rotating_schedule(manifest):
    tasks, excluded = population()
    train = runner.train_task_ids()
    assert len(tasks) == 67 and len(manifest['schedule']) == 201
    assert tasks == sorted(tasks, key=int)
    assert set(tasks) <= set(train) and not set(tasks) & set(EXCLUDED)
    assert manifest['task_ids'] == tasks
    assert manifest['excluded_development_task_ids'] == excluded == list(EXCLUDED)
    assert manifest['kernel_version'] == 'aftercare-kernel-v3.1'
    assert manifest['split'] == 'train' and manifest['scope'] == 'confirm'
    for i, task in enumerate(tasks):
        rows = manifest['schedule'][3*i:3*i+3]
        assert [r['architecture'] for r in rows] == [ARMS[(i+j) % 3] for j in range(3)]
        assert all(r['task_id'] == task and r['seed'] == 42+i for r in rows)
    assert manifest['budget']['max_calls'] == 44421


@pytest.mark.parametrize('names,extra,message', [
    (','.join(ARMS), ('--task-ids', '1'), '--task-ids'),
    (','.join(ARMS), ('--task-ids', ''), '--task-ids'),
    ('M2,S1c,S1', (), 'S1c,M2,S1'), ('S1,S2,S1c,M1,M2', (), 'S1c,M2,S1'),
    ('S1c,M2,M2', (), 'S1c,M2,S1'), ('S1c,M2', (), 'S1c,M2,S1'),
    ('', (), 'S1c,M2,S1'), (','.join(ARMS), ('--max-usd', '21'), 'fixed E3'),
])
def test_cli_rejects_selection_and_other_groups(tmp_path, names, extra, message):
    result = dry(tmp_path / 'absent', names=names, extra=extra)
    assert result.returncode != 0 and message in result.stderr
    assert not (tmp_path / 'absent').exists()


def test_development_hash_and_exclusions_fail_closed(tmp_path, monkeypatch):
    import kernel_e3_protocol as protocol
    evidence = tmp_path / 'evidence/architecture_refactor'
    evidence.mkdir(parents=True)
    path = evidence / 'kernel_v3_dev_tasks.json'
    frozen = read_json(runner.ROOT / 'evidence/architecture_refactor/kernel_v3_dev_tasks.json')
    monkeypatch.setattr(protocol, 'ROOT', tmp_path)
    args = SimpleNamespace(scope='confirm', architectures=','.join(ARMS), task_ids=None, **LIMITS)
    for corrupt in ('hash', 'exclusions'):
        value = deepcopy(frozen)
        if corrupt == 'hash':
            value['dataset_sha256']['tasks.json'] = 'invalid'
        else:
            value['development_task_ids'] = list(EXCLUDED[:-1])
        write_json(path, value)
        with pytest.raises(SystemExit, match='hash mismatch' if corrupt == 'hash' else 'exclusions'):
            runner.new_campaign(args)


def test_full_five_group_schedule_is_unchanged(tmp_path):
    process = dry(tmp_path / 'absent', scope='full', names='S1,S2,S1c,M1,M2')
    assert process.returncode == 0, process.stderr
    current = json.loads(process.stdout)
    e2 = read_json(runner.ROOT / '.runtime/kernel-e2-20261113/manifest.json')
    assert current['schedule'] == e2['schedule'] and len(current['schedule']) == 200
    assert current['task_ids'] == e2['task_ids'] and current['split'] == 'test'
    assert not (tmp_path / 'absent').exists()


def campaign(directory, manifest, successes=None, costs=None):
    directory.mkdir()
    (directory / 'rows').mkdir()
    (directory / 'official').mkdir()
    write_json(directory / 'manifest.json', manifest)
    successes = successes or dict.fromkeys(ARMS, 10)
    costs = costs or {'S1c': '0.01', 'M2': '0.02', 'S1': '0.04'}
    lines = []
    for index, item in enumerate(manifest['schedule'], 1):
        name, task = item['architecture'], item['task_id']
        key = f'{task}-{name}'
        row = {**item, 'status': 'completed', 'duration_seconds': 1,
               'official_reward': int(manifest['task_ids'].index(task) < successes[name]),
               'agent_calls': 1, 'user_calls': 0, 'review_calls': 0,
               'agent_cost_usd': float(costs[name]), 'user_cost_usd': 0, 'review_cost_usd': 0}
        write_json(directory / 'rows' / f'{key}.json', row)
        write_json(directory / 'official' / f'{key}.json', {'messages': [], 'private_trace': []})
        lines.extend([{'call_id': index, 'row': key, 'role': 'agent', 'actor': 'executor', 'reserve_usd': '1'},
                      {'call_id': index, 'actual_usd': costs[name]}])
    (directory / 'calls.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in lines))
    return directory


@pytest.mark.parametrize('counts,costs,lift,cost,conclusion', [
    ((10, 20, 20), None, True, True, 'lift_and_cost_advantage_shown'),
    ((10, 11, 10), None, False, True, 'cost_advantage_shown'),
    ((10, 10, 14), None, False, False, 'no_advantage_shown'),
    ((10, 10, 13), None, False, True, 'cost_advantage_shown'),
    ((10, 20, 20), {'S1c': '0.01', 'M2': '0.04', 'S1': '0.04'}, True, False, 'lift_advantage_shown'),
    ((10, 10, 10), {'S1c': '0.01', 'M2': '0.04', 'S1': '0.04'}, False, False, 'no_advantage_shown'),
    ((20, 10, 10), None, False, True, 'cost_advantage_shown'),
    ((0, 0, 0), None, False, None, 'undetermined'),
])
def test_fixed_decisions(tmp_path, manifest, counts, costs, lift, cost, conclusion):
    directory = campaign(tmp_path / 'run', manifest, dict(zip(ARMS, counts)), costs)
    report = analyse(directory)
    assert report['hypotheses']['H_lift']['holds'] is lift
    assert report['hypotheses']['H_cost']['holds'] is cost
    assert report['conclusion'] == conclusion
    assert report['tasks'] == 67
    for name, success in zip(ARMS, counts):
        assert report['arms'][name]['tasks'] == 67
        assert report['arms'][name]['success_ci95'] == wilson(success, 67)


@pytest.mark.parametrize('kind', ['unsettled_S1c', 'unsettled_M2', 'missing_journal',
                                 'truncated', 'missing_row', 'missing_cost', 'orphan', 'count_mismatch'])
def test_incomplete_cost_is_always_undetermined(tmp_path, manifest, kind):
    root = campaign(tmp_path / 'run', manifest)
    task = manifest['task_ids'][0]
    journal = root / 'calls.jsonl'
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    if kind.startswith('unsettled'):
        arm = kind.split('_')[1]
        ident = next(e['call_id'] for e in events if e.get('row') == f'{task}-{arm}')
        events = [e for e in events if not ('actual_usd' in e and e['call_id'] == ident)]
        journal.write_text(''.join(json.dumps(e) + '\n' for e in events))
    elif kind == 'missing_journal':
        journal.unlink()
    elif kind in {'truncated', 'orphan'}:
        with journal.open('a') as f:
            f.write('{' if kind == 'truncated' else '{"call_id":9999,"actual_usd":"0.1"}\n')
    else:
        path = root / 'rows' / f'{task}-M2.json'
        row = read_json(path)
        if kind == 'missing_row':
            path.unlink()
        else:
            row['agent_cost_usd' if kind == 'missing_cost' else 'agent_calls'] = None
            write_json(path, row)
    report = analyse(root)
    assert report['hypotheses']['H_cost']['status'] == report['conclusion'] == 'undetermined'
    assert report['hypotheses']['H_cost']['holds'] is None
    assert all(a['tasks'] == 67 for a in report['arms'].values())


def test_auto_merge_repair_keeps_costs_denominator_and_provenance(tmp_path, manifest):
    root = campaign(tmp_path / 'run', manifest)
    item = next(i for i in manifest['schedule'] if i['architecture'] == 'M2')
    key = f"{item['task_id']}-M2"
    original_path = root / 'rows' / f'{key}.json'
    original = read_json(original_path)
    original.update(status='error', official_reward=None)
    write_json(original_path, original)
    repair_manifest = {**manifest, 'scope': 'repair', 'task_ids': [item['task_id']], 'schedule': [item],
        'repair_of': {'manifest_sha256': sha256((root / 'manifest.json').read_bytes()).hexdigest()},
        'original_attempts': {key: {'row_sha256': sha256(original_path.read_bytes()).hexdigest()}}}
    campaign(root.with_name('run.repair'), repair_manifest)
    result = analyse(root)
    assert result['arms']['M2']['success'] == 10 and result['arms']['M2']['tasks'] == 67
    assert result['arms']['M2']['total_cost_usd'] == 68 * .02
    assert result['per_task'][item['task_id']]['M2']['total_cost_usd'] == .04
    assert result['repair']['attempts'][key]['score_source'] == 'repair'
    assert read_json(original_path) == original
    original['official_reward'] = 0
    write_json(original_path, original)
    with pytest.raises(ValueError, match='Original row changed'):
        analyse(root)


def test_repair_uses_train_and_original_balance_once(tmp_path, manifest, monkeypatch):
    root = campaign(tmp_path / 'run', manifest)
    item = manifest['schedule'][0]
    path = root / 'rows' / f"{item['task_id']}-{item['architecture']}.json"
    row = read_json(path)
    row.update(status='unknown', official_reward=None)
    write_json(path, row)
    captured = []
    # No agent is constructed by this planning test. Avoid the process-global registry mutation.
    monkeypatch.setattr(runner, 'initialize_official', lambda: None)
    monkeypatch.setattr(runner, 'execute_campaign', lambda *a: captured.append(a))
    args = SimpleNamespace(source=root, max_usd=Decimal('1'), live=False)
    runner.repair_campaign(args)
    directory, repaired, tasks, live, _ = captured[0]
    assert repaired['split'] == 'train' and repaired['schedule'] == [item]
    assert set(tasks) == {item['task_id']} and not live and not directory.exists()
    assert repaired['budget']['max_calls'] == 221
    args.max_usd = Decimal('20')
    with pytest.raises(SystemExit, match='remaining original'):
        runner.repair_campaign(args)
    args.max_usd = Decimal('1')
    directory.mkdir()
    with pytest.raises(SystemExit, match='already exists'):
        runner.repair_campaign(args)


def test_m2_descriptive_events_and_settled_incomplete_receipt(tmp_path, manifest):
    root = campaign(tmp_path / 'run', manifest)
    item = next(i for i in manifest['schedule'] if i['architecture'] == 'M2')
    key = f"{item['task_id']}-M2"
    events = [
        {'stage': 'expert_fact_expert', 'output': {'read_log': [{'status': 'executed'}, {'status': 'rejected'}]}},
        {'stage': 'expert_self_write_rebased', 'output': {'records': ['order:1', 'order:2']}},
        {'stage': 'guard', 'output': {'rule_ids': ['amount_arithmetic']}},
        {'stage': 'guard_soft_unresolved', 'output': {'rule_ids': ['amount_arithmetic']}},
        {'stage': 'proposal_review', 'output': {'verdict': 'revise', 'fallback_reason': 'IncompleteResponse'}},
    ]
    write_json(root / 'official' / f'{key}.json', {'messages': [], 'private_trace': events})
    # A repeated receipt must not count a call twice; the already-settled cost stays known.
    with (root / 'calls.jsonl').open('a') as f:
        for _ in range(2):
            f.write(json.dumps({'call_id': 2, 'failure_type': 'IncompleteResponse'}) + '\n')
    report = analyse(root)
    assert report['M2_descriptive'] == {
        'expert_consultations': 1, 'expert_reads': 1, 'self_write_updates': 1,
        'amount_rule_triggers': 1, 'review_revise': 1, 'review_fallback': 1, 'incomplete_response': 1}
    assert report['arms']['M2']['cost_complete']


def test_transport_records_incomplete_after_settlement(tmp_path):
    from tau_official_budget import Budget, IncompleteResponse, RowTransport
    from test_tau_official_campaign import fake_response, LIMITS as TRANSPORT_LIMITS
    response = fake_response('partial')
    response.status = 'incomplete'
    budget = Budget(3, 1, tmp_path / 'calls.jsonl')
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
    transport = RowTransport('1-M2', TRANSPORT_LIMITS, budget, client)
    with pytest.raises(IncompleteResponse):
        transport.completion(model='gpt-6.1-sol', messages=[], tools=None, tool_choice=None,
                             aftercare_role='executor')
    events = [json.loads(line) for line in budget.journal.read_text().splitlines()]
    assert len(events) == 3 and 'actual_usd' in events[1]
    assert events[2]['failure_type'] == 'IncompleteResponse'
    assert len({e['call_id'] for e in events}) == 1 and transport.failure_status is None
