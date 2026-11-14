"""Retail policy effects, distinct from the official tool's permissive state checks."""
from .contracts import StepProposal

ITEMS = 'modify_pending_order_items'
ADDRESS = 'modify_pending_order_address'
PAYMENT = 'modify_pending_order_payment'
CANCEL = 'cancel_pending_order'
RETURN = 'return_delivered_order_items'
EXCHANGE = 'exchange_delivered_order_items'
ORDER_WRITES = (ITEMS, ADDRESS, PAYMENT, CANCEL, RETURN, EXCHANGE)
EFFECTS = {
    ITEMS: {'status': 'pending (item modified)', 'blocks': (CANCEL, ADDRESS, PAYMENT, ITEMS),
            'basis': 'policy: no further modifications/cancellation; address tool itself is more permissive'},
    EXCHANGE: {'status': 'exchange requested', 'blocks': (RETURN, EXCHANGE), 'basis': 'policy and strict delivered tool precondition'},
    RETURN: {'status': 'return requested', 'blocks': (EXCHANGE, RETURN), 'basis': 'strict delivered tool precondition'},
    CANCEL: {'status': 'cancelled', 'blocks': ORDER_WRITES, 'basis': 'order write preconditions; reads remain available'},
    PAYMENT: {'status': 'pending', 'blocks': (PAYMENT,), 'basis': 'tool payment-history precondition, not a status lock'},
}
ITEM_OPERATIONS = (ITEMS, RETURN, EXCHANGE)


def effect(proposal):
    if not proposal.calls:
        return None
    call = proposal.calls[0]
    spec = EFFECTS.get(call.name)
    return dict(spec, order_id=call.arguments.get('order_id'), operation=call.name) if spec else None


def fulfills(request, call):
    if request.operation != call.name or request.order_id != call.arguments.get('order_id'):
        return False
    if call.name in ITEM_OPERATIONS:
        from collections import Counter
        item_ids = Counter(request.item_ids) - Counter(call.arguments.get('new_item_ids', []))
        return bool(item_ids) and not (item_ids - Counter(call.arguments.get('item_ids', [])))
    return True


def conflict(proposal: StepProposal, requests):
    spec = effect(proposal)
    if not spec:
        return None
    call = proposal.calls[0]
    current = [r for r in requests if r.order_id == spec['order_id']]
    for request in current:
        # Deferral/withdrawal require customer evidence, validated by the reviewer.
        if request.status == 'pending' and request.operation in spec['blocks'] and not fulfills(request, call):
            return 'order_dependency_conflict'
    if not current or not any(fulfills(r, call) for r in current):
        return 'request_inventory_missing'
    return None
