"""Audit frozen E1 candidates using original public evidence, without model calls."""
from hashlib import sha256
import json
from collections import Counter
from refactor_baseline import ROOT,WRITES
from build_review_benchmark import text
from run_review_benchmark import DATA
from aftercare.agent_kernel.retail_guards import facts

HANDOFF_NOTES={
 '12':'Customer explicitly rejected policy-valid original-card refund and requested human.',
 '36':'Post-write order has inconsistent item metadata; one-time modification prevents correction.',
 '51':'Return already requested; customer asks human for unavailable alternate instruction delivery.',
 '53':'Return already requested; customer asks human for unavailable alternate instruction delivery.',
 '65':'New purchase/cross-product exchange outside tool scope; alternatives already discussed.',
 '70':'Exchange completed; remaining question is charge timing absent from available policy/results.',
 '100':'Post-write luggage metadata contradicts catalogue; no second item modification allowed.',
}


def main():
 data=json.loads(DATA.read_text());out=[];correct={}
 for s in data['samples']:
  assert sha256((ROOT/s['source']).read_bytes()).hexdigest()==s['source_sha256']
  assert s['split']==data['splits'][s['task_id']]
  if s['kind']=='correct_write':
   state=facts(s['messages']);a=s['proposal']['arguments'];order=state.orders.get(a.get('order_id'),{})
   users=[m for m in s['messages'] if m['role']=='user'];reply=text(users[-1]).lower()
   assert any(w in reply for w in ('yes','yeah','please','sounds good')),s['id']
   if a.get('order_id'):
    assert order.get('user_id')==state.user_id
    assert order['status']==('delivered' if s['proposal']['name'].startswith(('return','exchange')) else 'pending')
   if a.get('item_ids'):
    items={i['item_id']:i for i in order['items']};assert set(a['item_ids'])<=set(items)
    for old,new in zip(a['item_ids'],a.get('new_item_ids',[])):
     assert new!=old and state.products[items[old]['product_id']]['variants'][new]['available']
   canonical=json.dumps([s['task_id'],s['proposal']['name'],a],sort_keys=True)
   correct[canonical]=s
 for s in data['samples']:
  kind=s['kind'];mutation=s['mutation'];a=s['proposal']['arguments'];state=facts(s['messages'])
  note=''
  if kind=='correct_write':note='Original successful run; authenticated ownership/status/items and customer confirmation checked; reviewer still checks semantic details.'
  elif kind=='correct_abandon':
   note=HANDOFF_NOTES[s['task_id']]
   if s['task_id'] in {'36','100'}:
    assert any(o.get('status')=='pending (item modified)' for o in state.orders.values())
  elif kind=='wrong_abandon':
   assert any(v['task_id']==s['task_id'] and v['messages']==s['messages'] for v in correct.values())
   note='Same public prefix as a validated feasible write; premature handoff replaces that write.'
  elif mutation=='remove_confirmation':
   assert s['messages'][-1]['role']=='assistant' and not s['messages'][-1].get('tool_calls')
   note='Prefix ends at confirmation request; subsequent confirmation and tool activity removed together.'
  elif mutation=='foreign_payment':
   assert a['payment_method_id'] not in state.user['payment_methods']
   note='Selected method absent from authenticated user payment catalogue; actual other-user ID.'
  elif mutation=='different_product':
   order=state.orders[a['order_id']];old=next(i for i in order['items'] if i['item_id']==a['item_ids'][0])
   assert a['new_item_ids'][0] not in state.products[old['product_id']]['variants']
   note='Replacement is a visible variant of another product, not the ordered product.'
  elif mutation=='cancel_reason':
   user=text([m for m in s['messages'] if m['role']=='user'][-1]).lower()
   other='no longer needed' if a['reason']=='ordered by mistake' else 'ordered by mistake'
   assert other in user and a['reason'] not in user
   note='Last customer confirmation explicitly names the other cancellation reason.'
  elif mutation=='same_product_variant':
   note='Customer selected a specific variant ID; candidate changes that ID.'
  else:raise AssertionError(mutation)
  out.append({'id':s['id'],'task_id':s['task_id'],'kind':kind,'expected':s['expected'],'basis':note})
 result={'dataset_sha256':sha256(DATA.read_bytes()).hexdigest(),'audit_type':'developer evidence audit, not independent human adjudication',
         'rows':out,'limitations':data['limitations'],'count':len(out)}
 target=ROOT/'.runtime/e1-review/label-audit-approved.json'
 with target.open('x') as f:json.dump(result,f,indent=2)
 print(json.dumps({'count':len(out),'by_kind':dict(Counter(r['kind'] for r in out))}))

if __name__=='__main__':main()
