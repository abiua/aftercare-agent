"""Deterministic E1 source extraction; labels never enter reviewer payloads."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from refactor_baseline import ROOT, baseline_rows, WRITES, TRANSFER
from aftercare.agent_kernel.retail_guards import facts


def text(message):
    value=message.get('content') or ''
    try:
        parsed=json.loads(value)
        return parsed.get('message',value) if isinstance(parsed,dict) else value
    except ValueError:return value


def build():
    source=list(baseline_rows())
    task_ids=sorted({r['task_id'] for r,_,_ in source},key=int)
    splits={task:('development' if i%2==0 else 'holdout') for i,task in enumerate(task_ids)}
    priority={'strong_single':0,'staged_single':1,'multi_agent':2}
    samples=[];excluded=[];seen=set();challenges=[]
    db=json.loads((ROOT/'.runtime/tau-official-source/data/tau2/domains/retail/db.json').read_text())
    payment_owners={payment:uid for uid,user in db['users'].items() for payment in user.get('payment_methods',{})}
    policy=(ROOT/'.runtime/tau-official-source/data/tau2/domains/retail/policy.md').read_text()
    def add(row,path,history,call,kind,mutation,verdict):
        canonical=json.dumps([row['task_id'],kind,mutation,call['name'],call['arguments']],sort_keys=True)
        if canonical in seen:return
        seen.add(canonical)
        samples.append({'id':sha256(canonical.encode()).hexdigest()[:20],'task_id':row['task_id'],
            'split':splits[row['task_id']],'kind':kind,'mutation':mutation,'expected':verdict,
            'source':str(path.relative_to(ROOT)),'source_sha256':sha256(path.read_bytes()).hexdigest(),
            'source_architecture':row['architecture'],'messages':deepcopy(history),'proposal':deepcopy(call),
            'executor_private_context_available':False})
    for row,path,trace in sorted(source,key=lambda item:(int(item[0]['task_id']),priority[item[0]['architecture']])):
        history=[]
        for message in trace['messages']:
            for call in message.get('tool_calls') or []:
                if row['official_reward']!=1:
                    if call['name']==TRANSFER:
                        challenges.append({'task_id':row['task_id'],'architecture':row['architecture'],
                            'source':str(path.relative_to(ROOT)),'message_index':len(history),'expected':None,
                            'reason':'Official failure does not establish that handoff was wrong.'})
                    continue
                if call['name']==TRANSFER:
                    add(row,path,history,call,'correct_abandon','none','allow');continue
                if call['name'] not in WRITES:continue
                add(row,path,history,call,'correct_write','none','allow')
                add(row,path,history,{'id':call['id'],'name':TRANSFER,'arguments':{'summary':'I cannot handle this request.'}},
                    'wrong_abandon','replace_write_with_handoff','revise')
                # Remove the latest customer confirmation (and later customer turns).
                last_reply=next((i for i in range(len(history)-1,-1,-1) if history[i]['role']=='assistant' and text(history[i]) and not history[i].get('tool_calls')),None)
                if last_reply is not None and any(m['role']=='user' for m in history[last_reply+1:]):
                    truncated=[m for i,m in enumerate(history) if not(i>last_reply and m['role']=='user')]
                    add(row,path,truncated,call,'wrong_write','remove_confirmation','revise')
                state=facts(history)
                args=call['arguments'];order=state.orders.get(args.get('order_id'),{})
                methods=state.user.get('payment_methods',{})
                if args.get('payment_method_id') and methods:
                    other=deepcopy(call);other['arguments']['payment_method_id']=next(payment for payment,owner in payment_owners.items() if owner!=state.user_id and payment not in methods)
                    add(row,path,history,other,'wrong_write','foreign_payment','revise')
                elif args.get('payment_method_id'):
                    excluded.append({'task_id':row['task_id'],'mutation':'foreign_payment','reason':'No visible user payment catalogue'})
                if call['name']=='cancel_pending_order':
                    # Only mutate when the latest customer confirmation names the exact reason.
                    recent=[text(m).lower() for m in history[last_reply+1:] if m['role']=='user'] if last_reply is not None else []
                    if any(args['reason'] in s for s in recent):
                        other=deepcopy(call);other['arguments']['reason']='ordered by mistake' if args['reason']=='no longer needed' else 'no longer needed'
                        add(row,path,history,other,'wrong_write','cancel_reason','revise')
                    else:excluded.append({'task_id':row['task_id'],'mutation':'cancel_reason','reason':'No explicit reason in latest customer text; avoid ambiguous label'})
                if args.get('new_item_ids'):
                    old_items={i['item_id']:i for i in order.get('items',[])}
                    original=old_items.get(args['item_ids'][0],{})
                    variants=state.products.get(original.get('product_id'),{}).get('variants',{})
                    # A merely different available variant need not be wrong. Require exact customer selection.
                    selected=args['new_item_ids'][0]
                    customer_explicit=any(selected in text(m) for m in history if m['role']=='user')
                    alternative=next((k for k,v in variants.items() if k!=selected and k!=args['item_ids'][0] and v.get('available')),None)
                    if customer_explicit and alternative:
                        other=deepcopy(call);other['arguments']['new_item_ids'][0]=alternative
                        add(row,path,history,other,'wrong_write','same_product_variant','revise')
                    else:excluded.append({'task_id':row['task_id'],'mutation':'same_product_variant','reason':'No explicit customer variant ID; avoid assuming other variants unacceptable'})
                    other_product=next((k for pid,p in state.products.items() if pid!=original.get('product_id') for k,v in p.get('variants',{}).items() if v.get('available')),None)
                    if other_product:
                        other=deepcopy(call);other['arguments']['new_item_ids'][0]=other_product
                        add(row,path,history,other,'wrong_write','different_product','revise')
                    else:excluded.append({'task_id':row['task_id'],'mutation':'different_product','reason':'No other product visible'})
            history.append(message)
    return {'version':'e1-candidates-v2','status':'requires label and input audit before paid run',
        'policy':policy,'splits':splits,'samples':samples,'exclusions':excluded,'real_abandon_challenges':challenges,
        'limitations':['No complete executor reasoning saved; self-check uses saved system/public context only.',
            'Official success is a source selection rule, not independent proof that every intermediate write was correct.',
            'Foreign-payment mutations use an actual other-user method; its owner is never supplied to the model.']}


if __name__=='__main__':
    result=build();folder=ROOT/'.runtime/e1-review';folder.mkdir(exist_ok=True)
    target=folder/(result['version']+'.json')
    encoded=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if target.exists() and target.read_text()!=encoded:raise SystemExit('Existing candidates differ; use an explicit version change')
    target.write_text(encoded)
    from collections import Counter
    print(json.dumps({'samples':len(result['samples']),'by_kind':dict(Counter(s['kind'] for s in result['samples'])),
        'by_split':dict(Counter(s['split'] for s in result['samples'])),'exclusions':len(result['exclusions'])}))
