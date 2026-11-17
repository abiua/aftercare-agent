"""Offline v3.2 replay, frozen entry barriers and exact-prefix alignment."""
import socket
from types import SimpleNamespace

import pytest

from aftercare.agent_kernel import contracts
from aftercare.agent_kernel.contracts import StepProposal
import replay_guard_recovery as replay


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Guard replay must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.mark.parametrize('scope', ['confirm', 'ablate'])
def test_frozen_campaign_entry_rejects_v32(scope, monkeypatch, tmp_path):
    import run_tau_official_campaign as runner
    import kernel_e3_protocol as e3
    import kernel_e4_protocol as e4
    protocol = e3 if scope=='confirm' else e4
    monkeypatch.setattr(contracts, 'VERSION', 'aftercare-kernel-v3.2')
    monkeypatch.setattr(runner, 'initialize_official', lambda: pytest.fail('Version rejection must precede SDK initialization'))
    with pytest.raises(SystemExit, match=f'{scope} requires aftercare-kernel-v3.1'):
        runner.new_campaign(SimpleNamespace(scope=scope, architectures=','.join(protocol.ARMS),
                                            task_ids=None, directory=str(tmp_path/'absent'), **protocol.LIMITS))
    assert not (tmp_path/'absent').exists()


def event(stage, output):
    return {'stage':stage, 'output':output}


def test_alignment_keeps_retry_prefix_and_normalizes_review_revision():
    first = StepProposal(text='first')
    second = StepProposal(text='accepted')
    revised = StepProposal(text='final')
    trajectory = {'messages':[
        {'role':'assistant','content':'greeting'}, {'role':'user','content':'help'},
        {'role':'assistant','content':'accepted'}, {'role':'user','content':'again'},
        {'role':'assistant','content':'final'}], 'private_trace':[
        event('executor_step', first.model_dump()), event('guard', {'allowed':False}),
        event('executor_step', second.model_dump()), event('guard', {'allowed':True}),
        event('executor_step', first.model_dump()), event('guard', {'allowed':True}),
        event('proposal_review', {'verdict':'revise'}), event('executor_revision', revised.model_dump()),
        event('post_review_guard', {'allowed':True}), event('review_resolution', {'status':'proposal_changed_not_semantically_verified'})]}
    groups = replay.align(trajectory)
    assert len(groups)==2 and [g['message_index'] for g in groups]==[2,4]
    assert [len(g['checks']) for g in groups]==[2,2]
    assert groups[0]['messages']==trajectory['messages'][:2]
    trajectory['messages'][4]['content'] = 'different'
    with pytest.raises(ValueError, match='mismatch'):
        replay.align(trajectory)


def test_terminal_rejection_uses_recorded_prefix_without_invented_assistant():
    bad = StepProposal(text='bad')
    trajectory = {'messages':[{'role':'user','content':'help'}], 'private_trace':[
        event('executor_step', bad.model_dump()), event('guard', {'allowed':False}),
        event('executor_step', bad.model_dump()), event('guard', {'allowed':False})]}
    group, = replay.align(trajectory)
    assert group['messages']==trajectory['messages'] and group['message_index']==1
    trajectory['private_trace'] += [event('executor_step', bad.model_dump())]
    with pytest.raises(ValueError, match='retry boundary'):
        replay.align(trajectory)


def test_output_creation_never_overwrites_existing_evidence(tmp_path):
    target = tmp_path/'report.json'
    replay.write_report({'test':True}, target)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        replay.write_report({'test':False}, target)
    assert target.read_bytes()==before


def test_all_frozen_terminations_and_guard_judgments_replayed():
    report = replay.replay()
    assert report['summary']['guard_terminations']==22
    assert report['summary']['categories']=={
        'prerequisite_read':17,'text_fallback':4,'still_terminated':0,'original_reply_allowed':1}
    assert report['summary']['specific_feedback']==4
    assert report['uncovered']==[]
    assert report['judgments']['replayed_judgments']==report['judgments']['recorded_judgments']==4784
    assert report['judgments']['coverage']==1
    # Change 5 deliberately introduces this soft reminder for an UNTRIED name combination.
    changed_allowed = [m for m in report['mismatches'] if m['old_allowed']]
    assert len(changed_allowed)==1
    changed, = changed_allowed
    assert (changed['directory'],changed['key'],changed['trace_index'])==('.runtime/kernel-e3/run-v1','67-M2',10)
    assert changed['new_rules']==['authentication_username']
    assert all(m['old_rules']==['authentication_reply'] for m in report['mismatches'] if not m['old_allowed'])
    for recovery in report['recoveries']:
        if recovery['category']=='prerequisite_read':
            assert recovery['model_stages']==['executor_step']
            assert recovery['prerequisite']['name'] in {'get_user_details','get_product_details'}
        elif recovery['category']=='text_fallback':
            assert recovery['model_stages']==['executor_step','executor_step','executor_guard_fallback']
            assert recovery['specific_feedback'] and not recovery['returned_proposal']['calls']
        else:
            assert recovery['model_stages']==['executor_step']
            assert recovery['new_guard']['allowed']
