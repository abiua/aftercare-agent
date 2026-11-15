from types import SimpleNamespace

import pytest

from aftercare.agent_kernel.contracts import GuardResult, ReviewResult, StepProposal, TraceEvent, architectures
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected
from aftercare.agent_kernel.retail_guards import RetailGuards
from agent_kernel.test_retail_guards import result


AUTH = result('find_user_id_by_email', {}, 'user1', 'auth')


def reviewer(verdict='allow'):
    seen = []
    def review(architecture, proposal, messages, policy, guard_result, inputs):
        seen.append(guard_result)
        return ReviewResult(verdict=verdict, issue='Check amount', next_step='Recalculate')
    return SimpleNamespace(review=review, seen=seen, record_resolution=lambda _: None,
                           trace=[TraceEvent(role='reviewer', stage='proposal_review',
                                             input_sha256='fixture', output={})])


@pytest.mark.parametrize('group', ['S2', 'M1x', 'M2x'])
def test_second_soft_failure_continues_to_review_with_allowed_facts(group):
    calls = []
    proposal = StepProposal(text="I cannot refund $347.93.")
    model = SimpleNamespace(generate=lambda **kw: calls.append(kw) or proposal)
    review = reviewer()
    kernel = ExecutionKernel(architectures()[group], model, guard=RetailGuards(), reviewer=review)
    assert kernel.step(AUTH + [{'role':'user', 'content':'Refund please'}], (), '') == proposal
    assert len(calls) == 2 and 'calculate' in calls[1]['messages'][-1]['content']
    assert review.seen == [GuardResult(allowed=True, confirmed_facts={'authenticated_user':'user1'})]
    event, = [e for e in kernel.trace if e.stage == 'guard_soft_unresolved']
    assert event.output['rule_ids'] == ['amount_arithmetic']
    assert event.output['guard_result']['allowed'] is True
    assert kernel.trace[-1].stage == 'proposal_review'


def test_username_hint_is_soft_even_if_executor_keeps_asking():
    proposal = StepProposal(text='What is your full name?')
    messages = [{'role':'user', 'content':'Username mei_kovacs_8020 ZIP 28236'}]
    guard = RetailGuards()
    assert guard.check(messages, proposal, ()).soft is True
    kernel = ExecutionKernel(architectures()['S1'], SimpleNamespace(generate=lambda **_: proposal), guard=guard)
    assert kernel.step(messages, (), '') == proposal
    assert kernel.trace[-1].stage == 'guard_soft_unresolved'
    assert kernel.trace[-1].output['rule_ids'] == ['authentication_username']


@pytest.mark.parametrize('soft', [False, True])
def test_post_review_guard_soft_continues_but_hard_rejects(soft):
    original = StepProposal(text='I cannot handle this.')
    revised = StepProposal(text='Would you like a refund of $347.93?')
    responses = iter([original, revised])
    results = iter([GuardResult(allowed=True),
                    GuardResult(allowed=False, soft=soft, rule_ids=('fixture',), feedback='check')])
    guard = SimpleNamespace(check=lambda *args: next(results))
    kernel = ExecutionKernel(architectures()['S2'], SimpleNamespace(generate=lambda **_: next(responses)),
                             guard=guard, reviewer=reviewer('revise'))
    if soft:
        assert kernel.step([{'role':'user','content':'Help'}], (), '') == revised
        event, = [e for e in kernel.trace if e.stage == 'guard_soft_unresolved']
        assert event.output['source_stage'] == 'post_review_guard'
        assert event.output['rule_ids'] == ['fixture']
    else:
        with pytest.raises(GuardRejected, match='check'):
            kernel.step([{'role':'user','content':'Help'}], (), '')


def test_soft_recheck_cannot_hide_a_hard_rejection():
    guard = SimpleNamespace(
        check=lambda *args: GuardResult(allowed=False, soft=True, rule_ids=('hint',)),
        check_without_soft=lambda *args: GuardResult(allowed=False, feedback='hard rule'))
    kernel = ExecutionKernel(architectures()['S1'], SimpleNamespace(generate=lambda **_: StepProposal(text='x')), guard=guard)
    with pytest.raises(GuardRejected, match='hard rule'):
        kernel.step([], (), '')
