from decimal import Decimal

import pytest

from aftercare.agent_kernel.amount_arithmetic import amounts, checkable, amount_findings, grounded_amounts
from aftercare.agent_kernel.contracts import StepProposal, ToolCall, ToolDefinition, ToolKind
from aftercare.agent_kernel.retail_guards import RetailGuards
from agent_kernel.test_retail_guards import result


AUTH = result('find_user_id_by_email', {}, 'user1', 'auth')


def check(text, values=(), extra=()):
    messages = AUTH + [{'role':'tool','content': {'nested':[{'price':v} for v in values]}}] + list(extra)
    return RetailGuards().check(messages, StepProposal(text=text), ())


def test_108_wrong_total_and_feedback_exact_sum():
    checked = check('$244.02 + $52.03 + $50.88 = $347.93', [244.02, 52.03, 50.88])
    assert not checked.allowed and checked.soft and checked.rule_ids == ('amount_arithmetic',)
    assert all(s in checked.feedback for s in ('347.93', '244.02', '52.03', '50.88', '346.93', 'calculate'))


@pytest.mark.parametrize('text', [
    '$244.02 + $52.03 + $50.88 = $346.93',
    '$244.02 minus $52.03 minus $50.88 = $141.11',
    '$52.03 minus $244.02 = $-191.99',
    '$0.10 + $0.20 = $0.30',
])
def test_signed_arithmetic(text):
    assert check(text, [244.02, 52.03, 50.88, 0.1, 0.2]).allowed


def test_grounding_uses_price_fields_all_customer_numbers_and_assistant_history():
    assert check('$1135.47 and $52', [1135.47], [{'role':'user','content':'I paid 52 美元'}]).allowed
    assert not check('$346.93', [], [{'role':'tool','content':'346.93'}]).allowed
    assert check('$347.93', [], [{'role':'assistant','content':'Refund $347.93'}]).allowed
    assert check('$347.93', [], [{'role':'user','content':'Number 347.93'}]).allowed
    assert not check('$347.93', [], [{'role':'assistant','content':'Number 347.93'}]).allowed
    assert not check('$1', [True]).allowed


@pytest.mark.parametrize('total', ['346.93', '141.11', '-141.11'])
def test_order_prices_without_reply_breakdown(total):
    order = result('get_order_details', {'order_id':'o1'}, {
        'order_id':'o1', 'user_id':'user1', 'items':[{'price':244.02}, {'price':52.03}, {'price':50.88}]}, 'order')
    assert check('$'+total, extra=order).allowed


def test_order_duplicate_prices_are_separate_items():
    order = result('get_order_details', {'order_id':'o1'}, {
        'order_id':'o1', 'user_id':'user1', 'items':[{'price':10}, {'price':10}, {'price':3}]}, 'order')
    assert check('$20', extra=order).allowed
    assert check('$10 + $10 = $20', [10]).allowed  # clause c allows x=y=10


def test_currency_markers_only_and_percentage_exclusion():
    assert amounts('$1,234.56, $52, 1,135.47 美元, USD 20, 10 dollars, 22 USD') == [
        Decimal('1234.56'), Decimal('52'), Decimal('1135.47'), Decimal('10'), Decimal('22')]
    assert amounts('347.93, 52%, 5.2％, $20%, 10 美元%') == []
    assert check('347.93, 52%, 5.2％').allowed


def test_more_than_twelve_candidates_skips_only_signed_reply_combination():
    values = list(range(1, 14))
    text = ' '.join('$'+str(v) for v in values) + ' total $9999'
    assert not check(text, values).allowed
    assert not check(text, values[:12]).allowed
    assert not check(('$1 ' * 13) + '$9999', [1]).allowed
    # Pairwise sums remain enabled even above the signed-combination cap.
    assert check(' '.join('$'+str(v) for v in values) + ' total $26', values).allowed
    # 91 is the 13-term sum, but neither a pair nor an eligible <=12-term search.
    assert not check(' '.join('$'+str(v) for v in values) + ' total $91', values).allowed


@pytest.mark.parametrize('text', ['New $212.79, difference $10.84', 'Difference $10.84, new $212.79'])
def test_new_price_and_difference_with_old_price_only_in_tool_records(text):
    assert check(text, ['212.79', '201.95']).allowed
    assert check('$201.95 is $10.84 cheaper', ['212.79', '201.95']).allowed


@pytest.mark.parametrize('text', [
    '$499.29 difference $47.01; $217.06 difference $21.91; total $68.92',
    'Total $68.92; difference $47.01 on $499.29; difference $21.91 on $217.06',
])
def test_sum_of_two_verified_differences_including_total_before_details(text):
    assert check(text, ['499.29', '452.28', '217.06', '195.15']).allowed


def test_customer_bare_budget_and_thousands_commas():
    assert check('$3000', extra=[{'role':'user','content':'My budget is 3,000.'}]).allowed
    assert check('$3066.23 exceeds your budget by $66.23', ['3066.23'],
                 [{'role':'user','content':'Keep it under 3000 please'}]).allowed


def test_tool_provenance_excludes_non_price_fields_and_boolean_values():
    tool = {'role':'tool','content': '''{"zip":28236,"quantity":2,"id":12345,
        "items":[{"PrIcE":12.34,"quantity":7}],"payment_history":[{"AMOUNT":"45.67"}],
        "gift":{"current_BALANCE":89.01},"total_price":123.45,"price_metadata":{"zip":54321},
        "amount_flag":true}'''}
    assert grounded_amounts([tool]) == set(map(Decimal, ['12.34','45.67','89.01','123.45']))
    for value in ('28236', '2', '12345', '7', '54321', '1'):
        assert not check('$'+value, extra=[tool]).allowed
    assert check('$12.34, $45.67, $89.01, $123.45', extra=[tool]).allowed


def test_fabrication_and_unverified_derivations_do_not_validate_each_other():
    assert not check('$9876.54', ['244.02', '52.03', '50.88']).allowed
    assert not check('$17 + $23 = $40', ['100']).allowed


def test_first_bad_amount_triggers_once_then_history_suppresses_repeats():
    first = 'Refund $347.93 for $244.02, $52.03 and $50.88'
    assert not check(first, ['244.02','52.03','50.88']).allowed
    assert check('Confirm $347.93', extra=[{'role':'assistant','content':first}]).allowed
    assert check('Completed $347.93', extra=[{'role':'assistant','content':first},
                                           {'role':'assistant','content':'Confirm $347.93'}]).allowed


def test_feedback_includes_derived_verifiable_amounts_and_exact_total():
    messages = AUTH + [{'role':'tool','content':'{"items":[{"price":212.79},{"price":201.95}]}'}]
    text = 'New $212.79, difference $10.84, other $9999'
    finding = amount_findings(messages, text, {})
    assert finding['uncheckable_amounts'] == ['9999.00']
    assert finding['verifiable_amounts'] == ['212.79', '10.84']
    assert finding['verifiable_sum'] == '223.63'
    feedback = RetailGuards().check(messages, StepProposal(text=text), ()).feedback
    assert all(value in feedback for value in ['9999.00', '212.79', '10.84', '223.63', 'calculate'])


def test_order_sum_above_cap_but_no_large_signed_search():
    values = list(range(1, 14))
    order = result('get_order_details', {'order_id':'o1'}, {
        'order_id':'o1', 'user_id':'user1', 'items':[{'price':v} for v in values]}, 'order')
    assert check('$91', extra=order).allowed
    assert not check('$90', extra=order).allowed


def test_combination_cap_and_minimum_two_terms():
    assert checkable(Decimal(78), list(map(Decimal, range(1, 13))))
    assert not checkable(Decimal(91), list(map(Decimal, range(1, 14))))
    assert not checkable(Decimal(10), [Decimal(10), Decimal(3)])
    assert checkable(Decimal(0), [Decimal(10), Decimal(10)])


def test_unauthenticated_and_tool_proposals_do_not_check_amounts():
    guard = RetailGuards()
    assert guard.check([], StepProposal(text='Email or name and ZIP? $9999'), ()).allowed
    proposal = StepProposal(calls=(ToolCall(id='p', name='calculate', arguments={'expression':'347.93'}),))
    tools = (ToolDefinition(name='calculate', kind=ToolKind.READ),)
    assert guard.check(AUTH, proposal, tools).allowed


def test_calculate_result_is_a_source_but_other_bare_tool_numbers_are_not():
    from aftercare.agent_kernel.amount_arithmetic import grounded_amounts
    from decimal import Decimal
    call = lambda i, name: {'role': 'assistant', 'content': None,
                            'tool_calls': [{'id': i, 'function': {'name': name, 'arguments': '{}'}}]}
    messages = [call('c1', 'calculate'), {'role': 'tool', 'tool_call_id': 'c1', 'content': '14.06'},
                call('c2', 'get_order_details'), {'role': 'tool', 'tool_call_id': 'c2', 'content': '77.77'},
                call('c3', 'calculate'), {'role': 'tool', 'tool_call_id': 'c3', 'content': '9.99', 'error': True}]
    assert grounded_amounts(messages) == {Decimal('14.06')}
