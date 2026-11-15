"""Offline minitest construction, reviewer accounting and one-run safety checks."""
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal
from hashlib import sha256
import json
import socket

import pytest

from aftercare.agent_kernel.contracts import StepProposal, ToolCall
from aftercare.agent_kernel.dependencies import ADDRESS, ITEMS
from replay_review_v2 import public
import run_review_v2_minitest as mini


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network is forbidden in minitest tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(mini, 'live_client', forbidden)


@pytest.fixture(scope='module')
def official():
    return mini.official_inputs()


def test_sample_construction():
    samples = mini.samples()
    assert len(samples) == len({s['id'] for s in samples}) == 34
    assert Counter(s['group'] for s in samples) == {'false-block': 33, 'block': 1}
    control, = [s for s in samples if s['group'] == 'block']
    assert control['id'].startswith('97-M2-m')
    assert control['proposal']['calls'][0]['name'] == ITEMS
    for sample in samples:
        raw = (mini.ROOT / sample['source']).read_bytes()
        assert sha256(raw).hexdigest() == sample['source_sha256']
        messages = json.loads(raw)['messages']
        assert sample['messages'] == public(messages[:sample['message_index']])
        call = sample['proposal']['calls'][0]
        assert any(all(c[k] == call[k] for k in ('id', 'name', 'arguments'))
                   for c in messages[sample['message_index']]['tool_calls'])


def test_hash_mismatch_precedes_trajectory_parsing(tmp_path):
    rows = json.loads(mini.DATA.read_text())
    row = next(r for r in rows['rows'] if r['arm'] == 'M1' and r['outcome'] == 'success')
    bad = tmp_path / 'bad.json'
    bad.write_text('this is not even JSON')
    row.update(source=str(bad), source_sha256='0' * 64)
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match='Source SHA256 mismatch'):
        mini.samples(inventory)


def test_default_only_prints_manifest(tmp_path, monkeypatch, capsys, official):
    schedule = mini.samples()
    monkeypatch.setattr(mini, 'ROOT', tmp_path)
    monkeypatch.setattr(mini, 'samples', lambda: schedule)
    monkeypatch.setattr(mini, 'official_inputs', lambda: official)
    mini.main([])
    manifest = json.loads(capsys.readouterr().out)
    assert manifest['sample_count'] == 34
    assert manifest['max_calls'] == 68
    assert manifest['max_usd'] == '1.5'
    assert manifest['kernel_version'] == 'aftercare-kernel-v3.1'
    assert manifest['policy_sha256'] == sha256(official[0].encode()).hexdigest()
    assert manifest['prompt_sha256'] == sha256(mini.REVIEW_INSTRUCTION.encode()).hexdigest()
    assert len(manifest['sample_ids']) == 34
    assert not (tmp_path / '.runtime').exists()


@pytest.mark.parametrize('version', ['aftercare-kernel-v2', 'aftercare-kernel-v3.1', 'future-version'])
def test_only_live_manifest_requires_frozen_v2(version, monkeypatch, official):
    monkeypatch.setattr(mini, 'VERSION', version)
    schedule = mini.samples()
    assert mini.manifest_for(schedule, official[0])['kernel_version'] == version
    if version == 'aftercare-kernel-v2':
        assert mini.manifest_for(schedule, official[0], live=True)['kernel_version'] == version
    else:
        with pytest.raises(ValueError, match='requires kernel v2'):
            mini.manifest_for(schedule, official[0], live=True)


def fixture_sample(sample_id, group='false-block'):
    """Small authenticated, confirmed item write accepted by the real retail guard."""
    def exchange(name, arguments, result, number):
        return [{'role': 'assistant', 'content': None,
                 'tool_calls': [{'id': str(number), 'name': name, 'arguments': arguments}]},
                {'role': 'tool', 'id': str(number), 'content': json.dumps(result)}]
    messages = [{'role': 'user', 'content': 'Change my item and shipping address.'}]
    messages += exchange('find_user_id_by_email', {'email': 'a@example.com'}, 'alice_1', 1)
    messages += exchange('get_order_details', {'order_id': '#1'},
                         {'order_id': '#1', 'user_id': 'alice_1', 'status': 'pending',
                          'items': [{'item_id': 'old', 'product_id': 'product', 'price': 10}]}, 2)
    messages += exchange('get_product_details', {'product_id': 'product'},
                         {'product_id': 'product', 'variants': {'new': {'available': True, 'price': 10}}}, 3)
    messages += exchange('get_user_details', {'user_id': 'alice_1'},
                         {'user_id': 'alice_1', 'orders': ['#1'],
                          'payment_methods': {'card': {'source': 'credit_card'}}}, 4)
    messages += [{'role': 'assistant', 'content': 'Confirm changing old to new using card?'},
                 {'role': 'user', 'content': 'Yes.'}]
    proposal = StepProposal(calls=(ToolCall(id='write', name=ITEMS, arguments={
        'order_id': '#1', 'item_ids': ['old'], 'new_item_ids': ['new'], 'payment_method_id': 'card'}),))
    return {'id': sample_id, 'group': group,
            'expectation': {'blocking_reason': 'order_dependency_conflict' if group == 'block' else None},
            'messages': public(messages), 'proposal': proposal.model_dump(mode='json')}


def review_output(*, conflict=False, revise=False):
    reference = {'message_id': 'm0', 'field': 'content', 'quote': 'Change my item and shipping address.'}
    requests = [{'description': 'Change item', 'operation': ITEMS, 'order_id': '#1',
                 'item_ids': ['old'], 'status': 'pending', 'references': [reference]}]
    if conflict:
        requests.append({'description': 'Change shipping address', 'operation': ADDRESS,
                         'order_id': '#1', 'status': 'pending', 'references': [reference]})
    return json.dumps({'verdict': 'revise' if revise else 'allow', 'requests': requests,
                       'issue': 'Check the requested item' if revise else '',
                       'next_step': 'Ask which item variant is wanted' if revise else '',
                       'references': [reference] if revise else []})


def fake_port(monkeypatch, outputs):
    class FakePort:
        def __init__(self, row):
            self.row = row

        def generate(self, *, role, assignment, messages, tools, stage):
            assert role == 'reviewer' and assignment.model == 'gpt-6.1-sol'
            assert not tools and stage == 'proposal_review'
            assert 'executor_context' not in json.loads(messages[1]['content'])
            row = self.row
            assert row.calls['agent'] < row.limits['max_agent_calls_per_run']
            call_id = row.budget.reserve(row.row, 'agent', stage, Decimal('.01'))
            row.calls['agent'] += 1
            row.budget.settle(call_id, Decimal('.01'), Decimal('.001'))
            row.costs['agent'] += Decimal('.001')
            return StepProposal(text=outputs[row.row][row.calls['agent'] - 1])
    monkeypatch.setattr(mini, 'Port', FakePort)


def test_summarize_fixed_reviews_is_read_only(tmp_path, monkeypatch, capsys, official):
    ids = ['false-conflict', 'fallback', 'qualified', 'model-revise', '97-M2-m11', 'skip', 'not-run']
    schedule = [fixture_sample(key, 'block' if key.startswith('97-') else 'false-block') for key in ids]
    schedule[-2]['messages'] = []  # Real guard rejects unauthenticated write.
    outputs = {'false-conflict': [review_output(conflict=True)], 'fallback': ['invalid', 'invalid'],
               # First attempt drops a malformed entry and retries its missing current inventory.
               'qualified': [json.dumps({'verdict': 'allow', 'requests': [{}]}), review_output()],
               'model-revise': [review_output(revise=True)],
               '97-M2-m11': [review_output(conflict=True)]}
    fake_port(monkeypatch, outputs)
    directory = tmp_path / '.runtime/review-v2-minitest/run-v1'
    directory.mkdir(parents=True)
    manifest = {'schedule': [{k: v for k, v in s.items() if k != 'messages'} for s in schedule]}
    mini.dump(directory / 'manifest.json', manifest)
    budget = mini.Budget(14, '1.5', directory / 'calls.jsonl')
    for sample in schedule[:-1]:
        row = mini.RowTransport(sample['id'], mini.LIMITS, budget, None)
        record = mini.review_sample(sample, *official, row)
        mini.dump(directory / (sample['id'] + '.json'), record)
    # Unknown billed result remains explicitly unsettled; it is not silently costed at zero.
    budget.reserve('unknown', 'agent', 'proposal_review', Decimal('.01'))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report_path = tmp_path / 'report.json'
    monkeypatch.setattr(mini, 'ROOT', tmp_path)
    monkeypatch.setattr(mini, 'REPORT', report_path)
    monkeypatch.setattr(mini, 'official_inputs', lambda: pytest.fail('Summarize must not load current inputs'))
    mini.main(['--summarize'])
    report = json.loads(report_path.read_text())
    assert report == json.loads(capsys.readouterr().out)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert report['checklist_qualified_count'] == 4
    assert report['checklist_qualified_rate'] == 4 / 7
    assert report['false_conflict_count'] == 1
    assert report['false_conflicts'][0]['id'] == 'false-conflict'
    assert report['false_conflicts'][0]['requests'] and report['false_conflicts'][0]['issue']
    assert report['false_conflict_wilson_95'] == mini.wilson(1, 6)
    assert report['review_failure_count'] == 1
    assert report['review_failure_samples_by_stage'] == {'parse': 1}
    assert report['review_failure_events_by_stage'] == {'parse': 2}
    assert report['retry_count'] == 2
    assert report['model_revise_count'] == 1
    assert report['model_revisions'][0]['id'] == 'model-revise'
    assert report['block_group_97_hit'] is True
    assert report['guard_not_allowed'][0]['id'] == 'skip'
    assert report['missing_or_failed_samples'][0]['id'] == 'not-run'
    assert report['total_actual_usd'] == '0.007'
    assert report['unsettled_calls'] == 1
    assert report['attempted_calls'] == 8
    assert not report['complete']
    qualified = json.loads((directory / 'qualified.json').read_text())
    assert qualified['trace_output']['dropped_requests']
    assert qualified['trace_output']['failures'][0]['phase'] == 'request_inventory'


def test_one_run_and_no_failed_sample_retries(tmp_path, monkeypatch, official):
    schedule = [fixture_sample('fallback')]
    fake_port(monkeypatch, {'fallback': ['bad', 'bad']})
    manifest = {'max_calls': 2, 'schedule': schedule}
    directory = tmp_path / 'run'
    mini.run_once(directory, manifest, schedule, *official, lambda: nullcontext(None))
    record = json.loads((directory / 'fallback.json').read_text())
    assert record['result']['fallback_reason']
    assert record['metrics']['agent_calls'] == 2
    before = (directory / 'calls.jsonl').read_bytes()
    with pytest.raises(ValueError, match='runs once'):
        mini.run_once(directory, manifest, schedule, *official, lambda: pytest.fail('Second client'))
    assert (directory / 'calls.jsonl').read_bytes() == before
    with pytest.raises(ValueError, match='Frozen manifest mismatch'):
        mini.run_once(directory, {**manifest, 'max_calls': 3}, schedule, *official, lambda: None)


def test_empty_journal_still_prevents_restart(tmp_path, official):
    (tmp_path / 'calls.jsonl').touch()
    with pytest.raises(ValueError, match='runs once'):
        mini.run_once(tmp_path, {}, [], *official, lambda: pytest.fail('Restart client'))


def test_budget_exhaustion_stops_immediately(tmp_path, monkeypatch, official):
    schedule = [fixture_sample('first'), fixture_sample('never-called')]
    fake_port(monkeypatch, {'first': [review_output()]})
    # The first reservation cannot fit; no provider call or second sample is attempted.
    monkeypatch.setattr(mini, 'MAX_USD', '.001')
    mini.run_once(tmp_path, {'max_calls': 4}, schedule, *official, lambda: nullcontext(None))
    record = json.loads((tmp_path / 'first.json').read_text())
    assert record['result']['fallback_reason'] == 'BudgetExhausted'
    assert not record['used_retry']
    assert record['metrics']['agent_calls'] == 0
    assert not (tmp_path / 'never-called.json').exists()


def test_duplicate_claim_cannot_be_overwritten(tmp_path):
    target = tmp_path / 'sample.json'
    assert mini.claim_sample(target, 'sample', 'M1')
    before = target.read_bytes()
    assert not mini.claim_sample(target, 'sample', 'M1')
    assert target.read_bytes() == before


@pytest.mark.parametrize('name', ['..', '../other', '/tmp/other'])
def test_run_name_cannot_escape(name):
    with pytest.raises(SystemExit):
        mini.main(['--run', name])
