"""Offline, Decimal-only amount provenance and bounded arithmetic checks."""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal('0.01')
TOLERANCE = Decimal('0.005')
NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
MONEY = re.compile(
    rf'(?:\$\s*(?P<prefix>{NUMBER})|(?<![\w.,])(?P<suffix>{NUMBER})\s*(?:美元|USD\b|US\s+dollars?\b|dollars?\b))',
    re.IGNORECASE,
)


def cents(value):
    return Decimal(str(value).replace(',', '')).quantize(CENT, rounding=ROUND_HALF_UP)


def amounts(text):
    """Keep occurrences in text order; bare numbers and percentages are not amounts."""
    found = []
    for match in MONEY.finditer(text or ''):
        if re.match(r'\s*[%％]', (text or '')[match.end():]):
            continue
        found.append(cents(match.group('prefix') or match.group('suffix')))
    return found


def price_values(value, key=''):
    """Only numbers under price/amount/balance keys are tool amount sources."""
    if isinstance(value, dict):
        for name, child in value.items():
            yield from price_values(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from price_values(child, key)
    elif (any(word in key.lower() for word in ('price', 'amount', 'balance'))
          and not isinstance(value, bool) and isinstance(value, (int, float, Decimal, str))):
        # Some tool records encode prices as numeric strings.
        try:
            number = cents(value)
            if number.is_finite():
                yield number
        except (InvalidOperation, ValueError):
            pass


def grounded_amounts(messages):
    """P union U: price-like tool fields and all customer-written numbers."""
    result = set()
    names = {call['id']: call.get('function', call).get('name') for message in messages
             for call in message.get('tool_calls') or []}
    for message in messages:
        if message.get('role') == 'user':
            result.update(cents(match.group()) for match in re.finditer(NUMBER, message.get('content') or ''))
        elif message.get('role') == 'tool':
            value = message.get('content')
            if isinstance(value, str):
                try:
                    value = json.loads(value, parse_float=Decimal)
                except (ValueError, TypeError):
                    continue
            # The calculate tool returns a bare number; it is the sanctioned way to derive an amount.
            if (names.get(message.get('tool_call_id', message.get('id'))) == 'calculate'
                    and not message.get('error') and isinstance(value, (int, Decimal))
                    and not isinstance(value, bool)):
                result.add(cents(value))
            result.update(price_values(value))
    return result


def previous_assistant_amounts(messages):
    """H suppresses repeated reminders, even if the original amount was unverified."""
    return {value for message in messages if message.get('role') == 'assistant'
            for value in amounts(message.get('content'))}


def signed_sums(values):
    # For equal sums keep the largest number of terms, so cancellation still counts.
    sums = {Decimal(0): 0}
    for value in values:
        updated = dict(sums)
        for total, count in sums.items():
            for sign in (-1, 1):
                candidate = total + sign * value
                updated[candidate] = max(updated.get(candidate, 0), count + 1)
        sums = updated
    return sums


def checkable(target, values):
    """Meet in the middle: at most 2 * 3**6 states, each item used at most once."""
    if not 2 <= len(values) <= 12:
        return False
    middle = len(values) // 2
    left, right = signed_sums(values[:middle]), signed_sums(values[middle:])
    # Inputs and sums are exact cents, so equality implements the 0.005 tolerance.
    return any(count + right.get(target - subtotal, -len(values)) >= 2
               for subtotal, count in left.items())


def amount_findings(messages, text, orders):
    """Shared diagnostics for the guard and replay; callers enforce auth/no-tool scope."""
    listed = amounts(text)
    grounded = grounded_amounts(messages)
    previous = previous_assistant_amounts(messages)
    candidates = list(dict.fromkeys(listed))
    verified = set()
    order_prices = [[cents(item['price']) for item in order.get('items', []) if 'price' in item]
                    for order in orders.values()]
    for _ in range(3):
        before = len(verified)
        for value in candidates:
            if value in verified:
                continue
            basis = [v for v in candidates if v in grounded or v in verified]
            # Exact cents: these set lookups implement sum and absolute difference
            # within the 0.005 tolerance, without searching arbitrary tool combinations.
            pair = any(value - x in grounded or (value >= 0 and
                       (x - value in grounded or x + value in grounded)) for x in basis)
            if (value in grounded or value in previous or pair or checkable(value, basis)
                    or any(prices and (abs(value - sum(prices, Decimal(0))) <= TOLERANCE
                                       or checkable(value, prices)) for prices in order_prices)):
                verified.add(value)
        if len(verified) == before:
            break
    unknown = [value for value in candidates if value not in verified]
    if not unknown:
        return None
    basis = [value for value in candidates if value in verified]
    return {'uncheckable_amounts': [str(v) for v in unknown],
            'listed_amounts': [str(v) for v in listed],
            'verifiable_amounts': [str(v) for v in basis],
            'verifiable_sum': str(sum(basis, Decimal('0.00')))}
