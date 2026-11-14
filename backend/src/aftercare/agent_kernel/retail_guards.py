"""Deterministic public-policy guards; consumes visible records, never task answers."""
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from .contracts import GuardResult, ToolKind
from .dependencies import effect

AUTH={'find_user_id_by_email','find_user_id_by_name_zip'}
SWAPS={'modify_pending_order_items','exchange_delivered_order_items'}
USERNAME=re.compile(r'(?<![A-Za-z0-9_])([A-Za-z]+)_([A-Za-z]+)_\d+(?![A-Za-z0-9_])')
ZIP=re.compile(r'(?<![\w#])\d{5}(?!\w)')


def name_from_username(messages):
    """Account usernames have the form first_last_digits; with a ZIP that is a permitted lookup."""
    said=' '.join(m.get('content') or '' for m in messages if m.get('role')=='user')
    username,zipcode=USERNAME.search(said),ZIP.search(said)
    if username and zipcode:
        return username.group(1).capitalize(),username.group(2).capitalize(),zipcode.group()


def parse(value):
    try:return json.loads(value)
    except (ValueError,TypeError):return value


@dataclass
class Facts:
    user_id: str | None = None
    attempted: set = field(default_factory=set)
    user: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)
    products: dict = field(default_factory=dict)
    allowed_orders: set = field(default_factory=set)


def facts(messages):
    state=Facts();calls={}
    for message in messages:
        for c in message.get('tool_calls') or []:
            fn=c.get('function',c);args=fn.get('arguments',{})
            calls[c['id']]=(fn['name'],parse(args) if isinstance(args,str) else args)
        if message.get('role')!='tool':continue
        name,args=calls.get(message.get('tool_call_id',message.get('id')),('',{}))
        if name in AUTH:state.attempted.add(name)
        value=parse(message.get('content'))
        if message.get('error') or isinstance(value,str) and value.lower().startswith('error'):continue
        if name in AUTH and not state.user_id and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_]+',value):
            state.user_id=value
        if not isinstance(value,dict) or not state.user_id:continue
        if name=='get_user_details' and args.get('user_id')==state.user_id and value.get('user_id')==state.user_id:
            state.user=value;state.allowed_orders.update(value.get('orders',[]))
        if name=='get_product_details' and value.get('product_id')==args.get('product_id'):
            state.products[value['product_id']]=value
        # Write responses update status too; old pending evidence cannot authorize another write.
        if value.get('order_id') and value.get('user_id')==state.user_id and value.get('order_id')==args.get('order_id'):
            state.orders[value['order_id']]=value;state.allowed_orders.add(value['order_id'])
    return state


def reject(rule,feedback):return GuardResult(allowed=False,rule_ids=(rule,),feedback=feedback)


class RetailGuards:
    def normalize(self, proposal):
        # Public policy explicitly requires one tool and no simultaneous speech.
        if proposal.calls:return proposal.model_copy(update={'calls':proposal.calls[:1],'text':''})
        return proposal

    def check(self,messages,proposal,tools):
        state=facts(messages);by_name={t.name:t for t in tools}
        if len(proposal.calls)>1 or proposal.calls and proposal.text:
            return reject('single_tool','Make one tool call without a customer reply.')
        if not state.user_id:
            if proposal.calls:
                if proposal.calls[0].name not in AUTH:
                    return reject('authentication','First authenticate by email OR first name, last name and ZIP code; do not transfer yet.')
            else:
                known=None if 'find_user_id_by_name_zip' in state.attempted else name_from_username(messages)
                if known:
                    return reject('authentication_username','The customer gave an account username and a ZIP code. '
                        'Do not ask again yet: call find_user_id_by_name_zip with first_name="%s", last_name="%s", '
                        'zip="%s". If that finds no account, then ask for the name.'%known)
                text=proposal.text.lower()
                email=any(s in text for s in ('email','e-mail','邮箱','电子邮件'))
                name=any(s in text for s in ('name','姓名','名字'))
                zipcode=any(s in text for s in ('zip','postal','邮编'))
                if not(email and name and zipcode):
                    return reject('authentication_reply','Offer BOTH authentication routes: account email OR first and last name plus ZIP code.')
        if not proposal.calls:return GuardResult(allowed=True,confirmed_facts={'authenticated_user':state.user_id})
        call=proposal.calls[0];tool=by_name.get(call.name);args=call.arguments
        if not tool:return reject('known_tool','Use an available tool.')
        if tool.kind==ToolKind.AUTH:
            if state.user_id:return reject('single_user','This conversation already has an authenticated user; do not switch accounts.')
            return GuardResult(allowed=True)
        if args.get('user_id',state.user_id)!=state.user_id:
            return reject('single_user','Only query or update the authenticated user.')
        order_id=args.get('order_id')
        if order_id and order_id not in state.allowed_orders:
            return reject('single_user','Read the authenticated user profile to establish order ownership first.')
        if tool.kind!=ToolKind.WRITE:return GuardResult(allowed=True,confirmed_facts={'authenticated_user':state.user_id})
        order=state.orders.get(order_id,{})
        if order_id:
            expected='delivered' if call.name.startswith(('return_','exchange_')) else 'pending'
            if order.get('status')!=expected:
                return reject('order_status',f'Read current order details; {call.name} requires status {expected}.')
        problem=self.items(call,state,order) or self.payment(call,state,order)
        if problem:return problem
        if call.name=='cancel_pending_order' and args.get('reason') not in {'no longer needed','ordered by mistake'}:
            return reject('cancel_reason','Ask customer to confirm either no longer needed or ordered by mistake.')
        replies=[i for i,m in enumerate(messages) if m['role']=='assistant' and m.get('content') and not m.get('tool_calls')]
        if not replies or not any(m['role']=='user' for m in messages[replies[-1]+1:]):
            return reject('confirmation','Explain the proposed action and its details, then wait for a customer reply before writing.')
        return GuardResult(allowed=True,confirmed_facts={'authenticated_user':state.user_id,
            'order_id':order_id,'order_status':order.get('status'),'structural_confirmation':True,
            'write_effect':effect(proposal)})

    def items(self,call,state,order):
        args=call.arguments
        if 'item_ids' not in args:return None
        old=args['item_ids'];available=Counter(i['item_id'] for i in order.get('items',[]))
        if not isinstance(old,list) or not old or Counter(old)-available:
            return reject('item_membership','Select only items and quantities present in this order.')
        if call.name not in SWAPS:return None
        new=args.get('new_item_ids',[])
        if len(old)!=len(new):return reject('item_variant','Replacement lists must have equal lengths.')
        for previous,replacement in zip(old,new):
            item=next(i for i in order['items'] if i['item_id']==previous)
            variant=state.products.get(item['product_id'],{}).get('variants',{}).get(replacement)
            if previous==replacement or not variant or variant.get('available') is not True:
                return reject('item_variant','Read product variants and choose a different available variant of the same product.')
        return None

    def payment(self,call,state,order):
        chosen=call.arguments.get('payment_method_id')
        if not chosen:return None
        methods=state.user.get('payment_methods',{});method=methods.get(chosen)
        original=[p['payment_method_id'] for p in order.get('payment_history',[]) if p.get('transaction_type')=='payment']
        if not method:return reject('payment_owner','Read user payment methods; the selected method must belong to this user.')
        if call.name=='return_delivered_order_items' and chosen not in original and method.get('source')!='gift_card':
            return reject('refund_method','Refund to the original payment method or an existing gift card.')
        if call.name=='modify_pending_order_payment' and chosen in original:
            return reject('payment_change','Choose a payment method different from the original one.')
        due=Decimal(0)
        if call.name=='modify_pending_order_payment':due=sum(Decimal(str(i['price'])) for i in order.get('items',[]))
        if call.name in SWAPS:
            for old,new in zip(call.arguments['item_ids'],call.arguments['new_item_ids']):
                item=next(i for i in order['items'] if i['item_id']==old)
                variant=state.products[item['product_id']]['variants'][new]
                due+=Decimal(str(variant['price']))-Decimal(str(item['price']))
        if method.get('source')=='gift_card' and due>Decimal(str(method.get('balance',0))):
            return reject('gift_balance','Gift card balance must cover the payable amount.')
        return None
