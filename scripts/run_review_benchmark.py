"""Single-attempt E1 runner: exclusive lock, durable per-sample claims, shared $10 ceiling."""
import argparse
import ast
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from aftercare.agent_kernel.contracts import StepProposal,ToolCall,GuardResult,architectures
from aftercare.agent_kernel.review import ProposalReviewer, public_view, REVIEW_INSTRUCTION
from aftercare.agent_kernel.retail_guards import facts
from tau_official_budget import Budget,RowTransport,AGENT
from tau_official_summary import call_receipts
from refactor_baseline import ROOT

DATA=ROOT/'.runtime/e1-review/e1-candidates-v3.json'
RUN=ROOT/'.runtime/e1-review/run-v1'


def dump(path,data):
    encoded=json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)


def literal_constant(path,name):
    tree=ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            value=node.value
            if isinstance(value,ast.Call) and isinstance(value.func,ast.Attribute) and value.func.attr=='strip':
                return ast.literal_eval(value.func.value).strip()
            return ast.literal_eval(value)
    raise ValueError(name)


def request_data(sample,policy):
    messages=public_view(sample['messages'])
    # Preserve original positions used for evidence IDs; never send labels/source metadata.
    proposal=StepProposal(calls=(ToolCall(**{k:sample['proposal'][k] for k in ('id','name','arguments')}),))
    state=facts(messages)
    guard=GuardResult(allowed=True,confirmed_facts={'authenticated_user':state.user_id})
    source=ROOT/'.runtime/tau-official-source/src/tau2/agent/llm_agent.py'
    instruction=literal_constant(source,'AGENT_INSTRUCTION')
    template=literal_constant(source,'SYSTEM_PROMPT')
    guidance=literal_constant(ROOT/'scripts/tau_official_agents_legacy.py','EXECUTION_GUIDANCE')
    executor=[{'role':'system','content':template.format(agent_instruction=instruction,domain_policy=policy)+'\n\n'+guidance},*messages]
    return messages,proposal,guard,executor


class Port:
    def __init__(self,row):self.row=row
    def generate(self,*,role,assignment,messages,tools,stage):
        response=self.row.completion(model=assignment.model,messages=messages,tools=None,tool_choice=None,
            aftercare_role=role,call_name=stage,reasoning_effort=assignment.reasoning)
        return StepProposal(text=response.choices[0].message.content or '')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['development','holdout'],required=True)
    parser.add_argument('--live',action='store_true');parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    data=json.loads(DATA.read_text());samples=[s for s in data['samples'] if s['split']==args.split]
    if args.limit is not None:
        if args.split=='holdout':raise SystemExit('Holdout cannot be cherry-picked with --limit')
        samples=samples[:args.limit]
    schedule=[(s,mode) for s in samples for mode in ('M1','S2')]
    manifest={'dataset_sha256':sha256(DATA.read_bytes()).hexdigest(),'prompt_sha256':sha256(REVIEW_INSTRUCTION.encode()).hexdigest(),
        'code_sha256':{str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in [Path(__file__).resolve(),ROOT/'backend/src/aftercare/agent_kernel/review.py',ROOT/'scripts/tau_official_budget.py']},
        'model':AGENT,'max_usd':'10','no_retries':True,'split':args.split,'schedule':[(s['id'],m) for s,m in schedule]}
    if not args.live:print(json.dumps(manifest,indent=2));return
    if not (ROOT/'.runtime/e1-review/label-audit-approved.json').exists():raise SystemExit('Label/input audit must be completed before calls')
    audit=json.loads((ROOT/'.runtime/e1-review/label-audit-approved.json').read_text())
    if audit['dataset_sha256']!=manifest['dataset_sha256']:raise SystemExit('Audit dataset mismatch')
    RUN.mkdir(exist_ok=True)
    with (RUN/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        freeze=RUN/(args.split+'-manifest.json')
        if freeze.exists() and json.loads(freeze.read_text())!=manifest:raise SystemExit('Frozen manifest mismatch')
        if not freeze.exists():dump(freeze,manifest)
        from decimal import Decimal
        journal=RUN/'calls.jsonl';reservations,settlements=call_receipts(journal)
        budget=Budget(1000,'10',journal)
        budget.calls=max(reservations,default=0)
        budget.committed=sum((Decimal(settlements[k]['actual_usd']) if k in settlements else Decimal(v['reserve_usd'])) for k,v in reservations.items())
        from openai import OpenAI
        key=json.loads((ROOT/'.runtime/model-secret.json').read_text())['api_key']
        limits={'max_agent_calls_per_run':1,'max_user_calls_per_run':0,'max_review_calls_per_run':0,'max_output_tokens_per_call':1000}
        with OpenAI(api_key=key,base_url='https://api.openai.com/v1',max_retries=0,timeout=60) as client:
            for sample,mode in schedule:
                key=sample['id']+'-'+mode;target=RUN/(key+'.json')
                if target.exists():continue  # Started/unknown never retried.
                # Preclaim is durable, so interruption cannot silently cause a duplicate paid call.
                with target.open('x') as f:
                    json.dump({'id':sample['id'],'mode':mode,'status':'started'},f);f.flush();os.fsync(f.fileno())
                row=RowTransport(key,limits,budget,client)
                reviewer=ProposalReviewer(Port(row))
                messages,proposal,guard,executor=request_data(sample,data['policy'])
                result=reviewer.review(architectures()[mode],proposal,messages,data['policy'],guard,executor)
                dump(target,{'id':sample['id'],'mode':mode,'task_id':sample['task_id'],'kind':sample['kind'],'split':args.split,
                    'expected':sample['expected'],'status':'complete','result':result.model_dump(mode='json'),
                    'correct':result.verdict==sample['expected'],'metrics':row.metrics(),'trace':reviewer.trace[-1].model_dump(mode='json')})
                print(key,result.verdict,result.fallback_reason or '',flush=True)
                if result.fallback_reason=='BudgetExhausted':break

if __name__=='__main__':main()
