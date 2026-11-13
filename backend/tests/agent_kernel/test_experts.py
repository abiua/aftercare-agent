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


def test_all_six_groups_run_offline_with_expected_roles_and_models():
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
        cheap='deepseek-flash' if name in {'S1c','M2'} else 'gpt-6.1-sol'
        expected=[('fact_expert','gpt-6.1-sol')] if name in {'M1','M2'} else []
        expected.append(('executor',cheap))
        if name in {'M1','M2'}:expected.append(('reviewer','gpt-6.1-sol'))
        if name=='S2':expected.append(('self_check','gpt-6.1-sol'))
        assert roles==expected,name
        assert [e.role for e in kernel.trace if e.model]==[r for r,_ in expected]


def test_tolerant_parsing_of_real_model_shapes():
    from aftercare.agent_kernel.experts import parse
    fenced='```json\n{"candidates":[{"id":"i2","price":"7.00","note":"extra"}],"claims":[{"bad":1}],"unresolved":"one thing","policy_quotes":null,"answer":"x"}\n```'
    result=parse(fenced)
    assert result.candidates[0]['id']=='i2' and result.unresolved==('one thing',) and result.policy_quotes==()
    with pytest.raises(ValueError):parse('no json here')
