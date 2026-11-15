import json
from analyze_kernel_campaign import analyse, mcnemar, wilson


def campaign(tmp_path, results):
    """results: {arm: {task: (reward, executed tool or None)}}"""
    tasks = sorted({t for arm in results.values() for t in arm})
    schedule = [{'task_id': t, 'architecture': a, 'seed': 42} for a in results for t in tasks]
    (tmp_path / 'rows').mkdir(); (tmp_path / 'official').mkdir()
    (tmp_path / 'manifest.json').write_text(json.dumps(
        {'split': 'train', 'task_ids': tasks, 'architectures': list(results), 'schedule': schedule}))
    lines = []; call_id = 0
    for arm, by_task in results.items():
        for task, outcome in by_task.items():
            if outcome is None:
                continue  # scheduled but never run
            reward, tool = outcome
            key = f'{task}-{arm}'
            (tmp_path / 'rows' / f'{key}.json').write_text(json.dumps(
                {'task_id': task, 'architecture': arm, 'status': 'completed', 'official_reward': reward, 'duration_seconds': 10}))
            messages = [{'role': 'assistant', 'tool_calls': [{'id': 'a', 'name': 'find_user_id_by_email'}]},
                        {'role': 'tool', 'id': 'a', 'content': 'u1', 'execution_status': 'executed'}]
            if tool:
                messages += [{'role': 'assistant', 'tool_calls': [{'id': 'b', 'name': tool}]},
                             {'role': 'tool', 'id': 'b', 'content': '{}', 'execution_status': 'executed'}]
            trace = [{'stage': 'proposal_review', 'output': {'verdict': 'revise', 'fallback_reason': None}}] if arm == 'M1' else []
            (tmp_path / 'official' / f'{key}.json').write_text(json.dumps({'messages': messages, 'private_trace': trace}))
            for actor, cost in (('executor', '0.10'), ('user', '0.01')) + ((('reviewer', '0.02'),) if arm == 'M1' else ()):
                call_id += 1
                lines.append({'call_id': call_id, 'row': key, 'role': 'agent', 'actor': actor, 'model': 'm', 'reserve_usd': '1'})
                lines.append({'call_id': call_id, 'actual_usd': cost})
    (tmp_path / 'calls.jsonl').write_text(''.join(json.dumps(line) + '\n' for line in lines))
    return tmp_path


def test_outcomes_pairs_costs_and_fixed_denominator(tmp_path):
    report = analyse(campaign(tmp_path, {
        'S1': {'1': (1.0, None), '2': (0.0, 'transfer_to_human_agents'), '3': (0.0, None), '4': None},
        'M1': {'1': (1.0, None), '2': (1.0, 'cancel_pending_order'), '3': (0.0, 'cancel_pending_order'), '4': (1.0, None)},
    }))
    s1, m1 = report['arms']['S1'], report['arms']['M1']
    assert s1['outcomes'] == {'success': 1, 'wrong_write': 0, 'handoff': 1, 'authentication_failed': 0, 'inaction': 1, 'unfinished': 1}
    assert s1['tasks'] == m1['tasks'] == 4 and s1['give_up_failures'] == 2 and m1['wrong_writes'] == 1
    pair = report['pairs']['M1_vs_S1']
    assert pair['M1_only'] == ['2', '4'] and pair['S1_only'] == [] and pair['difference'] == 2 and pair['mcnemar_p'] == 0.5
    assert report['hypotheses']['H1']['holds'] and report['hypotheses']['H2'] == {
        'holds': True, 'difference': 2, 'mcnemar_p': 0.5, 'significant_at_0.05': False}
    assert m1['cost_usd_by_actor'] == {'executor': 0.4, 'reviewer': 0.08, 'user': 0.04}
    assert round(m1['agent_cost_usd'], 2) == 0.48 and round(m1['agent_cost_per_success_usd'], 2) == 0.16
    assert m1['review'] == {'triggered': 4, 'revise': 4, 'fallback': 0}
    assert report['complete'] is False and 'S0' not in report['arms']


def test_statistics():
    assert mcnemar(6, 2) == 0.2891 and mcnemar(0, 0) == 1.0 and mcnemar(6, 0) == 0.0312
    assert wilson(29, 40) == [0.5717, 0.8389]


def test_ablation_names_keep_manifest_denominators_and_default_arms(tmp_path):
    from analyze_kernel_campaign import NEW_ARMS
    assert NEW_ARMS == ('S1','S2','S1c','M1','M2')
    report = analyse(campaign(tmp_path, {a:{'1':(1.0, None)} for a in ('M1x','M1r','M2x','M2r')}))
    assert set(report['arms']) == {'M1x','M1r','M2x','M2r'}
    assert all(a['tasks'] == a['success'] == 1 for a in report['arms'].values())


def test_authentication_failure_is_secondary_only(tmp_path):
    root = campaign(tmp_path, {'S1': {'1': (0.0, None)}})
    (root / 'official/1-S1.json').write_text(json.dumps({'messages': []}))
    arm = analyse(root)['arms']['S1']
    assert arm['outcomes']['authentication_failed'] == 1
    assert arm['give_up_failures'] == 0
    assert arm['no_write_failures'] == 1


def test_unsettled_costs_cannot_prove_savings(tmp_path):
    root = campaign(tmp_path, {a: {'1': (1.0, None)} for a in ('S1', 'S1c', 'M2')})
    path = root / 'calls.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()]
    missing = next(e['call_id'] for e in events if e.get('row') == '1-M2')
    path.write_text(''.join(json.dumps(e) + '\n' for e in events
                            if not (e['call_id'] == missing and 'actual_usd' in e)))
    report = analyse(root)
    arm = report['arms']['M2']
    assert arm['known_total_cost_usd'] == 0.01
    assert arm['total_cost_usd'] is None and arm['total_cost_per_success_usd'] is None
    assert arm['unsettled_calls'] == 1 and not arm['cost_complete']
    assert report['hypotheses']['H3']['holds'] is None
    assert report['hypotheses']['H3']['status'] == 'undetermined'


def test_h3_independent_success_branch_and_settled_costs():
    from analyze_kernel_campaign import hypotheses
    def arm(success, cost, unsettled=0):
        return {'success': success, 'total_cost_per_success_usd': cost, 'unsettled_calls': unsettled}
    arms = {'S1': arm(10, 1), 'S1c': arm(7, 0.1), 'M2': arm(10, 0.01, 1)}
    h3 = hypotheses(arms, {})['H3']
    assert h3['holds'] is True and h3['cost_comparison_status'] == 'undetermined'
    arms['S1c'] = arm(10, 0.1)
    arms['M2'] = arm(9, 0.5)
    assert hypotheses(arms, {})['H3']['holds'] is True
    arms['S1']['unsettled_calls'] = 1
    assert hypotheses(arms, {})['H3']['holds'] is None
    arms['M2'] = arm(6, None, 1)
    assert hypotheses(arms, {})['H3']['holds'] is False


def test_historical_costs_keep_unknown_receipts(tmp_path, monkeypatch):
    import analyze_kernel_campaign as analysis
    from decimal import Decimal
    root = tmp_path / '.runtime/tau-official-full'
    root.mkdir(parents=True)
    events = [{'call_id': 1, 'row': '1-strong_single', 'reserve_usd': '1'},
              {'call_id': 1, 'actual_usd': '0.2'},
              {'call_id': 2, 'row': '1-strong_single', 'reserve_usd': '1'}]
    (root / 'calls.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    monkeypatch.setattr(analysis, 'ROOT', tmp_path)
    monkeypatch.setattr(analysis, 'baseline_rows', lambda: iter([
        ({'architecture': 'strong_single', 'task_id': '1', 'official_reward': 1},
         root / 'official/1-strong_single.json', {'messages': []})]))
    repair = root.with_name(root.name + '.repair')
    repair.mkdir()
    (repair / 'calls.jsonl').write_text(
        json.dumps({'call_id': 1, 'row': '1-strong_single', 'reserve_usd': '1'}) + '\n' +
        json.dumps({'call_id': 1, 'actual_usd': '0.1'}) + '\n')
    outcomes, cost, unsettled = analysis.baseline_s0()
    assert outcomes == {'1': 'success'} and cost == Decimal('0.3') and unsettled == 1


def test_repair_preserves_denominator_original_costs_and_unknowns(tmp_path):
    from hashlib import sha256
    from analyze_kernel_campaign import load, merge_repair
    root = campaign(tmp_path, {'S1': {'1': (0.0, None), '2': (1.0, None)}})
    path = root / 'rows/1-S1.json'
    original = json.loads(path.read_text())
    original.update(status='unknown', official_reward=None, seed=42)
    path.write_text(json.dumps(original))
    repair = root.with_name(root.name + '.repair'); repair.mkdir()
    campaign(repair, {'S1': {'1': (1.0, None)}})
    repair_row = repair / 'rows/1-S1.json'
    repaired = json.loads(repair_row.read_text()); repaired['seed'] = 42
    repair_row.write_text(json.dumps(repaired))
    manifest = json.loads((repair / 'manifest.json').read_text())
    manifest.update(repair_of={'manifest_sha256': sha256((root / 'manifest.json').read_bytes()).hexdigest()},
                    original_attempts={'1-S1': {'row_sha256': sha256(path.read_bytes()).hexdigest()}})
    (repair / 'manifest.json').write_text(json.dumps(manifest))
    with (root / 'calls.jsonl').open('a') as f:
        f.write(json.dumps({'call_id': 99, 'row': '1-S1', 'actor': 'executor', 'reserve_usd': '0.5'}) + '\n')
    report = analyse(root, include_repair=True)
    arm = report['arms']['S1']
    assert arm['tasks'] == 2 and arm['success'] == 2
    assert arm['known_total_cost_usd'] == 0.33 and arm['unsettled_calls'] == 1
    assert arm['calls_by_actor'] == {'executor': 4, 'user': 3}
    assert arm['total_cost_per_success_usd'] is None
    assert report['repair']['attempts']['1-S1']['original_status'] == 'unknown'
    assert original == json.loads(path.read_text())
    original['official_reward'] = 0
    path.write_text(json.dumps(original))
    import pytest
    with pytest.raises(ValueError, match='Original row changed'):
        analyse(root, include_repair=True)
