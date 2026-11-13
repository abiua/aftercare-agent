import json
from types import SimpleNamespace
import pytest
from aftercare.agent_kernel.contracts import architectures, StepProposal, ToolCall, ToolDefinition, ToolKind, GuardResult
from aftercare.agent_kernel.review import ProposalReviewer, trigger
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected
from aftercare.agent_kernel.experts import ExpertService

TOOLS=(ToolDefinition(name='write',kind=ToolKind.WRITE),ToolDefinition(name='read',kind=ToolKind.READ),ToolDefinition(name='handoff',kind=ToolKind.HANDOFF))
MESSAGES=[{'role':'user','content':'Return the mug','raw_data':{'private_reasoning':'secret'}}]
PROPOSAL=StepProposal(calls=(ToolCall(id='a',name='handoff'),),private_context=({'reasoning':'hidden'},))
REVISION={'verdict':'revise','issue':'Return still possible','next_step':'Read order status',
    'references':[{'message_id':'m0','field':'content','quote':'Return the mug'}]}


def test_independent_context_excludes_private_and_self_check_has_actual_context():
    seen=[]
    port=SimpleNamespace(generate=lambda **kw:seen.append(kw) or StepProposal(text='{"verdict":"allow"}'))
    reviewer=ProposalReviewer(port)
    for group in ('M1','S2'):
        reviewer.review(architectures()[group],PROPOSAL,MESSAGES,'policy',GuardResult(allowed=True),[{'role':'system','content':'executor instructions'}])
    independent=seen[0]['messages'][1]['content']
    assert 'secret' not in independent and 'hidden' not in independent and 'executor instructions' not in independent
    assert 'hidden' in seen[1]['messages'][1]['content']
    assert seen[0]['messages'][0]==seen[1]['messages'][0]
    assert seen[0]['role']=='reviewer' and seen[1]['role']=='self_check'


@pytest.mark.parametrize('response',['bad JSON','{"verdict":"veto"}',json.dumps({**REVISION,'references':[{'message_id':'missing','quote':'x'}]})])
def test_invalid_review_defaults_allow_with_reason(response):
    reviewer=ProposalReviewer(SimpleNamespace(generate=lambda **kw:StepProposal(text=response)))
    result=reviewer.review(architectures()['M1'],PROPOSAL,MESSAGES,'policy',GuardResult(allowed=True),[])
    assert result.verdict=='allow' and result.fallback_reason


def test_timeout_defaults_allow():
    def timeout(**kw):raise TimeoutError()
    reviewer=ProposalReviewer(SimpleNamespace(generate=timeout))
    assert reviewer.review(architectures()['M1'],PROPOSAL,MESSAGES,'',GuardResult(allowed=True),[]).fallback_reason=='TimeoutError'


def test_one_revision_no_recursive_review_feedback_not_persisted():
    seen=[]
    def generate(**kw):
        seen.append(kw)
        if kw['role']=='reviewer':return StepProposal(text=json.dumps(REVISION))
        if kw['stage']=='executor_revision':return StepProposal(text='Which order is the mug from?')
        return PROPOSAL
    port=SimpleNamespace(generate=generate)
    guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True))
    kernel=ExecutionKernel(architectures()['M1'],port,guard=guard,reviewer=ProposalReviewer(port),experts=ExpertService(port))
    result=kernel.step(MESSAGES,TOOLS,'policy')
    assert result.text=='Which order is the mug from?'
    assert [c['role'] for c in seen]==['executor','reviewer','executor']
    assert len(MESSAGES)==1 and 'One-use' in seen[-1]['messages'][-1]['content']
    seen.clear();kernel.step(MESSAGES,TOOLS,'policy')
    assert 'One-use' not in str(seen[0]['messages'])


def test_revised_invalid_write_cannot_bypass_guard():
    def generate(**kw):
        if kw['role']=='reviewer':return StepProposal(text=json.dumps(REVISION))
        return PROPOSAL if kw['stage']=='executor_step' else StepProposal(calls=(ToolCall(id='b',name='write'),))
    guard=SimpleNamespace(check=lambda m,p,t:GuardResult(allowed=not(p.calls and p.calls[0].name=='write'),feedback='no approval'))
    port=SimpleNamespace(generate=generate)
    kernel=ExecutionKernel(architectures()['M1'],port,guard=guard,reviewer=ProposalReviewer(port),experts=ExpertService(port))
    with pytest.raises(GuardRejected):kernel.step(MESSAGES,TOOLS,'policy')


def test_reads_and_ordinary_replies_make_no_review_call():
    for p in (StepProposal(text='Hello'),StepProposal(calls=(ToolCall(id='c',name='read'),))):
        seen=[]
        port=SimpleNamespace(generate=lambda **kw:seen.append(kw) or p)
        kernel=ExecutionKernel(architectures()['M1'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),reviewer=ProposalReviewer(port),experts=ExpertService(port))
        kernel.step(MESSAGES,TOOLS,'policy')
        assert len(seen)==1
    assert trigger(StepProposal(text='I cannot handle this; transfer to a human agent.'),TOOLS,MESSAGES)=='abandon'
