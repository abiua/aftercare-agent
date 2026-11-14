"""Offline E2 dependency inventory and focused recorded-step regressions; no model/network port."""
from pathlib import Path
from hashlib import sha256
import json
from collections import Counter
from types import SimpleNamespace
from aftercare.agent_kernel.contracts import StepProposal, ToolCall, ToolDefinition, ToolKind, GuardResult, architectures
from aftercare.agent_kernel.dependencies import effect, ITEMS, ADDRESS, ORDER_WRITES
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected
from aftercare.agent_kernel.review import ProposalReviewer
from aftercare.agent_kernel.experts import ExpertService

ROOT=Path(__file__).resolve().parents[1]


def public(messages):
    rows=[]
    for m in messages:
        row={'role':m['role'],'content':m.get('content')}
        if m['role']=='tool':row.update(tool_call_id=m['id'],error=m.get('error',False))
        if m.get('tool_calls'):
            row['tool_calls']=[{'id':c['id'],'function':{'name':c['name'],'arguments':json.dumps(c['arguments'])}} for c in m['tool_calls']]
        rows.append(row)
    return rows


def main():
    summary=json.loads((ROOT/'evidence/architecture_refactor/e2_final.json').read_text())
    inventory=[];counts={};fixtures={}
    for arm in ('S1','S2','S1c','M1','M2'):
        count=Counter()
        for task, outcomes in summary['per_task'].items():
            key=f'{task}-{arm}'
            base=ROOT/'.runtime'/('kernel-e2-20261113.repair' if key in summary['repair']['attempts'] else 'kernel-e2-20261113')
            path=base/'official'/f'{key}.json';trace=json.loads(path.read_text());steps=[]
            for index,message in enumerate(trace['messages']):
                for call in message.get('tool_calls') or []:
                    proposal=StepProposal(calls=(ToolCall(id=call['id'],name=call['name'],arguments=call['arguments']),))
                    spec=effect(proposal)
                    if spec:steps.append({'message_index':index,'tool':call['name'],'effect':spec})
            future=[]
            for step in steps:
                for message in trace['messages'][step['message_index']+1:]:
                    for call in message.get('tool_calls') or []:
                        if call['arguments'].get('order_id')==step['effect']['order_id'] and call['name'] in step['effect']['blocks']:
                            future.append({'first':step['tool'],'later':call['name']})
            count['tasks']+=1
            count['dependency_sensitive_tasks']+=bool(steps)
            count['successful_dependency_sensitive_tasks']+=bool(steps) and outcomes[arm]=='success'
            count['inventory_required_successful_tasks']+=bool(steps) and outcomes[arm]=='success' and arm in ('S2','M1','M2')
            inventory.append({'task':task,'arm':arm,'outcome':outcomes[arm],'source':str(path.relative_to(ROOT)),
                'source_sha256':sha256(path.read_bytes()).hexdigest(),'steps':steps,'later_blocked_operations':future,
                'disposition':'needs_annotated_request_inventory; not a measured false block' if steps and arm in ('S2','M1','M2') else 'review_rule_not_applicable'})
            if key in ('27-M1','97-M2'):fixtures[key]=trace
        counts[arm]=dict(count)
    tools=tuple(ToolDefinition(name=n,kind=ToolKind.WRITE) for n in ORDER_WRITES)+(ToolDefinition(name='transfer_to_human_agents',kind=ToolKind.HANDOFF),)
    # Task 97: hand-annotated pending item + address requests grounded in the original customer's utterance.
    trace=fixtures['97-M2'];index=next(i for i,m in enumerate(trace['messages']) if any(c['name']==ITEMS for c in m.get('tool_calls') or []))
    call=trace['messages'][index]['tool_calls'][0];p=StepProposal(calls=(ToolCall(id=call['id'],name=call['name'],arguments=call['arguments']),))
    messages=public(trace['messages'][:index]);origin=next(i for i,m in enumerate(messages) if m['role']=='user');reference={'message_id':f'm{origin}','field':'content','quote':messages[origin]['content']}
    requests=[{'description':'change speaker','operation':ITEMS,'order_id':call['arguments']['order_id'],'item_ids':call['arguments']['item_ids'],'status':'pending','references':[reference]},
        {'description':'change shipping address','operation':ADDRESS,'order_id':call['arguments']['order_id'],'status':'pending','references':[reference]}]
    def port97(**kw):
        return StepProposal(text=json.dumps({'verdict':'allow','requests':requests})) if kw['role']=='reviewer' else p
    port=SimpleNamespace(generate=port97);kernel=ExecutionKernel(architectures()['M1'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),reviewer=ProposalReviewer(port),experts=SimpleNamespace(trace=[],advise=lambda *a:[]))
    try:kernel.step(messages,tools,trace.get('policy',''));raise AssertionError('Conflicting write escaped')
    except GuardRejected:pass
    assert kernel.trace[-1].output['dependency']=='order_dependency_conflict'
    # Task 27: the recorded second review and ignored revision must cause one more generation.
    trace=fixtures['27-M1'];events=trace['private_trace'];reviews=[e for e in events if e['stage']=='proposal_review'];review=reviews[1]['output']
    revisions=[e for e in events if e['stage']=='executor_revision'];handoff=StepProposal.model_validate(revisions[1]['output'])
    index=next(i for i,m in enumerate(trace['messages']) if any(c['name']=='transfer_to_human_agents' for c in m.get('tool_calls') or []));messages=public(trace['messages'][:index]);calls=[]
    def port27(**kw):
        calls.append(kw['stage'])
        if kw['role']=='reviewer':return StepProposal(text=json.dumps(review))
        if calls.count('executor_revision')==2:return StepProposal(text='Would you like to prioritize the boot exchange or the returns?')
        return handoff
    port=SimpleNamespace(generate=port27);kernel=ExecutionKernel(architectures()['M1'],port,guard=SimpleNamespace(check=lambda *a:GuardResult(allowed=True)),reviewer=ProposalReviewer(port),experts=SimpleNamespace(trace=[],advise=lambda *a:[]))
    result=kernel.step(messages,tools,trace.get('policy',''))
    assert result.text and calls.count('executor_revision')==2
    report={'mode':'offline, recorded actions plus explicitly hand-annotated request fixture; no live model',
        'counts':counts,'focused_regressions':{'97-M2':'pending address blocks item write even when reviewer says allow','27-M1':'ignored recorded handoff revision causes second generation'},
        'limitations':'No old full request inventories exist. Scan registers every sensitive success for annotation; it does not measure new model extraction or false-block rate. S1/S1c dependency metadata has no new veto.',
        'rows':inventory}
    target=ROOT/'evidence/architecture_refactor/review_v2_replay.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
