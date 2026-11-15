"""Offline v3 specialist adapter and recorded-message scheduling checks."""
from hashlib import sha256
import json
from unittest.mock import patch

import pytest
from tau2.domains.retail.environment import get_environment

from aftercare.agent_kernel.contracts import ToolCall
from replay_expert_staleness import statistics
from tau_official_agents import PRIVATE_READ_TOOLS, create_agent


def tool_messages(ident, name, arguments, result):
    return [{'role': 'assistant', 'tool_calls': [{'id': ident, 'name': name, 'arguments': arguments}]},
            {'role': 'tool', 'id': ident, 'content': json.dumps(result)}]


def test_private_read_real_official_tools_preserve_database_and_public_history():
    env = get_environment()
    agent = create_agent('M1', env.get_tools(), env.get_policy(), 'unused')
    state = agent.get_init_state()
    history_before = state.model_dump()
    db = env.tools.db
    digest = lambda: sha256(json.dumps(db.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
    before = digest()
    order_id = next(iter(db.orders))
    product_id = next(iter(db.products))
    for name, args, key in [('get_order_details', {'order_id': order_id}, 'order_id'),
                            ('get_product_details', {'product_id': product_id}, 'product_id')]:
        result = agent.private_read(ToolCall(id='read', name=name, arguments=args))
        assert json.loads(result)[key] == args[key]
        assert digest() == before
    for name in ('cancel_pending_order', 'calculate', 'missing'):
        with pytest.raises(ValueError, match='private read path'):
            agent.private_read(ToolCall(id='bad', name=name))
    # An allowlisted name must also exist in this environment's provided tools.
    agent.tools = [tool for tool in agent.tools if tool.name != 'get_order_details']
    with pytest.raises(ValueError):
        agent.private_read(ToolCall(id='missing', name='get_order_details', arguments={'order_id': order_id}))
    assert digest() == before and state.model_dump() == history_before
    assert agent.kernel.experts.read_tools == PRIVATE_READ_TOOLS
    assert agent.private_trace == []


def test_specialist_private_read_is_logged_without_entering_official_history():
    from tau2.data_model.message import AssistantMessage, ToolCall as OfficialCall, UserMessage
    env = get_environment()
    db = env.tools.db
    order = next(iter(db.orders.values()))
    user_id = order.user_id
    user = db.users[user_id]
    product_id = order.items[0].product_id
    agent = create_agent('M1', env.get_tools(), env.get_policy(), 'unused')
    history = []
    for ident, name, arguments in [('auth', 'find_user_id_by_email', {'email': user.email}),
                                    ('user', 'get_user_details', {'user_id': user_id}),
                                    ('order', 'get_order_details', {'order_id': order.order_id})]:
        c = OfficialCall(id=ident, name=name, arguments=arguments)
        history += [AssistantMessage(role='assistant', tool_calls=[c]), env.get_response(c)]
    history += [UserMessage(role='user', content='Please find the cheapest option.')]
    state = agent.get_init_state(history)
    public_call = OfficialCall(id='public_product', name='get_product_details', arguments={'product_id': product_id})
    state.messages.append(AssistantMessage(role='assistant', tool_calls=[public_call]))
    result = env.get_response(public_call)
    before = sha256(json.dumps(db.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
    private_calls = []
    def fake(**kwargs):
        if kwargs['aftercare_role'] == 'fact_expert':
            private_calls.append(kwargs)
            if len(private_calls) == 1:
                return AssistantMessage(role='assistant', tool_calls=[OfficialCall(
                    id='private_order', name='get_order_details', arguments={'order_id': order.order_id})])
            return AssistantMessage(role='assistant', content='{}')
        assert kwargs['aftercare_role'] == 'executor'
        return AssistantMessage(role='assistant', content='Which option would you like?')
    with patch('tau_official_agents.generate', fake):
        reply, state = agent.generate_next_message(result, state)
    assert reply.content == 'Which option would you like?'
    assert not any(getattr(m, 'id', None) == 'private_order' or
                   any(c.id == 'private_order' for c in getattr(m, 'tool_calls', None) or []) for m in state.messages)
    event = next(e for e in agent.private_trace if e['stage'] == 'expert_fact_expert')
    assert event['output']['reads'] == 1
    assert event['output']['read_log'][0]['status'] == 'executed'
    assert json.loads(event['output']['read_log'][0]['result'])['order_id'] == order.order_id
    assert sha256(json.dumps(db.model_dump(mode='json'), sort_keys=True).encode()).hexdigest() == before


def test_replay_statistics_count_refreshes_and_drop_once():
    messages = [{'role': 'user', 'content': 'cheapest please'},
                *tool_messages('auth', 'find_user_id_by_email', {'email': 'a'}, 'ann_1'),
                *tool_messages('user', 'get_user_details', {'user_id': 'ann_1'},
                               {'user_id': 'ann_1', 'orders': []})]
    for n in range(7):
        messages += tool_messages(str(n), 'get_product_details', {'product_id': str(n)},
                                  {'product_id': str(n), 'variants': {str(n): {'price': n, 'available': True}}})
    messages += [{'role': 'assistant', 'content': 'Which one?'},
                 {'role': 'user', 'content': 'cheapest again'}, {'role': 'assistant', 'content': 'Done'}]
    result = statistics(messages)
    assert result['has_signal'] and result['old_consults'] == 2 and result['new_consults'] == 5
    assert result['stale_dropped'] == 1
    assert result['changed'] == [f'product:{n}' for n in range(1, 6)]
    assert [e['event'] for e in result['events']] == ['consult', *(['refresh'] * 4), 'expert_stale_dropped']
    assert result['events'][-1]['changed'] == ['product:5']
    assert statistics(messages) == result
    assert statistics([{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Hi'}])['new_consults'] == 0
