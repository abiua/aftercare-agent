"""v3.2 recovery must never authorize the rejected business action."""
from types import SimpleNamespace

import pytest

from aftercare.agent_kernel.contracts import (
    VERSION, GuardResult, StepProposal, ToolCall, ToolDefinition, ToolKind, architectures,
)
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected
from aftercare.agent_kernel.retail_guards import RetailGuards, facts
from agent_kernel.test_retail_guards import history, result, TOOLS as BASE_TOOLS

TOOLS = BASE_TOOLS + tuple(ToolDefinition(name=name, kind=ToolKind.READ) for name in
                          ('get_user_details', 'get_product_details'))
AUTH = result('find_user_id_by_email', {}, 'user1', 'auth')
GUARD = RetailGuards()


def proposal(name='get_order_details', **args):
    return StepProposal(calls=(ToolCall(id='original', name=name, arguments=args),))


def swap(**overrides):
    return proposal('modify_pending_order_items', **{
        'order_id':'o1', 'item_ids':['i1'], 'new_item_ids':['i2'], 'payment_method_id':'pay1', **overrides})


def kernel_for(action, *, fallback=None, group='S1', guard=GUARD):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return fallback if kwargs['stage']=='executor_guard_fallback' and fallback is not None else action
    reviewer = SimpleNamespace(review=lambda *a: pytest.fail('Recovery must not trigger review'))
    kernel = ExecutionKernel(architectures()[group], SimpleNamespace(generate=generate),
                             guard=guard, reviewer=reviewer)
    return kernel, calls


@pytest.mark.parametrize('group', ['S1', 'S2', 'M2x'])
@pytest.mark.parametrize('kind', ['user', 'product'])
def test_prerequisite_only_scoped_read_no_retry_or_review(kind, group):
    messages, action, name, args = (AUTH, proposal(order_id='o1'), 'get_user_details', {'user_id':'user1'}) if kind=='user' else (
        history()[:6] + history()[8:], swap(), 'get_product_details', {'product_id':'p1'})
    rejected = GUARD.check(messages, action, TOOLS)
    assert not rejected.allowed and rejected.prerequisite is not None
    kernel, generated = kernel_for(action, group=group)
    reply = kernel.step(messages, TOOLS, '')
    assert not reply.text and len(reply.calls)==1
    read, = reply.calls
    assert read.name==name and read.arguments==args and read.id.startswith('guard_read_')
    assert next(t.kind for t in TOOLS if t.name==name)==ToolKind.READ
    assert len(generated)==1 and kernel.last_review is None
    assert kernel.trace[-1].stage=='guard_prerequisite_read'
    assert kernel.trace[-1].output['proposal']==action.model_dump(mode='json', exclude={'private_context'})
    assert GUARD.check(messages, reply, TOOLS).allowed


def test_reads_have_unique_ids_even_after_kernel_recreation():
    action = proposal(order_id='o1')
    kernel, _ = kernel_for(action)
    first = kernel.step(AUTH, TOOLS, '')
    second = kernel.step(AUTH, TOOLS, '')
    assert first.calls[0].id != second.calls[0].id
    # An unrelated earlier call already uses the first generated id; a new kernel must not reuse it.
    prior = AUTH + result('get_order_details', {'order_id':'unknown'}, 'Error: not found', first.calls[0].id)
    new, _ = kernel_for(action)
    assert new.step(prior, TOOLS, '').calls[0].id != first.calls[0].id


def test_successful_reads_not_repeated_and_specific_feedback():
    denied = GUARD.check(history(), proposal(order_id='foreign'), TOOLS)
    assert not denied.allowed and denied.prerequisite is None
    assert all(s in denied.feedback for s in ('foreign', 'user1', 'o1', 'verify'))
    for replacement in ['foreign', 'i3', 'i1']:
        denied = GUARD.check(history(), swap(new_item_ids=[replacement]), TOOLS)
        assert not denied.allowed and denied.prerequisite is None
        assert all(s in denied.feedback for s in (replacement, 'p1', '1 available variants'))
    named = history()
    named[7]['content'] = named[7]['content'].replace('"product_id": "p1"', '"product_id": "p1", "name": "Keyboard"')
    assert 'Keyboard' in GUARD.check(named, swap(new_item_ids=['wrong']), TOOLS).feedback
    kernel, calls = kernel_for(proposal(order_id='foreign'), fallback=StepProposal(text='Please verify your order ID.'))
    assert not kernel.step(history(), TOOLS, '').calls
    assert len(calls)==3 and not any(e.stage=='guard_prerequisite_read' for e in kernel.trace)


def test_failed_or_foreign_profile_read_does_not_suppress_scoped_prerequisite():
    for value in ['Error: unavailable', {'user_id':'other', 'orders':['o1']}]:
        messages = AUTH + result('get_user_details', {'user_id':'user1'}, value, 'failed')
        read = GUARD.check(messages, proposal(order_id='o1'), TOOLS).prerequisite
        assert read.arguments=={'user_id':'user1'}


def test_first_missing_product_in_multi_item_order():
    messages = history()[:6]
    import json
    order = json.loads(messages[5]['content'])
    order['items'].append({'item_id':'j1', 'product_id':'p2', 'price':10})
    messages[5]['content'] = json.dumps(order)
    action = swap(item_ids=['i1','j1'], new_item_ids=['i2','j2'])
    assert GUARD.check(messages, action, TOOLS).prerequisite.arguments=={'product_id':'p1'}
    messages += history()[6:8]
    assert GUARD.check(messages, action, TOOLS).prerequisite.arguments=={'product_id':'p2'}


@pytest.mark.parametrize('name,args,kind', [
    ('get_user_details', {'user_id':'other'}, ToolKind.READ),
    ('get_user_details', {'user_id':'user1','order_id':'foreign'}, ToolKind.READ),
    ('get_user_details', {'user_id':'user1'}, ToolKind.WRITE),
    ('get_product_details', {'product_id':'foreign'}, ToolKind.READ),
    ('cancel_pending_order', {'order_id':'o1'}, ToolKind.WRITE),
    ('get_order_details', {'order_id':'o1'}, ToolKind.READ),
])
def test_engine_refuses_unscoped_or_non_read_prerequisites(name, args, kind):
    read = ToolCall(id='unsafe', name=name, arguments=args)
    guard = SimpleNamespace(check=lambda *a: GuardResult(allowed=False, feedback='blocked', prerequisite=read))
    kernel, _ = kernel_for(proposal(order_id='o1'), guard=guard)
    with pytest.raises(GuardRejected, match='blocked'):
        kernel.step(AUTH, (ToolDefinition(name=name, kind=kind),), '')
    assert not any(e.stage=='guard_prerequisite_read' for e in kernel.trace)


@pytest.mark.parametrize('status,words', [
    ('pending', ('modify the address', 'payment method', 'same-product', 'cancel', 'cannot return or exchange')),
    ('delivered', ('return or exchange', 'cannot modify or cancel')),
    ('cancelled', ('cannot be handled',)),
    ('pending (items modified)', ('cannot be handled',)),
])
def test_status_feedback_preserves_rejection(status, words):
    action = proposal('return_delivered_order_items', order_id='o1', item_ids=['i1']) if status=='pending' else swap()
    denied = GUARD.check(history(status), action, TOOLS)
    assert denied.rule_ids==('order_status',) and not denied.allowed
    assert all(s in denied.feedback for s in ('o1', status, *words))


def test_fallback_empty_tools_strips_calls_and_counts_across_steps():
    action = proposal(order_id='foreign')
    fallback = StepProposal(text='Please check the order number.', calls=action.calls)
    kernel, calls = kernel_for(action, fallback=fallback, group='S2')
    for count in range(1, 4):
        reply = kernel.step(history(), TOOLS, 'policy')
        assert reply.text==fallback.text and not reply.calls
        assert calls[-1]['tools']==() and calls[-1]['stage']=='executor_guard_fallback'
        assert 'customer\'s language' in calls[-1]['messages'][-1]['content']
        assert 'foreign' in calls[-1]['messages'][-1]['content']
        assert kernel.guard_fallback_count==count and kernel.trace[-1].output['fallback_count']==count
    with pytest.raises(GuardRejected, match='Order foreign'):
        kernel.step(history(), TOOLS, 'policy')
    assert len([c for c in calls if c['stage']=='executor_guard_fallback'])==3
    assert kernel.trace[-1].stage=='guard_fallback_limit'


@pytest.mark.parametrize('soft', [False, True])
def test_fallback_rejected_even_for_soft_rule_preserves_original_exception(soft):
    count = 0
    def check(*args):
        nonlocal count
        count += 1
        return GuardResult(allowed=False, soft=soft and count==3,
                           feedback='original reason' if count<3 else 'fallback reason')
    kernel, _ = kernel_for(StepProposal(text='bad'), guard=SimpleNamespace(check=check))
    with pytest.raises(GuardRejected, match='^original reason$'):
        kernel.step([], (), '')
    assert count==3


def test_real_fallback_soft_guard_is_not_bypassed():
    kernel, _ = kernel_for(proposal(order_id='foreign'), fallback=StepProposal(text='I cannot refund $347.93.'))
    with pytest.raises(GuardRejected, match='Order foreign'):
        kernel.step(history(), TOOLS, '')
    assert kernel.trace[-1].output['rule_ids']==['amount_arithmetic']


def test_budget_exhausted_during_fallback_propagates_without_recovery():
    class BudgetExhausted(RuntimeError):
        pass
    def generate(**kwargs):
        if kwargs['stage']=='executor_guard_fallback':
            raise BudgetExhausted('no budget')
        return proposal(order_id='foreign')
    kernel = ExecutionKernel(architectures()['S1'], SimpleNamespace(generate=generate), guard=GUARD)
    with pytest.raises(BudgetExhausted, match='no budget'):
        kernel.step(history(), TOOLS, '')


@pytest.mark.parametrize('read', [
    ToolCall(id='r', name='get_user_details', arguments={'user_id':'user1'}),
    ToolCall(id='r', name='get_product_details', arguments={'product_id':'p1'}),
])
def test_engine_does_not_repeat_a_successful_prerequisite_even_from_custom_guard(read):
    guard = SimpleNamespace(check=lambda *a: GuardResult(allowed=False, feedback='blocked', prerequisite=read))
    kernel, _ = kernel_for(proposal(order_id='foreign'), guard=guard)
    with pytest.raises(GuardRejected, match='blocked'):
        kernel.step(history(), TOOLS, '')
    assert not any(e.stage=='guard_prerequisite_read' for e in kernel.trace)


@pytest.mark.parametrize('routes', ['Email or first and last name and ZIP?', '邮箱或姓名加邮编？'])
def test_followup_auth_question_allowed_only_after_both_routes_in_one_assistant_reply(routes):
    followup = StepProposal(text='What is your last name?')
    assert not GUARD.check([], followup, TOOLS).allowed
    assert GUARD.check([{'role':'assistant','content':routes}], followup, TOOLS).allowed
    assert not GUARD.check([{'role':'user','content':routes}], followup, TOOLS).allowed
    assert not GUARD.check([{'role':'assistant','content':'Email?'},
                           {'role':'assistant','content':'Name and ZIP?'}], followup, TOOLS).allowed
    assert not GUARD.check([{'role':'assistant','content':routes}], proposal(order_id='o1'), TOOLS).allowed


@pytest.mark.parametrize('first,last,zipcode,blocked', [
    ('MEI','kOvAcS','28236',False), ('Other','Kovacs','28236',True),
    ('Mei','Other','28236',True), ('Mei','Kovacs','00000',True),
])
def test_username_hint_tracks_combinations_not_tool_name(first, last, zipcode, blocked):
    messages = [{'role':'user','content':'Username mei_kovacs_8020 ZIP 28236'}]
    messages += result('find_user_id_by_name_zip', {'first_name':first,'last_name':last,'zip':zipcode},
                       'Error: no account', 'lookup')
    state = facts(messages)
    assert 'find_user_id_by_name_zip' in state.attempted
    assert (first.casefold(),last.casefold(),zipcode) in state.attempted_name_zip
    checked = GUARD.check(messages, StepProposal(text='Email or name and ZIP?'), TOOLS)
    assert checked.allowed is not blocked
    if blocked:
        assert checked.soft and checked.rule_ids==('authentication_username',)


@pytest.mark.parametrize('messages,action,allowed,rule', [
    (AUTH, proposal(order_id='o1'), False, 'single_user'),
    (history()[:6], swap(), False, 'item_variant'),
    (history(), swap(), True, None),
    (history(), swap(item_ids=['foreign']), False, 'item_membership'),
    (history(), swap(new_item_ids=['foreign']), False, 'item_variant'),
    (history(), swap(payment_method_id='foreign'), False, 'payment_owner'),
    (history()[:-1], swap(), False, 'confirmation'),
    (history('delivered'), swap(), False, 'order_status'),
    (AUTH, proposal('find_user_id_by_email', email='other'), False, 'single_user'),
    ([], proposal(order_id='o1'), False, 'authentication'),
])
def test_existing_business_decisions_unchanged(messages, action, allowed, rule):
    checked = GUARD.check(messages, action, TOOLS)
    assert checked.allowed==allowed
    assert checked.rule_ids==((rule,) if rule else ())


def test_version_and_optional_contract_field():
    assert VERSION=='aftercare-kernel-v3.2'
    assert GuardResult(allowed=False).prerequisite is None


def test_failed_prerequisite_read_is_not_substituted_again():
    messages = AUTH + result('get_user_details', {'user_id':'user1'}, 'Error: unavailable', 'failed')
    assert GUARD.check(messages, proposal(order_id='o1'), TOOLS).prerequisite is not None
    kernel, calls = kernel_for(proposal(order_id='o1'), fallback=StepProposal(text='Please confirm the order ID.'))
    out = kernel.step(messages, TOOLS, '')
    assert out.calls == () and out.text == 'Please confirm the order ID.'
    assert not any(e.stage=='guard_prerequisite_read' for e in kernel.trace)
    assert [c['stage'] for c in calls][-1] == 'executor_guard_fallback'
