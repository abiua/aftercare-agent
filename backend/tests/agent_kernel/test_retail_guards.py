import json
from aftercare.agent_kernel.contracts import StepProposal, ToolCall, ToolDefinition, ToolKind
from aftercare.agent_kernel.retail_guards import RetailGuards, facts

G=RetailGuards()
NAMES={'get_order_details':'read','find_user_id_by_email':'auth','modify_pending_order_items':'write','cancel_pending_order':'write','return_delivered_order_items':'write','modify_pending_order_payment':'write','transfer_to_human_agents':'handoff'}
TOOLS=tuple(ToolDefinition(name=n,kind=ToolKind(k)) for n,k in NAMES.items())

def result(name,args,value,ident):
 return [{'role':'assistant','content':None,'tool_calls':[{'id':ident,'function':{'name':name,'arguments':json.dumps(args)}}]},
         {'role':'tool','tool_call_id':ident,'content':json.dumps(value) if not isinstance(value,str) else value}]

def history(status='pending',balance=100):
 return result('find_user_id_by_email',{'email':'a@b.com'},'user1','a')+result('get_user_details',{'user_id':'user1'},
  {'user_id':'user1','orders':['o1'],'payment_methods':{'pay1':{'source':'paypal'},'pay2':{'source':'paypal'},'gift':{'source':'gift_card','balance':balance}}},'b')+result('get_order_details',{'order_id':'o1'},
  {'user_id':'user1','order_id':'o1','status':status,'items':[{'item_id':'i1','product_id':'p1','price':20}],
   'payment_history':[{'transaction_type':'payment','payment_method_id':'pay1'}]},'c')+result('get_product_details',{'product_id':'p1'},
  {'product_id':'p1','variants':{'i2':{'available':True,'price':30},'i3':{'available':False,'price':10}}},'d')+[
   {'role':'assistant','content':'Change i1 to i2 on o1 for $10?'},{'role':'user','content':'yes'}]

def check(name='modify_pending_order_items',args=None,h=None):
 return G.check(history() if h is None else h,StepProposal(calls=(ToolCall(id='new',name=name,arguments=args or {'order_id':'o1','item_ids':['i1'],'new_item_ids':['i2'],'payment_method_id':'pay1'}),)),TOOLS)


def test_authentication_and_reply_routes():
 assert check('find_user_id_by_email',{'email':'a@b.com'},[]).allowed
 assert not check(h=[]).allowed
 assert G.check([],StepProposal(text='Email or name and ZIP?'),TOOLS).allowed
 assert not G.check([],StepProposal(text='Your username?'),TOOLS).allowed
 assert not check('transfer_to_human_agents',{'summary':'help'},[]).allowed


def test_single_user_and_known_order():
 assert check('get_order_details',{'order_id':'o1'}).allowed
 assert not check('get_order_details',{'order_id':'other'}).allowed
 assert not check('get_order_details',{'order_id':'o1','user_id':'other'}).allowed


def test_normalization_drops_extra_calls_and_speech():
 p=StepProposal(text='done',calls=(ToolCall(id='1',name='get_order_details'),ToolCall(id='2',name='cancel_pending_order')))
 assert G.normalize(p).calls==p.calls[:1] and not G.normalize(p).text
 assert G.normalize(StepProposal(text='hello')).text=='hello'


def test_status_latest_write_result_invalidates_pending():
 assert check().allowed
 assert not check(h=history('delivered')).allowed
 h=history()+result('modify_pending_order_items',{'order_id':'o1'},
  {'user_id':'user1','order_id':'o1','status':'pending (items modifed)'},'write')
 assert not check(h=h).allowed


def test_variant_membership_quantity_and_availability():
 assert check().allowed
 for old,new in [(['missing'],['i2']),(['i1'],['i3']),(['i1'],['i1']),(['i1'],[]),(['i1','i1'],['i2','i2'])]:
  assert not check(args={'order_id':'o1','item_ids':old,'new_item_ids':new,'payment_method_id':'pay1'}).allowed


def test_payment_owner_refund_change_balance():
 assert check().allowed
 assert not check(args={'order_id':'o1','item_ids':['i1'],'new_item_ids':['i2'],'payment_method_id':'foreign'}).allowed
 assert check('return_delivered_order_items',{'order_id':'o1','item_ids':['i1'],'payment_method_id':'pay1'},history('delivered')).allowed
 assert not check('return_delivered_order_items',{'order_id':'o1','item_ids':['i1'],'payment_method_id':'pay2'},history('delivered')).allowed
 assert check('modify_pending_order_payment',{'order_id':'o1','payment_method_id':'pay2'}).allowed
 assert not check('modify_pending_order_payment',{'order_id':'o1','payment_method_id':'pay1'}).allowed
 assert check('modify_pending_order_payment',{'order_id':'o1','payment_method_id':'gift'}).allowed
 assert not check('modify_pending_order_payment',{'order_id':'o1','payment_method_id':'gift'},history(balance=10)).allowed


def test_reason_and_structural_confirmation():
 assert check('cancel_pending_order',{'order_id':'o1','reason':'no longer needed'}).allowed
 assert not check('cancel_pending_order',{'order_id':'o1','reason':'broken'}).allowed
 assert check().allowed
 assert not check(h=history()[:-1]).allowed


def test_error_auth_is_never_identity():
 assert facts(result('find_user_id_by_email',{},'Error: not found','x')).user_id is None
 assert facts(result('find_user_id_by_email',{},'user1','x')).user_id=='user1'


def test_username_and_zip_are_tried_before_asking_again():
 ask=StepProposal(text='Please give your email, or first and last name plus ZIP code.')
 said=[{'role':'user','content':'No email. My username is mei_kovacs_8020 and my ZIP code is 28236.'}]
 r=G.check(said,ask,TOOLS)
 assert r.rule_ids==('authentication_username',) and 'first_name="Mei", last_name="Kovacs", zip="28236"' in r.feedback
 failed=said+result('find_user_id_by_name_zip',{'first_name':'Mei','last_name':'Kovacs','zip':'28236'},'Error: user not found','n')
 assert G.check(failed,ask,TOOLS).allowed  # one attempt, then asking is right
 assert G.check([{'role':'user','content':'My username is mei_kovacs_8020.'}],ask,TOOLS).allowed  # no ZIP
 assert G.check([{'role':'user','content':'Order #W12345 please, I am Mei, ZIP 28236'}],ask,TOOLS).allowed  # no username
