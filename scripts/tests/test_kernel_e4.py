"""E4 offline fixtures, provenance checks and dry runs; no paid calls or runtime writes."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

import freeze_kernel_e4 as freezer
import analyze_kernel_e4 as analyzer
import kernel_e4_protocol as protocol
import run_tau_official_campaign as runner
from analyze_kernel_campaign import mcnemar, wilson
from analyze_kernel_e3 import analyse as analyse_e3
from analyze_kernel_e4 import analyse
from kernel_e3_protocol import ARMS as E3_ARMS
from kernel_e4_protocol import ARMS, CELLS, LIMITS, population
from tau_official_summary import read_json, write_json
from test_kernel_e3 import campaign


@pytest.fixture(scope='module', autouse=True)
def e3_source(tmp_path_factory):
    """Synthetic E3 fixture for current test code; frozen E3 stays byte-for-byte intact.

    VERSION alone cannot emulate old inputs: E4 also compares actual source hashes.
    Keep that comparison active against a temporary fixture, including corruption tests.
    """
    from aftercare.agent_kernel import contracts
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(contracts, 'VERSION', 'aftercare-kernel-v3.1')
        original = read_json(protocol.E3_MANIFEST)
        original['kernel_sha256'] = protocol.current_configuration()['kernel_sha256']
        path = tmp_path_factory.mktemp('e4-synthetic-e3') / 'manifest.json'
        write_json(path, original)
        for module in (protocol, freezer, analyzer):
            patch.setattr(module, 'E3_MANIFEST', path)
        yield path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from aftercare.agent_kernel import contracts
    monkeypatch.setattr(contracts, 'VERSION', 'aftercare-kernel-v3.1')
    def forbidden(*args, **kwargs):
        raise AssertionError('E4 tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def dry(directory, extra=()):
    argv = [str(runner.ROOT / 'scripts/run_tau_official_campaign.py'), 'run',
            '--scope', 'ablate', '--directory', str(directory), '--architectures', ','.join(ARMS)]
    for key, value in LIMITS.items():
        argv.extend(['--' + key.replace('_', '-'), str(value)])
    code = '''import runpy, socket, sys
from pathlib import Path
e3_manifest = Path(sys.argv.pop(1))
def forbidden(*args, **kwargs):
    raise AssertionError('No network permitted')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
sys.argv = sys.argv[1:]
from pytest import MonkeyPatch
from aftercare.agent_kernel import contracts
import kernel_e4_protocol
with MonkeyPatch.context() as patch:
    patch.setattr(contracts, 'VERSION', 'aftercare-kernel-v3.1')
    patch.setattr(kernel_e4_protocol, 'E3_MANIFEST', e3_manifest)
    runpy.run_path(sys.argv[0], run_name='__main__')
'''
    return subprocess.run([sys.executable, '-B', '-c', code, str(protocol.E3_MANIFEST), *argv, *extra], capture_output=True, text=True)


@pytest.fixture(scope='module')
def manifest(tmp_path_factory, e3_source):
    directory = tmp_path_factory.mktemp('e4-dry') / '.runtime/kernel-e4/run-v1'
    process = dry(directory)
    assert process.returncode == 0, process.stderr
    assert not directory.parents[1].exists()
    return json.loads(process.stdout)


def test_schedule_population_seeds_and_dry_run(manifest, tmp_path):
    tasks, excluded = population()
    e3 = read_json(protocol.E3_MANIFEST)
    assert len(manifest['schedule']) == 134 and len(tasks) == 67
    assert manifest['task_ids'] == e3['task_ids'] == tasks
    assert manifest['excluded_development_task_ids'] == excluded
    assert manifest['architectures'] == ['M2x', 'M2r']
    assert manifest['scope'] == 'ablate' and manifest['split'] == 'train'
    assert manifest['budget']['max_calls'] == 29614
    assert manifest['kernel_version'] == 'aftercare-kernel-v3.1'
    for i, task in enumerate(tasks):
        items = manifest['schedule'][2*i:2*i+2]
        assert [row['architecture'] for row in items] == (['M2x', 'M2r'] if i % 2 == 0 else ['M2r', 'M2x'])
        assert all(row['task_id'] == task and row['seed'] == 42+i for row in items)
        assert {row['seed'] for row in e3['schedule'] if row['task_id'] == task} == {42+i}
    again = dry(tmp_path / '.runtime/kernel-e4/run-v1')
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == manifest
    assert not (tmp_path / '.runtime').exists()


@pytest.mark.parametrize('field', list(LIMITS))
def test_every_fixed_limit_rejected(field):
    args = SimpleNamespace(task_ids=None, architectures=','.join(ARMS), **LIMITS)
    setattr(args, field, LIMITS[field] + 1)
    with pytest.raises(SystemExit, match='fixed E4'):
        protocol.validate_options(args)


@pytest.mark.parametrize('extra,message', [
    (('--task-ids', '0'), '--task-ids'), (('--task-ids', ''), '--task-ids'),
    (('--architectures', 'M2r,M2x'), 'M2x,M2r'),
    (('--architectures', 'M2x,M2x'), 'M2x,M2r'),
    (('--architectures', 'S1c,M2'), 'M2x,M2r'),
    (('--architectures', ''), 'M2x,M2r'), (('--max-usd', '9'), 'fixed E4'),
])
def test_cli_invalid_options_do_not_create_runtime(tmp_path, extra, message):
    process = dry(tmp_path / '.runtime/kernel-e4/run-v1', extra)
    assert process.returncode != 0 and message in process.stderr
    assert not (tmp_path / '.runtime').exists()


@pytest.mark.parametrize('field', list(protocol.MATCH_FIELDS))
def test_e3_consistency_fails_closed_with_forged_manifest(tmp_path, manifest, field):
    original = read_json(protocol.E3_MANIFEST)
    path = tmp_path / 'e3-manifest.json'
    write_json(path, original)
    protocol.validate_e3_consistency(manifest, path)
    original[field] = 'changed'
    write_json(path, original)
    with pytest.raises(ValueError, match='E3'):
        protocol.validate_e3_consistency(manifest, path)


def test_current_kernel_hash_mismatch_even_when_run_manifests_match(tmp_path, manifest, monkeypatch):
    kernel = tmp_path / 'backend/src/aftercare/agent_kernel'
    kernel.mkdir(parents=True)
    for path in (runner.ROOT / 'backend/src/aftercare/agent_kernel').glob('*.py'):
        (kernel / path.name).write_bytes(path.read_bytes())
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/tau_official_agents.py').write_bytes((runner.ROOT / 'scripts/tau_official_agents.py').read_bytes())
    original = tmp_path / 'e3.json'
    original.write_bytes(protocol.E3_MANIFEST.read_bytes())
    monkeypatch.setattr(protocol, 'ROOT', tmp_path)
    protocol.validate_e3_consistency(manifest, original)
    with (kernel / 'engine.py').open('a') as stream:
        stream.write('\n# modified\n')
    with pytest.raises(ValueError, match='kernel_sha256'):
        protocol.validate_e3_consistency(manifest, original)


@pytest.mark.parametrize('kind', ['seed', 'order', 'duplicate', 'budget', 'split', 'excluded', 'model'])
def test_manifest_validation_rejects_changed_design(manifest, kind):
    value = deepcopy(manifest)
    if kind == 'seed':
        value['schedule'][0]['seed'] += 1
    elif kind == 'order':
        value['schedule'].reverse()
    elif kind == 'duplicate':
        value['schedule'][1] = value['schedule'][0]
    elif kind == 'budget':
        value['budget']['max_usd'] = 20
    elif kind == 'split':
        value['split'] = 'test'
    elif kind == 'excluded':
        value['excluded_development_task_ids'] = []
    else:
        value['agent_model'] = 'other'
    with pytest.raises(ValueError, match='E4'):
        protocol.validate_manifest(value, *population())


def set_successes(root, items, selected):
    for item in items:
        path = root / 'rows' / f"{item['task_id']}-{item['architecture']}.json"
        row = read_json(path)
        row['official_reward'] = int(item['task_id'] in selected[item['architecture']])
        write_json(path, row)


@pytest.fixture
def fixture_inputs(tmp_path, manifest):
    tasks = manifest['task_ids']
    e3_manifest = read_json(protocol.E3_MANIFEST)
    old = campaign(tmp_path / 'e3', e3_manifest)
    old_success = {'S1c': {tasks[i] for i in (0, 1)}, 'M2': {tasks[i] for i in (0, 2, 3, 4)}, 'S1': set()}
    set_successes(old, e3_manifest['schedule'], old_success)
    e3 = analyse_e3(old)
    e3_path = tmp_path / 'e3-results.json'
    write_json(e3_path, e3)
    root = campaign(tmp_path / 'run', manifest, dict.fromkeys(ARMS, 0), {'M2x': '.01', 'M2r': '.02'})
    selected = {'M2x': {tasks[i] for i in (0, 1, 2)}, 'M2r': {tasks[i] for i in (1, 3)}}
    set_successes(root, manifest['schedule'], selected)
    registration = tmp_path / 'preregistration.json'
    write_json(registration, {
        'kernel_e3_results_sha256': sha256(e3_path.read_bytes()).hexdigest(),
        'e3_manifest_sha256': sha256(protocol.E3_MANIFEST.read_bytes()).hexdigest(),
        'schedule_sha256': protocol.schedule_sha256(manifest['schedule']), 'status': 'draft_unfrozen',
    })
    return root, {'e3_results': e3_path, 'preregistration': registration}


def test_four_cells_pairs_interaction_and_costs(fixture_inputs, manifest):
    root, options = fixture_inputs
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = analyse(root, **options)
    tasks = manifest['task_ids']
    expected = {'S1c': 2, 'M2r': 2, 'M2x': 3, 'M2': 4}
    e3 = read_json(options['e3_results'])
    assert list(result['arms']) == list(CELLS)
    for name, success in expected.items():
        arm = result['arms'][name]
        assert arm['success'] == success and arm['tasks'] == 67
        assert arm['success_rate'] == round(success / 67, 4)
        assert arm['success_ci95'] == wilson(success, 67)
        assert sum(arm['outcomes'].values()) == 67
        assert arm['cost_per_task_usd'] == pytest.approx(.02 if name in ('M2r', 'M2') else .01)
        assert arm['total_cost_per_success_usd'] == pytest.approx(arm['total_cost_usd'] / success)
        assert arm['calls_by_actor'] == {'executor': 67}
        assert arm['cost_usd_by_actor']['executor'] == arm['total_cost_usd']
        if name in ('S1c', 'M2'):
            assert arm == {**e3['arms'][name], 'source': 'E3'}
            for task in tasks:
                assert result['per_task'][task][name] == {**e3['per_task'][task][name], 'source': 'E3'}
    comparisons = {'M2_vs_M2r': ((0, 2, 4), (1,)), 'M2x_vs_S1c': ((2,), ()),
                   'M2_vs_M2x': ((3, 4), (1,)), 'M2r_vs_S1c': ((3,), (0,))}
    assert set(result['pairs']) == set(comparisons)
    for key, (left_ids, right_ids) in comparisons.items():
        left, right = key.split('_vs_')
        pair = result['pairs'][key]
        assert pair[f'{left}_only'] == [tasks[i] for i in left_ids]
        assert pair[f'{right}_only'] == [tasks[i] for i in right_ids]
        assert pair[f'{left}_only_count'] == len(left_ids)
        assert pair[f'{right}_only_count'] == len(right_ids)
        assert pair['difference'] == len(left_ids) - len(right_ids)
        assert pair['mcnemar_p'] == mcnemar(len(left_ids), len(right_ids))
    assert result['interaction']['success_count_difference'] == 1
    assert result['complete'] and result['unsettled_calls'] == 0
    assert result['anomalies'] == result['missing_rows'] == result['missing_trace_rows'] == []
    assert result['descriptive'] is True and '运行日期不同' in result['descriptive_note']
    assert not {'hypotheses', 'conclusion', 'holds'} & result.keys()
    assert {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_mechanisms_and_disabled_role_anomalies(fixture_inputs, manifest):
    root, options = fixture_inputs
    task = manifest['task_ids'][0]
    events = [
        {'stage': 'expert_fact_expert', 'output': {'read_log': [{'status': 'executed'}, {'status': 'rejected'}]}},
        {'stage': 'expert_self_write_rebased', 'output': {}},
        {'stage': 'guard', 'output': {'rule_ids': ['amount_arithmetic']}},
        {'stage': 'guard_soft_unresolved', 'output': {'rule_ids': ['amount_arithmetic']}},
        {'stage': 'proposal_review', 'output': {'verdict': 'revise', 'fallback_reason': 'IncompleteResponse'}},
        {'stage': 'proposal_review', 'output': {'verdict': 'allow'}},
    ]
    for name in ARMS:
        write_json(root / 'official' / f'{task}-{name}.json', {'messages': [], 'private_trace': events})
    with (root / 'calls.jsonl').open('a') as stream:
        for call_id in (1, 1, 2):
            stream.write(json.dumps({'call_id': call_id, 'failure_type': 'IncompleteResponse'}) + '\n')
    report = analyse(root, **options)
    for name in ARMS:
        assert report['mechanisms'][name] == {
            'expert_consultations': 1, 'expert_reads': 1, 'self_write_updates': 1,
            'amount_rule_triggers': 1, 'review_revise': 1, 'review_fallback': 1, 'incomplete_response': 1}
    anomalies = {(a['architecture'], a['metric']): a['count'] for a in report['anomalies']}
    assert anomalies == {('M2x', 'expert_consultations'): 1, ('M2x', 'expert_reads'): 1,
                         ('M2x', 'self_write_updates'): 1, ('M2r', 'review_triggered'): 2,
                         ('M2r', 'review_revise'): 1, ('M2r', 'review_fallback'): 1}


@pytest.mark.parametrize('kind', ['e3_results', 'e3_manifest', 'schedule'])
def test_preregistration_hashes_fail_closed(fixture_inputs, kind):
    root, options = fixture_inputs
    if kind == 'e3_results':
        # Invalid JSON deliberately verifies that the hash check precedes JSON parsing.
        options['e3_results'].write_text('not JSON')
    else:
        registration = read_json(options['preregistration'])
        registration[f'{kind}_sha256'] = 'wrong'
        write_json(options['preregistration'], registration)
    with pytest.raises(ValueError, match='SHA256 mismatch'):
        analyse(root, **options)


@pytest.mark.parametrize('kind', ['missing_row', 'missing_trace', 'unsettled', 'missing_journal', 'truncated', 'zero_success'])
def test_incomplete_rows_costs_and_zero_success(fixture_inputs, manifest, kind):
    root, options = fixture_inputs
    task = manifest['task_ids'][0]
    if kind == 'missing_row':
        (root / 'rows' / f'{task}-M2x.json').unlink()
    elif kind == 'missing_trace':
        (root / 'official' / f'{task}-M2x.json').unlink()
    elif kind == 'zero_success':
        set_successes(root, manifest['schedule'], dict.fromkeys(ARMS, set()))
    else:
        journal = root / 'calls.jsonl'
        if kind == 'unsettled':
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            journal.write_text(''.join(json.dumps(e) + '\n' for e in events if not (e['call_id'] == 1 and 'actual_usd' in e)))
        elif kind == 'missing_journal':
            journal.unlink()
        else:
            with journal.open('a') as stream:
                stream.write('{')
    result = analyse(root, **options)
    assert all(a['tasks'] == 67 for a in result['arms'].values())
    if kind == 'zero_success':
        assert result['arms']['M2x']['total_cost_per_success_usd'] is None
    else:
        assert result['complete'] is False
    if kind == 'missing_row':
        assert result['missing_rows'] == [f'E4/run/{task}-M2x']
        assert f'E4/{task}-M2x' in result['unfinished_rows']
    elif kind == 'missing_trace':
        assert result['missing_trace_rows'] == [f'E4/run/{task}-M2x']
    elif kind == 'unsettled':
        assert result['unsettled_calls'] == 1
    if kind not in {'missing_trace', 'zero_success'}:
        assert result['arms']['M2x']['cost_complete'] is False
        assert result['arms']['M2x']['total_cost_usd'] is None
        assert result['arms']['M2x']['cost_per_task_usd'] is None


def make_repair(root, manifest):
    item = manifest['schedule'][0]
    key = f"{item['task_id']}-{item['architecture']}"
    path = root / 'rows' / f'{key}.json'
    row = read_json(path)
    row.update(status='unknown', official_reward=None)
    write_json(path, row)
    return item, key, path


def test_repair_analysis_accumulates_attempts_and_checks_provenance(fixture_inputs, manifest):
    root, options = fixture_inputs
    item, key, path = make_repair(root, manifest)
    repaired = {**manifest, 'scope': 'repair', 'schedule': [item], 'task_ids': [item['task_id']],
                'repair_of': {'manifest_sha256': sha256((root / 'manifest.json').read_bytes()).hexdigest()},
                'original_attempts': {key: {'row_sha256': sha256(path.read_bytes()).hexdigest()}}}
    repair_dir = campaign(root.with_name('run.repair'), repaired, dict.fromkeys(ARMS, 1), {'M2x': '.01', 'M2r': '.02'})
    event = {'stage': 'guard', 'output': {'rule_ids': ['amount_arithmetic']}}
    for directory in (root, repair_dir):
        write_json(directory / 'official' / f'{key}.json', {'messages': [], 'private_trace': [event]})
        with (directory / 'calls.jsonl').open('a') as stream:
            stream.write('{"call_id":1,"failure_type":"IncompleteResponse"}\n')
    report = analyse(root, **options)
    assert report['arms']['M2x']['success'] == 3
    assert report['arms']['M2x']['total_cost_usd'] == .68
    assert report['arms']['M2x']['calls_by_actor']['executor'] == 68
    assert report['per_task'][item['task_id']]['M2x']['total_cost_usd'] == .02
    assert report['mechanisms']['M2x']['amount_rule_triggers'] == 2
    assert report['mechanisms']['M2x']['incomplete_response'] == 2
    assert report['repair']['attempts'][key]['score_source'] == 'repair'
    row = read_json(path)
    row['official_reward'] = 0
    write_json(path, row)
    with pytest.raises(ValueError, match='Original row changed'):
        analyse(root, **options)


def test_repair_runner_uses_e4_validation_balance_and_single_attempt(fixture_inputs, manifest, monkeypatch):
    root, _ = fixture_inputs
    item, _, _ = make_repair(root, manifest)
    captured = []
    monkeypatch.setattr(runner, 'initialize_official', lambda: None)
    monkeypatch.setattr(runner, 'execute_campaign', lambda *args: captured.append(args))
    args = SimpleNamespace(source=root, max_usd=Decimal('1'), live=False)
    runner.repair_campaign(args)
    directory, repaired, tasks, live, _ = captured[0]
    assert repaired['split'] == 'train' and repaired['schedule'] == [item]
    assert set(tasks) == {item['task_id']} and not live and not directory.exists()
    assert repaired['budget']['max_calls'] == 221
    assert repaired['repair_of']['budget']['max_usd'] == 8
    args.max_usd = Decimal('7')  # $8 - $2.01 = $5.99 remaining.
    with pytest.raises(SystemExit, match='remaining original'):
        runner.repair_campaign(args)
    args.max_usd = Decimal('1')
    corrupted = deepcopy(manifest)
    corrupted['budget']['max_usd'] = 20
    write_json(root / 'manifest.json', corrupted)
    with pytest.raises(ValueError, match='fixed E4'):
        runner.repair_campaign(args)
    write_json(root / 'manifest.json', manifest)
    directory.mkdir()
    with pytest.raises(SystemExit, match='already exists'):
        runner.repair_campaign(args)


def test_freezer_only_creates_draft_in_temporary_directory(tmp_path, manifest, monkeypatch):
    frozen_before = protocol.PREREGISTRATION.read_bytes() if protocol.PREREGISTRATION.exists() else None
    output = tmp_path / 'preregistration_e4.json'
    monkeypatch.setattr(sys, 'argv', ['freeze_kernel_e4.py', '--output', str(output)])
    freezer.main()
    data = read_json(output)
    assert data['status'] == 'draft_unfrozen' and data['status_note'] == '草稿，未冻结'
    assert data['run']['runs'] == 134 and data['run']['max_calls'] == 29614
    assert data['design']['new_arms'] == ['M2x', 'M2r']
    assert data['design']['reused_E3_arms'] == ['S1c', 'M2']
    assert data['schedule_sha256'] == protocol.schedule_sha256(manifest['schedule'])
    assert data['kernel_e3_results_sha256'] == sha256(protocol.E3_RESULTS.read_bytes()).hexdigest()
    assert data['e3_manifest_sha256'] == sha256(protocol.E3_MANIFEST.read_bytes()).hexdigest()
    assert len(data['git_head']) == 40
    for relative, digest in data['sha256'].items():
        assert digest == sha256((runner.ROOT / relative).read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        freezer.main()
    assert (protocol.PREREGISTRATION.read_bytes() if protocol.PREREGISTRATION.exists() else None) == frozen_before
