import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from aftercare.agent_kernel.contracts import (StepProposal, ToolCall, ToolDefinition, ToolKind,
    GuardResult, CustomerRequest, architectures)
from aftercare.agent_kernel.dependencies import *
from aftercare.agent_kernel.review import ProposalReviewer
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected

MESSAGES=[{'role':'user','content':'Change item A and send order #W1 to New York.'},
 {'role':'assistant','content':'Changing the item will prevent changing the address. Prioritize item A and leave the address for a human?'},
 {'role':'user','content':'Yes, prioritize item A and leave the address for a human.'}]
REF={'message_id':'m0','field':'content','quote':MESSAGES[0]['content']}
CONSENT={'message_id':'m2','field':'content','quote':MESSAGES[2]['content']}
TOOLS=tuple(ToolDefinition(name=n,kind=ToolKind.WRITE) for n in ORDER_WRITES)+(ToolDefinition(name='handoff',kind=ToolKind.HANDOFF),)

def proposal(name=ITEMS, **args):
    return StepProposal(calls=(ToolCall(id='x',name=name,arguments={'order_id':'#W1',**args}),))

def req(operation=ITEMS, **extra):
    return {'description':'customer request','operation':operation,'order_id':'#W1','item_ids':['A'] if operation in ITEM_OPERATIONS else [],'status':'pending','references':[REF],**extra}

def reviewer_result(port, p=None):
    reviewer=ProposalReviewer(port)
    result=reviewer.review(architectures()['S2'],p or proposal(item_ids=['A']),MESSAGES,'policy',GuardResult(allowed=True),[])
    return reviewer,result

@pytest.mark.parametrize('operation,blocked',[(ITEMS,ADDRESS),(EXCHANGE,RETURN),(RETURN,EXCHANGE),(CANCEL,ADDRESS),(PAYMENT,PAYMENT)])
def test_effect_sources_and_current_request_not_self_blocked(operation,blocked):
    p=proposal(operation,item_ids=['A'])
    own=CustomerRequest(**req(operation))
    assert conflict(p,[own]) is None
    if blocked==operation:
        assert effect(p)['basis'].startswith('tool payment-history')
    else:
        other=CustomerRequest(**req(blocked,item_ids=['B'] if blocked in ITEM_OPERATIONS else []))
        assert conflict(p,[own,other])=='order_dependency_conflict'
    assert effect(proposal(ADDRESS)) is None

@pytest.mark.parametrize('status',['pending','unavailable'])
def test_model_allow_cannot_override_dependency(status):
    port=SimpleNamespace(generate=lambda **kw:StepProposal(text=json.dumps({'verdict':'allow','requests':[req(),req(ADDRESS,status=status)]})))
    _,result=reviewer_result(port)
    assert result.verdict == ('revise' if status == 'pending' else 'allow')
    assert result.blocking_reason == ('order_dependency_conflict' if status == 'pending' else None)

@pytest.mark.parametrize('status',['withdrawn','deferred'])
def test_release_requires_customer_evidence(status):
    for consent in ([],[CONSENT]):
        port=SimpleNamespace(generate=lambda **kw:StepProposal(text=json.dumps({'verdict':'allow','requests':[req(),req(ADDRESS,status=status,resolution_references=consent)]})))
        reviewer,result=reviewer_result(port)
        assert result.verdict=='allow' and not result.fallback_reason
        assert len(result.requests)==(2 if consent else 1)
        assert bool(reviewer.trace[-1].output['dropped_requests'])== (not bool(consent))


def test_same_operation_other_items_not_exempted():
    assert conflict(proposal(item_ids=['A']),[CustomerRequest(**req()),CustomerRequest(**req(item_ids=['B']))])=='order_dependency_conflict'


@pytest.mark.parametrize('operation', [ITEMS, EXCHANGE])
@pytest.mark.parametrize('item_ids, expected', [
    (['A', 'B'], True),
    (['B'], False),
    (['A', 'B', 'C'], False),
    (['A', 'B', 'B'], False),
    (['A', 'A', 'B'], False),
])
def test_fulfills_replacement_item_ids(operation, item_ids, expected):
    call = proposal(operation, item_ids=['A'], new_item_ids=['B']).calls[0]
    assert fulfills(CustomerRequest(**req(operation, item_ids=item_ids)), call) is expected


def test_fulfills_subtracts_replacement_ids_by_occurrence():
    call = proposal(EXCHANGE, item_ids=['A'], new_item_ids=['A', 'B', 'B']).calls[0]
    assert fulfills(CustomerRequest(**req(EXCHANGE, item_ids=['A', 'A', 'B', 'B'])), call)


@pytest.mark.parametrize('extra', [{'operation': ITEMS}, {'order_id': '#W2'}])
def test_fulfills_replacements_still_requires_exact_operation_and_order(extra):
    call = proposal(EXCHANGE, item_ids=['A'], new_item_ids=['B']).calls[0]
    request = CustomerRequest(**{**req(EXCHANGE, item_ids=['A', 'B']), **extra})
    assert not fulfills(request, call)


@pytest.mark.parametrize('item_ids, expected', [
    (['A'], True), ([], False), (['A', 'B'], False), (['A', 'A'], False),
])
def test_fulfills_return_without_replacements_unchanged(item_ids, expected):
    call = proposal(RETURN, item_ids=['A']).calls[0]
    assert fulfills(CustomerRequest(**req(RETURN, item_ids=item_ids)), call) is expected


@pytest.mark.parametrize('operation', [ITEMS, EXCHANGE])
def test_replacement_request_does_not_self_block_or_fall_back(operation):
    p = proposal(operation, item_ids=['A'], new_item_ids=['B'])
    own = req(operation, item_ids=['A', 'B'])
    assert conflict(p, [CustomerRequest(**own)]) is None
    port = SimpleNamespace(generate=lambda **kw: StepProposal(
        text=json.dumps({'verdict': 'allow', 'requests': [own]})))
    reviewer, result = reviewer_result(port, p)
    assert result.verdict == 'allow'
    assert result.blocking_reason is None and result.fallback_reason is None
    assert not reviewer.trace[-1].output['failures']


@pytest.mark.parametrize('filename', ['101-M1-m26.json', '102-M1-m26.json'])
def test_recorded_review_request_fulfills_current_call(filename):
    path = Path(__file__).resolve().parents[3] / '.runtime/review-v2-minitest/run-v1' / filename
    if not path.is_file():
        pytest.skip(f'Recorded review evidence missing: {filename}')
    data = json.loads(path.read_text())
    p = StepProposal.model_validate(data['proposal'])
    requests = [CustomerRequest.model_validate(raw) for raw in data['result']['requests']]
    assert any(fulfills(request, p.calls[0]) for request in requests)
    assert conflict(p, requests) is None


def test_missing_list_retried_then_records_unavailable():
    calls=[]
    port=SimpleNamespace(generate=lambda **kw:calls.append(kw) or StepProposal(text='{"verdict":"allow"}'))
    reviewer,result=reviewer_result(port)
    assert len(calls)==2 and result.fallback_reason=='ValueError' and result.blocking_reason is None
    assert reviewer.trace[-1].output['failures'][0]['phase']=='request_inventory'


def test_fences_extra_fields_and_one_bad_reference_are_tolerated():
    raw={'verdict':'revise','issue':'question','next_step':'Ask which order','extra':'ignored','references':[REF,{'message_id':'m99','quote':'fake'}]}
    reviewer,result=reviewer_result(SimpleNamespace(generate=lambda **kw:StepProposal(text='```json\n'+json.dumps(raw)+'\n```')),proposal('handoff'))
    assert result.verdict=='revise' and len(result.references)==1 and not reviewer.trace[-1].output['failures']


def test_budget_failure_never_retries_or_regenerates():
    class BudgetExhausted(Exception):pass
    calls=[]
    def generate(**kw):
        calls.append(kw['role'])
        if kw['role']=='executor':return proposal(item_ids=['A'])
        raise BudgetExhausted('budget reached')
    port=SimpleNamespace(generate=generate);kernel=ExecutionKernel(architectures()['S2'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),reviewer=ProposalReviewer(port))
    assert kernel.step(MESSAGES,TOOLS,'policy')==proposal(item_ids=['A'])
    assert kernel.trace[-1].output['status']=='review_unavailable'
    assert calls==['executor','self_check']


def run_kernel(initial, revisions, review_json=None, broken=False):
    seen=[];it=iter(revisions)
    def generate(**kw):
        seen.append(kw)
        if kw['role']=='self_check':
            if broken:raise TimeoutError('timed out')
            return StepProposal(text=json.dumps(review_json))
        return next(it) if kw['stage']=='executor_revision' else initial
    port=SimpleNamespace(generate=generate);kernel=ExecutionKernel(architectures()['S2'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),reviewer=ProposalReviewer(port))
    return kernel,seen


def test_two_revisions_soft_opinion_may_remain_but_recorded():
    p=proposal('handoff');review={'verdict':'revise','issue':'premature','next_step':'Ask priority','references':[REF]}
    kernel,seen=run_kernel(p,[p,p],review)
    assert kernel.step(MESSAGES,TOOLS,'policy')==p
    assert sum(c['stage']=='executor_revision' for c in seen)==2
    assert kernel.trace[-1].output['status']=='opinion_unresolved'


def test_second_revision_can_ask_question_after_ignored_first():
    p=proposal('handoff');review={'verdict':'revise','issue':'premature','next_step':'Ask priority','references':[REF]}
    kernel,seen=run_kernel(p,[p,StepProposal(text='Which action should I do first?')],review)
    assert kernel.step(MESSAGES,TOOLS,'policy').text
    assert len([c for c in seen if c['stage']=='executor_revision'])==2


def test_changed_parameters_do_not_bypass_dependency():
    p=proposal(item_ids=['A'],new_item_ids=['B']);q=proposal(item_ids=['A'],new_item_ids=['C'])
    kernel,seen=run_kernel(p,[q,q],{'verdict':'allow','requests':[req(),req(ADDRESS)]})
    with pytest.raises(GuardRejected):kernel.step(MESSAGES,TOOLS,'policy')
    assert kernel.trace[-1].output['dependency']=='order_dependency_conflict'

@pytest.mark.parametrize('name',[ADDRESS,'handoff'])
def test_unavailable_review_nonlocking_actions_allowed(name):
    p=proposal(name);kernel,seen=run_kernel(p,[],broken=True)
    assert kernel.step(MESSAGES,TOOLS,'policy')==p
    assert len(seen)==3


def test_unavailable_review_regenerates_once_and_accepts_locking_write():
    p=proposal(item_ids=['A']);kernel,seen=run_kernel(p,[p],broken=True)
    assert kernel.step(MESSAGES,TOOLS,'policy')==p
    assert kernel.trace[-1].output['status']=='review_unavailable'
    feedback=seen[-1]['messages'][-1]['content']
    assert ADDRESS in feedback and 'Program write effects' in feedback
    assert len([c for c in seen if c['stage']=='executor_revision'])==1


def test_history_is_two_public_reviews_and_scoped_to_conversation():
    captured=[];port=SimpleNamespace(generate=lambda **kw:captured.append(kw) or StepProposal(text='{"verdict":"allow"}'))
    reviewer=ProposalReviewer(port)
    for _ in range(4):
        reviewer.review(architectures()['M1'],proposal('handoff'),MESSAGES,'policy',GuardResult(allowed=True),[{'secret':'executor context'}])
        reviewer.record_resolution('allowed')
    payload=json.loads(captured[-1]['messages'][1]['content'])
    assert len(payload['previous_reviews'])==2 and 'secret' not in str(payload)
    assert payload['previous_reviews'][0]['resolution']=='allowed'
    reviewer.review(architectures()['M1'],proposal('handoff'),[{'role':'user','content':'Different customer'}],'',GuardResult(allowed=True),[])
    assert json.loads(captured[-1]['messages'][1]['content'])['previous_reviews']==[]


def test_completed_claim_without_successful_tool_record_rejected():
    port=SimpleNamespace(generate=lambda **kw:StepProposal(text=json.dumps({'verdict':'allow','requests':[req(),req(ADDRESS,status='completed',resolution_references=[CONSENT])]})))
    reviewer,r=reviewer_result(port)
    assert r.verdict=='allow' and len(r.requests)==1 and not r.fallback_reason
    assert 'Completed request' in reviewer.trace[-1].output['dropped_requests'][0]['reason']


@pytest.mark.parametrize('bad',[None, 'bad', {'status':'bogus'}, req(ADDRESS,references=[])])
def test_bad_entries_dropped_without_retrying_valid_review(bad):
    seen=[]
    port=SimpleNamespace(generate=lambda **kw:seen.append(kw) or StepProposal(text=json.dumps({'verdict':'allow','requests':[bad,req()]})))
    reviewer,r=reviewer_result(port)
    assert len(seen)==1 and r.verdict=='allow' and len(r.requests)==1
    assert reviewer.trace[-1].output['dropped_requests'][0]['index']==0


def test_bad_current_request_makes_review_unavailable():
    seen=[]
    port=SimpleNamespace(generate=lambda **kw:seen.append(kw) or StepProposal(text=json.dumps({'verdict':'allow','requests':[req(references=[])]})))
    reviewer,r=reviewer_result(port)
    assert len(seen)==2 and r.fallback_reason=='ValueError'
    assert len(reviewer.trace[-1].output['dropped_requests'])==2


def test_dropped_entry_does_not_erase_valid_pending_conflict():
    port=SimpleNamespace(generate=lambda **kw:StepProposal(text=json.dumps({'verdict':'allow','requests':[None,req(),req(ADDRESS)]})))
    reviewer,r=reviewer_result(port)
    assert r.blocking_reason=='order_dependency_conflict'
    assert len(reviewer.trace[-1].output['dropped_requests'])==1
