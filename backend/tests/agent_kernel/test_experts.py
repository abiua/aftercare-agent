import json
from types import SimpleNamespace
import pytest
from aftercare.agent_kernel.contracts import (architectures, ExpertRequest, GuardResult, StepProposal, ToolCall,
    ToolDefinition, ToolKind)
from aftercare.agent_kernel.engine import ExecutionKernel
from aftercare.agent_kernel.experts import ExpertService, signals
from aftercare.agent_kernel.review import ProposalReviewer

TOOLS=(ToolDefinition(name='get_order_details',kind=ToolKind.READ),ToolDefinition(name='get_user_details',kind=ToolKind.READ),
    ToolDefinition(name='cancel_pending_order',kind=ToolKind.WRITE))
POLICY='An order can only be cancelled if its status is pending.\nExchange is for delivered orders.'


def call(i,name,args,result):
    return [{'role':'assistant','content':None,'tool_calls':[{'id':i,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},
        {'role':'tool','tool_call_id':i,'content':result if isinstance(result,str) else json.dumps(result)}]


def conversation(request,status='pending',orders=('#W1',)):
    rows=[{'role':'user','content':'Hi'},*call('a','find_user_id_by_email',{'email':'x'},'ann_1'),
        *call('b','get_user_details',{'user_id':'ann_1'},{'user_id':'ann_1','orders':list(orders),'payment_methods':{}})]
    for n,order in enumerate(orders):
        rows+=call(f'o{n}','get_order_details',{'order_id':order},{'order_id':order,'user_id':'ann_1','status':status,
            'items':[{'item_id':'i1','product_id':'p1','price':10}]})
    rows+=call('p','get_product_details',{'product_id':'p1'},{'product_id':'p1','variants':{
        'i1':{'available':True,'price':10},'i2':{'available':True,'price':7}}})
    return [*rows,{'role':'user','content':request}]


def answer(**fields):return StepProposal(text=json.dumps(fields))


def test_no_signal_means_no_specialist_call():
    port=SimpleNamespace(generate=lambda **kw:pytest.fail('must not call'))
    service=ExpertService(port)
    assert service.advise(architectures()['M1'],conversation('Please change the colour of my mug.'),TOOLS,POLICY)==[]
    assert service.advise(architectures()['M1'],[{'role':'user','content':'cheapest one please'}],TOOLS,POLICY)==[]


def test_constraint_triggers_once_and_drops_ungrounded_output():
    seen=[]
    port=SimpleNamespace(generate=lambda **kw:seen.append(kw) or answer(
        candidates=[{'id':'i2','satisfies':['cheapest'],'fails':[]},{'id':'made_up'}],policy_quotes=['not in policy']))
    service=ExpertService(port);messages=conversation('Switch it to the cheapest option.')
    for _ in range(2):findings=service.advise(architectures()['M2'],messages,TOOLS,POLICY)
    assert len(seen)==1 and seen[0]['role']=='fact_expert' and seen[0]['assignment'].model=='gpt-6.1-sol'
    assert seen[0]['tools']==() and 'policy' not in json.loads(seen[0]['messages'][1]['content'])
    assert [c['id'] for c in findings[0]['candidates']]==['i2'] and findings[0]['policy_quotes']==[]
    assert service.trace[0].reason=='dropped_ungrounded:2'


def test_status_conflict_goes_to_policy_expert_with_verified_quote():
    port=SimpleNamespace(generate=lambda **kw:answer(unresolved=['Not feasible'],
        policy_quotes=['An order can only be cancelled if its status is pending.']))
    service=ExpertService(port)
    findings=service.advise(architectures()['M1'],conversation('Please cancel order #W1.',status='delivered'),TOOLS,POLICY)
    assert findings[0]['role']=='policy_expert' and len(findings[0]['policy_quotes'])==1


def test_multi_order_signal_and_consult_cap():
    assert signals(conversation('I need help with my orders.',orders=('#W1','#W2')))==[]
    names=[s[0] for s in signals(conversation('I need help with #W1 and #W2.',orders=('#W1','#W2')))]
    assert names==['multi_order']
    calls=[]
    service=ExpertService(SimpleNamespace(generate=lambda **kw:calls.append(1) or answer()),max_consults=1)
    service.advise(architectures()['M1'],conversation('Cheapest for both orders, cancel #W1',status='delivered',orders=('#W1','#W2')),TOOLS,POLICY)
    assert len(calls)==1


def test_out_of_scope_read_rejected_and_read_limit_enforced():
    outputs=[StepProposal(calls=(ToolCall(id='1',name='get_order_details',arguments={'order_id':'#W9'}),)),
        StepProposal(calls=(ToolCall(id='2',name='cancel_pending_order',arguments={'order_id':'#W1'}),)),
        StepProposal(calls=(ToolCall(id='3',name='get_order_details',arguments={'order_id':'#W1'}),)),
        answer(candidates=[{'id':'fresh'}])]
    offered=[];executed=[]
    port=SimpleNamespace(generate=lambda **kw:offered.append(kw['tools']) or outputs.pop(0))
    service=ExpertService(port,read=lambda c:executed.append(c.name) or '{"item":"fresh"}')
    request=ExpertRequest(role='fact_expert',question='q',allowed_tools=('get_order_details',),user_id='ann_1',order_ids=('#W1',),max_reads=3)
    result=service.consult(architectures()['M1'],request,conversation('x'),TOOLS,POLICY)
    assert executed==['get_order_details'] and len(service.rejections)==2
    assert [t.name for t in offered[0]]==['get_order_details'] and offered[-1]==()
    assert result.stop_reason=='read_limit' and result.candidates[0]['id']=='fresh'


@pytest.mark.parametrize('failure',[TimeoutError(),ValueError('bad')])
def test_specialist_failure_never_blocks_the_executor(failure):
    def generate(**kw):
        if kw['role']=='executor':return StepProposal(text='Which option would you like?')
        raise failure
    port=SimpleNamespace(generate=generate)
    kernel=ExecutionKernel(architectures()['M1'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),
        reviewer=ProposalReviewer(port),experts=ExpertService(port))
    assert kernel.step(conversation('cheapest please'),TOOLS,POLICY).text
    assert [e.role for e in kernel.trace][:2]==['fact_expert','executor']


def test_all_groups_run_offline_with_expected_roles_and_models():
    for name,group in architectures().items():
        roles=[]
        def generate(**kw):
            roles.append((kw['role'],kw['assignment'].model))
            if kw['role']=='executor':return StepProposal(calls=(ToolCall(id='h',name='transfer'),))
            return StepProposal(text='{"verdict":"allow"}' if kw['role'] in {'reviewer','self_check'} else '{}')
        port=SimpleNamespace(generate=generate)
        tools=(*TOOLS,ToolDefinition(name='transfer',kind=ToolKind.HANDOFF))
        kernel=ExecutionKernel(group,port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),
            reviewer=ProposalReviewer(port),experts=ExpertService(port))
        kernel.step(conversation('cheapest please'),tools,POLICY)
        cheap='deepseek-flash' if name in {'S1c','M2','M2x','M2r'} else 'gpt-6.1-sol'
        expected=[('fact_expert','gpt-6.1-sol')] if name in {'M1','M2','M1r','M2r'} else []
        expected.append(('executor',cheap))
        if name in {'M1','M2','M1x','M2x'}:expected.append(('reviewer','gpt-6.1-sol'))
        if name=='S2':expected.append(('self_check','gpt-6.1-sol'))
        assert roles==expected,name
        assert [e.role for e in kernel.trace if e.model]==[r for r,_ in expected]


def test_tolerant_parsing_of_real_model_shapes():
    from aftercare.agent_kernel.experts import parse
    fenced='```json\n{"candidates":[{"id":"i2","price":"7.00","note":"extra"}],"claims":[{"bad":1}],"unresolved":"one thing","policy_quotes":null,"answer":"x"}\n```'
    result=parse(fenced)
    assert result.candidates[0]['id']=='i2' and result.unresolved==('one thing',) and result.policy_quotes==()
    with pytest.raises(ValueError):parse('no json here')


PRODUCT_TOOL = ToolDefinition(name='get_product_details', kind=ToolKind.READ)
PRODUCT = {'product_id': 'p2', 'variants': {'i3': {'price': 12.34, 'available': False}}}


def product_messages(product=PRODUCT):
    return call('new', 'get_product_details', {'product_id': product['product_id']}, product)


@pytest.mark.parametrize('read_tools,expected', [(None, ['get_order_details', 'get_user_details', 'get_product_details']),
                                                ({'get_product_details', 'cancel_pending_order'}, ['get_product_details']),
                                                (set(), [])])
def test_read_tools_intersection_and_default_limits(read_tools, expected):
    offered = []
    service = ExpertService(SimpleNamespace(generate=lambda **kw: offered.append(kw['tools']) or answer()),
                            read=lambda c: '{}', read_tools=read_tools)
    service.advise(architectures()['M1'], conversation('cheapest'), (*TOOLS, PRODUCT_TOOL), POLICY)
    assert [t.name for t in offered[0]] == expected
    assert service.max_consults == 5
    assert ExpertRequest(role='fact_expert', question='q', allowed_tools=(), user_id='ann_1', order_ids=()).max_reads == 5
    with pytest.raises(ValueError):
        ExpertRequest(role='fact_expert', question='q', allowed_tools=(), user_id='ann_1', order_ids=(), max_reads=9)


def test_all_calls_have_results_and_read_log_including_errors_and_limit():
    calls = tuple(ToolCall(id=str(n), name=name, arguments=args) for n, (name, args) in enumerate([
        ('get_product_details', {'product_id': 'p2'}),
        ('get_order_details', {'order_id': '#W9'}),
        ('cancel_pending_order', {'order_id': '#W1'}),
        ('get_user_details', {'user_id': 'other'}),
        ('get_order_details', {'order_id': '#W1'}),
        ('get_product_details', {'product_id': 'p3'}),
    ]))
    outputs = [StepProposal(text='Checking records', calls=calls), answer(candidates=[{'id': 'i3'}])]
    inputs, executed = [], []
    def read(c):
        executed.append(c)
        if c.name == 'get_order_details':
            raise RuntimeError('lookup failed')
        return json.dumps(PRODUCT)
    port = SimpleNamespace(generate=lambda **kw: inputs.append(kw) or outputs.pop(0))
    service = ExpertService(port, read=read, read_tools={'get_order_details', 'get_product_details', 'get_user_details'})
    result = service.advise(architectures()['M1'], conversation('cheapest'), (*TOOLS, PRODUCT_TOOL), POLICY)
    event = service.trace[-1]
    assert [r['status'] for r in event.output['read_log']] == ['executed', 'rejected', 'rejected', 'rejected', 'error', 'limit']
    assert event.output['reads'] == 1 and len(executed) == 2 and len(service.rejections) == 3
    assert event.output['read_log'][0] == {'tool': calls[0].name, 'arguments': calls[0].arguments,
                                         'status': 'executed', 'result': json.dumps(PRODUCT)}
    assert event.output['read_log'][4]['result'] == 'Error: lookup failed'
    assert event.output['read_log'][5]['result'] == 'Rejected: read limit reached.'
    assistant, *results = inputs[1]['messages'][2:]
    assert assistant['content'] == 'Checking records' and len(assistant['tool_calls']) == 6
    assert [r['tool_call_id'] for r in results] == [c.id for c in calls]
    assert [r['content'] for r in results] == [r['result'] for r in event.output['read_log']]
    assert inputs[1]['tools'] == () and result[0]['stop_reason'] == 'read_limit'
    assert result[0]['candidates'][0]['origin'] == 'specialist_read'


def test_calls_after_tools_withdrawn_are_unavailable():
    c = ToolCall(id='a', name='get_order_details', arguments={'order_id': '#W1'})
    port = SimpleNamespace(generate=lambda **kw: StepProposal(calls=(c,)))
    service = ExpertService(port, read=lambda c: '{}')
    request = ExpertRequest(role='fact_expert', question='q', allowed_tools=('get_order_details',),
                            user_id='ann_1', order_ids=('#W1',), max_reads=1)
    assert service.consult(architectures()['M1'], request, conversation('x'), TOOLS, POLICY).stop_reason == 'unavailable'
    assert service.trace[-1].reason == 'ValueError: Specialist exceeded its read limit'
    assert service.trace[-1].output['reads'] == 1


def test_new_records_refresh_replace_and_consult_budget_counts_refreshes():
    calls = []
    port = SimpleNamespace(generate=lambda **kw: calls.append(kw) or answer(unresolved=[str(len(calls))]))
    service = ExpertService(port, max_consults=2)
    messages = conversation('cheapest')
    first = service.advise(architectures()['M1'], messages, TOOLS, POLICY)
    original_hash = service.trace[-1].input_sha256
    messages += product_messages()
    second = service.advise(architectures()['M1'], messages, TOOLS, POLICY)
    assert first[0]['unresolved'] == ['1'] and second[0]['unresolved'] == ['2']
    assert len(service.cache) == 1 and service.consults == 2
    assert service.trace[-1].output['refresh_of'] == original_hash
    assert service.trace[-1].output['changed'] == ['product:p2']
    assert all('seen' not in finding for finding in first + second)
    # Both an existing key's changed content and a new signal compete for the exhausted budget.
    changed = {'product_id': 'p2', 'variants': {'i3': {'price': 13, 'available': True}}}
    messages += product_messages(changed) + [{'role': 'user', 'content': 'cheapest please again'}]
    for _ in range(3):
        assert service.advise(architectures()['M1'], messages, TOOLS, POLICY) == []
    assert len(calls) == 2 and len(service.trace) == 3
    event = service.trace[-1]
    assert event.stage == 'expert_stale_dropped' and event.role == 'fact_expert'
    assert event.output == {'signal': 'constrained_choice', 'changed': ['product:p2']}


def test_public_record_identical_to_private_read_does_not_refresh():
    outputs = [StepProposal(calls=(ToolCall(id='p', name='get_product_details', arguments={'product_id': 'p2'}),)),
               answer(candidates=[{'id': 'i3'}])]
    service = ExpertService(SimpleNamespace(generate=lambda **kw: outputs.pop(0)), read=lambda c: json.dumps(PRODUCT))
    messages = conversation('cheapest')
    first = service.advise(architectures()['M1'], messages, (*TOOLS, PRODUCT_TOOL), POLICY)
    # JSON formatting/key order do not affect the content digest.
    messages += product_messages(json.loads(json.dumps(PRODUCT, sort_keys=True)))
    assert service.advise(architectures()['M1'], messages, (*TOOLS, PRODUCT_TOOL), POLICY) == first
    assert service.consults == 1


def test_order_change_refreshes_even_when_original_status_signal_disappears():
    service = ExpertService(SimpleNamespace(generate=lambda **kw: answer()))
    messages = conversation('cancel #W1', status='delivered')
    service.advise(architectures()['M1'], messages, TOOLS, POLICY)
    messages += call('updated', 'get_order_details', {'order_id': '#W1'},
                     {'order_id': '#W1', 'user_id': 'ann_1', 'status': 'pending', 'items': []})
    assert signals(messages) == []
    service.advise(architectures()['M1'], messages, TOOLS, POLICY)
    assert service.consults == 2 and service.trace[-1].output['changed'] == ['order:#W1']


def test_prices_recomputed_and_difference_requires_loaded_replacement():
    outputs = [StepProposal(calls=(ToolCall(id='p', name='get_product_details', arguments={'product_id': 'p2'}),)),
               answer(candidates=[{'id': 'i2', 'price': 999, 'price_difference': 999, 'replaces': 'i1'},
                                  {'id': 'i3', 'price': 999, 'price_difference': 999, 'replaces': 'i1'},
                                  {'id': 'i2', 'price': 7, 'price_difference': 99},
                                  {'id': 'i2', 'replaces': 'unknown', 'price_difference': 99},
                                  {'id': '#W1', 'price': 99, 'price_difference': 99},
                                  {'id': 'invented', 'price': 99}])]
    service = ExpertService(SimpleNamespace(generate=lambda **kw: outputs.pop(0)), read=lambda c: json.dumps(PRODUCT))
    findings = service.advise(architectures()['M1'], conversation('cheapest'), (*TOOLS, PRODUCT_TOOL), POLICY)
    a, b, c, d, e = findings[0]['candidates']
    assert a['price'] == 7.0 and a['price_difference'] == -3.0 and a['available'] is True
    assert b['price'] == 12.34 and b['price_difference'] == 2.34 and b['available'] is False
    assert a['origin'] == 'conversation' and b['origin'] == 'specialist_read'
    assert a['replaces'] == b['replaces'] == 'i1'
    assert 'price_difference' not in c and 'replaces' not in d and 'price_difference' not in d
    assert 'price' not in e and 'price_difference' not in e and e['origin'] == 'conversation'
    assert service.trace[-1].reason == 'dropped_ungrounded:1;price_corrected:2'


@pytest.mark.parametrize('price,reason', [(7.005, None), (7.0051, 'price_corrected:1')])
def test_price_correction_threshold_counts_candidates_once(price, reason):
    service = ExpertService(SimpleNamespace(generate=lambda **kw: answer(candidates=[{'id': 'i2', 'price': price}])))
    findings = service.advise(architectures()['M1'], conversation('cheapest'), TOOLS, POLICY)
    assert findings[0]['candidates'][0]['price'] == 7.0
    assert service.trace[-1].reason == reason
