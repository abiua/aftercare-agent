"""Read-only v3 development diagnostics. Missing evidence is unknown, never success."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from aftercare.agent_kernel.amount_arithmetic import amounts
from aftercare.agent_kernel.retail_guards import facts
from tau_official_summary import call_receipts, read_json, row_key, write_json

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'evidence/architecture_refactor/kernel_v3_dev_check.json'
UNKNOWN = 'unknown'
ACTORS = ('executor', 'reviewer', 'self_check', 'fact_expert', 'policy_expert', 'vision_expert', 'user', 'judge')
READ_STATUSES = ('executed', 'rejected', 'error', 'limit')
GUARDS = ('guard', 'post_review_guard')
EXPERTS = ('expert_fact_expert', 'expert_policy_expert')


def total(values):
    values = list(values)
    return UNKNOWN if UNKNOWN in values else sum(values)


def all_known(values):
    values = list(values)
    return False if False in values else UNKNOWN if UNKNOWN in values else True


def any_known(values):
    values = list(values)
    return True if True in values else UNKNOWN if UNKNOWN in values else False


class Evidence:
    def __init__(self):
        self.unknown = []

    def missing(self, location):
        if location not in self.unknown:
            self.unknown.append(location)
        return UNKNOWN

    def field(self, obj, key, kind, location, nullable=False):
        value = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(obj, dict) and key in obj and (
            (value is None and nullable) or type(value) in (kind if isinstance(kind, tuple) else (kind,))
        ):
            return value
        return self.missing(f'{location}.{key}')

    def file(self, path):
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError('Expected object')
            return value
        except (OSError, ValueError):
            self.missing(str(path))
            return {}


def billing(directory, keys, ev):
    """Validate before reusing call_receipts (which otherwise ignores a torn last line)."""
    path = directory / 'calls.jsonl'
    try:
        raw = path.read_text()
        if raw and not raw.endswith('\n'):
            raise ValueError('Partial journal line')
        seen, settled = set(), set()
        for line in raw.splitlines():
            item = json.loads(line)
            ident = item['call_id']
            if type(ident) is not int:
                raise ValueError('Bad call id')
            if 'reserve_usd' in item:
                if ident in seen or item['row'] not in keys:
                    raise ValueError('Duplicate reservation or unexpected row')
                value = Decimal(item['reserve_usd'])
                seen.add(ident)
            elif 'actual_usd' in item:
                if ident not in seen or ident in settled:
                    raise ValueError('Orphan/duplicate settlement')
                value = Decimal(item['actual_usd'])
                settled.add(ident)
            elif 'failure_type' in item and ident in seen:
                continue  # A failure record does not settle its reservation.
            else:
                raise ValueError('Unexpected billing record')
            if not value.is_finite() or value < 0:
                raise ValueError('Invalid cost')
        reservations, settlements = call_receipts(path)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        ev.missing('calls.jsonl: missing, malformed, partial, or inconsistent journal')
        return None
    rows = {key: [] for key in keys}
    for ident, reservation in reservations.items():
        rows[reservation['row']].append((reservation, settlements.get(ident)))
    return rows


def costs(calls, ev):
    if calls is None:
        return {k: UNKNOWN for k in ('calls', 'calls_by_actor', 'cost_usd_by_actor',
                                     'known_total_cost_usd', 'total_cost_usd', 'unsettled_calls')}
    counts = Counter(dict.fromkeys(ACTORS, 0))
    paid = {actor: Decimal(0) for actor in ACTORS}
    pending = Counter()
    for reservation, settlement in calls:
        actor = ev.field(reservation, 'actor', str, 'billing.reservation')
        if actor not in ACTORS:
            actor = ev.missing('billing.reservation.actor: absent or unsupported')
        counts[actor] += 1
        if settlement is None:
            pending[actor] += 1
        else:
            paid[actor] = paid.get(actor, Decimal(0)) + Decimal(settlement['actual_usd'])
    known = sum(paid.values())
    return {'calls': len(calls), 'calls_by_actor': dict(counts),
            'cost_usd_by_actor': {a: UNKNOWN if pending[a] else float(paid.get(a, 0)) for a in counts},
            'known_total_cost_usd': float(known),
            'total_cost_usd': UNKNOWN if sum(pending.values()) else float(known),
            'unsettled_calls': sum(pending.values())}


def outcome(row, ev):
    status = ev.field(row, 'status', str, 'row')
    error = row.get('error_type')  # Only the runner's exception branch writes this key.
    termination = row.get('termination_reason')
    if error == 'GuardRejected':
        category = 'guard_rejected'
    elif status == 'budget_exhausted' or error == 'BudgetExhausted':
        category = 'budget_or_call_limit'
    elif status == 'completed' and termination in {'user_stop', 'agent_stop'}:
        category = 'normal'
    elif status in {'running', 'not_run'}:
        category = 'unfinished'
    elif status in {'error', 'unknown', 'timeout'} or (status == 'completed' and termination):
        category = 'other_exception_or_limit'
    else:
        category = ev.missing('row.normal_completion_evidence')
    return {'category': category, 'normally_finished': UNKNOWN if category == UNKNOWN else category == 'normal',
            'status': status, 'termination_reason': termination if termination is not None else UNKNOWN,
            'error_type': error if error is not None else None,
            'official_reward': ev.field(row, 'official_reward', (int, float), 'row', nullable=True),
            'duration_seconds': ev.field(row, 'duration_seconds', (int, float), 'row', nullable=True),
            'guard_rejected': error == 'GuardRejected'}


def messages_info(trace, ev):
    messages = ev.field(trace, 'messages', list, 'official')
    if messages == UNKNOWN:
        return UNKNOWN, UNKNOWN
    valid, transfers = [], 0
    before = len(ev.unknown)
    for i, message in enumerate(messages):
        loc = f'messages[{i}]'
        role = ev.field(message, 'role', str, loc)
        ev.field(message, 'content', str, loc, nullable=True)
        if role == 'assistant':
            calls = ev.field(message, 'tool_calls', list, loc, nullable=True)
            if isinstance(calls, list):
                for call in calls:
                    ev.field(call, 'id', str, loc + '.tool_call')
                    name = ev.field(call, 'name', str, loc + '.tool_call')
                    ev.field(call, 'arguments', dict, loc + '.tool_call')
                    transfers += name == 'transfer_to_human_agents'
        elif role == 'tool':
            ev.field(message, 'id', str, loc)
            ev.field(message, 'error', bool, loc)
        if isinstance(message, dict):
            valid.append(message)
    if len(ev.unknown) != before:
        return UNKNOWN, UNKNOWN
    # Same successful find_user_id_* interpretation as the actual retail guard.
    return bool(facts(valid).user_id), transfers


def empty_trace_metrics():
    return {
        'experts': {'consults': 0, 'refreshes': 0, 'stale_dropped': 0, 'unavailable': 0,
                    'price_corrected': 0, 'dropped_ungrounded': 0,
                    'read_statuses': dict.fromkeys(READ_STATUSES, 0), 'consultations': []},
        'amount_rule': {'triggered': 0, 'corrected': 0, 'soft_unresolved': 0, 'other_or_unknown': 0, 'events': []},
        'auth_reminder': {'triggered': 0, 'corrected': 0, 'soft_unresolved': 0, 'other_or_unknown': 0, 'events': []},
        'program_blocks': {'status': 'needs_manual_review', 'events': [], 'count': 0},
        'guard_rejections_by_rule': {},
        'review': {'count': 0, 'verdicts': {'allow': 0, 'revise': 0}, 'fallback_reason_count': 0,
                   'fallback_count': 0, 'failures': 0, 'dropped_requests': 0,
                   'order_dependency_conflict': 0, 'resolutions': {}, 'executor_revision': 0},
    }


def retry_result(events, index, rule, proposal, ev):
    """Only an immediately adjacent executor retry belongs to this guard's step.

    A post-review soft rejection has no new retry: engine.accept_soft records it directly.
    Never search into the next customer turn for a later allowed guard.
    """
    def stage(j):
        return events[j].get('stage') if j < len(events) and isinstance(events[j], dict) else None

    def unresolved(j):
        if stage(j) != 'guard_soft_unresolved':
            return False
        output = events[j].get('output')
        rules = ev.field(output, 'rule_ids', list, f'private_trace[{j}].output')
        return isinstance(rules, list) and rule in rules

    if unresolved(index + 1):
        return 'soft_unresolved', False, None
    if events[index]['stage'] != 'guard' or stage(index + 1) != 'executor_step':
        return UNKNOWN, UNKNOWN, None
    revised = events[index + 1].get('output', {})
    j = index + 2
    if stage(j) == 'single_tool_normalization':
        revised = events[j].get('output', {})
        j += 1
    if stage(j) != 'guard':
        return UNKNOWN, UNKNOWN, revised
    allowed = ev.field(events[j].get('output'), 'allowed', bool, f'private_trace[{j}].output')
    changed = revised != proposal if isinstance(proposal, dict) else UNKNOWN
    if allowed is True:
        return 'corrected', changed, revised
    if unresolved(j + 1):
        return 'soft_unresolved', changed, revised
    return UNKNOWN, changed, revised


def trace_metrics(trace, task_id, ev):
    result = empty_trace_metrics()
    events = ev.field(trace, 'private_trace', list, 'official')
    if events == UNKNOWN:
        return {key: UNKNOWN for key in result}
    proposal, requests, review_index = UNKNOWN, UNKNOWN, None
    step, previous, fallback_ids = 0, None, set()
    damaged = set()
    for i, event in enumerate(events):
        loc = f'private_trace[{i}]'
        stage = ev.field(event, 'stage', str, loc)
        output = ev.field(event, 'output', dict, loc)
        if stage == UNKNOWN or output == UNKNOWN:
            return {key: UNKNOWN for key in result}
        details = {'task_id': task_id, 'event_index': i, 'step': step, 'stage': stage}
        get = lambda key, kind, nullable=False: ev.field(output, key, kind, loc + '.output', nullable)
        if stage in {'executor_step', 'executor_revision', 'single_tool_normalization'}:
            if stage == 'executor_step' and not (previous and previous['stage'] == 'guard'
                                                and previous['output'].get('allowed') is False):
                step += 1
                requests, review_index = UNKNOWN, None
            proposal = output
            get('text', str); get('calls', list)
            result['review']['executor_revision'] += stage == 'executor_revision'
        elif stage in EXPERTS:
            expert = result['experts']
            expert['consults'] += 1
            expert['refreshes'] += 'refresh_of' in output
            stop = get('stop_reason', str)
            expert['unavailable'] = total([expert['unavailable'], UNKNOWN if stop == UNKNOWN else int(stop == 'unavailable')])
            logs = get('read_log', list)
            counts = Counter(dict.fromkeys(READ_STATUSES, 0))
            if isinstance(logs, list):
                for j, entry in enumerate(logs):
                    status = ev.field(entry, 'status', str, f'{loc}.output.read_log[{j}]')
                    if status not in READ_STATUSES:
                        status = ev.missing(f'{loc}.output.read_log[{j}].status: unsupported or missing')
                    counts[status] += 1
                for status, count in counts.items():
                    expert['read_statuses'][status] = total([expert['read_statuses'].get(status, 0), count])
                if UNKNOWN in counts:
                    expert['read_statuses'].update(dict.fromkeys(READ_STATUSES, UNKNOWN))
            else:
                expert['read_statuses'] = dict.fromkeys(READ_STATUSES, UNKNOWN)
            reason = ev.field(event, 'reason', str, loc, nullable=True)
            for name in ('price_corrected', 'dropped_ungrounded'):
                value = UNKNOWN if reason == UNKNOWN else sum(int(n) for n in re.findall(rf'(?:^|;){name}:(\d+)(?:;|$)', reason or ''))
                expert[name] = total([expert[name], value])
            expert['consultations'].append({**details, 'step': step + 1, 'refresh': 'refresh_of' in output,
                'signal': output.get('signal', UNKNOWN), 'read_log_count': len(logs) if isinstance(logs, list) else UNKNOWN,
                'read_statuses': dict(counts) if isinstance(logs, list) else UNKNOWN, 'stop_reason': stop, 'reason': reason})
        elif stage == 'expert_stale_dropped':
            result['experts']['stale_dropped'] += 1
        elif stage in GUARDS:
            rule_ids, allowed, soft = get('rule_ids', list), get('allowed', bool), get('soft', bool)
            feedback = get('feedback', str)
            if isinstance(rule_ids, list) and any(not isinstance(rule, str) for rule in rule_ids):
                rule_ids = ev.missing(loc + '.output.rule_ids: expected strings')
            if rule_ids == UNKNOWN or allowed == UNKNOWN:
                damaged.update(('amount_rule', 'auth_reminder', 'program_blocks', 'guard_rejections_by_rule'))
            else:
                for rule in rule_ids:
                    if allowed is False:
                        counts = result['guard_rejections_by_rule']
                        counts[rule] = counts.get(rule, 0) + 1
                    if allowed is False and soft is False:
                        result['program_blocks']['events'].append({**details, 'rule': rule, 'feedback': feedback, 'proposal': proposal})
                    if soft == UNKNOWN:
                        damaged.add('program_blocks')
                    if rule in {'amount_arithmetic', 'authentication_username'}:
                        key = 'amount_rule' if rule == 'amount_arithmetic' else 'auth_reminder'
                        status, changed, revised = retry_result(events, i, rule, proposal, ev)
                        finding = {**details, 'feedback': feedback, 'resolution': status, 'proposal_changed': changed,
                                   'proposal': proposal, 'retry_proposal': revised}
                        if rule == 'amount_arithmetic':
                            text = proposal.get('text') if isinstance(proposal, dict) else None
                            finding['reply_amounts'] = [str(v) for v in amounts(text)] if isinstance(text, str) else UNKNOWN
                            # These bare numbers are the exact program diagnostic, not dollar-regex guesses.
                            match = re.search(r'Uncheckable dollar amount\(s\): (.*?)\. Verifiable amounts', feedback) if feedback != UNKNOWN else None
                            finding['uncheckable_amounts'] = match.group(1).split(', ') if match else UNKNOWN
                        result[key]['events'].append(finding)
                        result[key]['triggered'] += 1
                        result[key][status if status in {'corrected', 'soft_unresolved'} else 'other_or_unknown'] += 1
        elif stage == 'proposal_review':
            review_index = i
            review = result['review']
            review['count'] += 1
            verdict = get('verdict', str)
            review['verdicts'][verdict] = review['verdicts'].get(verdict, 0) + 1
            fallback = get('fallback_reason', str, True)
            if fallback == UNKNOWN:
                damaged.add('review')
            elif fallback:
                fallback_ids.add(i)
                review['fallback_reason_count'] += 1
            for name in ('failures', 'dropped_requests'):
                values = get(name, list)
                review[name] = total([review[name], len(values) if isinstance(values, list) else UNKNOWN])
            requests = get('requests', list)
            blocking = get('blocking_reason', str, True)
            if blocking == UNKNOWN:
                damaged.add('program_blocks')
                review['order_dependency_conflict'] = UNKNOWN
            elif blocking == 'order_dependency_conflict':
                review['order_dependency_conflict'] = total([review['order_dependency_conflict'], 1])
                result['program_blocks']['events'].append({**details, 'rule': blocking, 'requests': requests, 'proposal': proposal})
        elif stage == 'review_resolution':
            status = get('status', str)
            counts = result['review']['resolutions']
            counts[status] = counts.get(status, 0) + 1
            if status == 'review_unavailable':
                fallback_ids.add(review_index if review_index is not None else i)
            elif status == UNKNOWN:
                damaged.update(('review', 'program_blocks'))
            if status == 'blocked_dependency':
                result['program_blocks']['events'].append({**details, 'rule': 'order_dependency_conflict',
                    'requests': requests, 'proposal': proposal})
        elif stage == 'guard_soft_unresolved':
            accepted = get('guard_result', dict)
            allowed = ev.field(accepted, 'allowed', bool, loc + '.output.guard_result')
            rules = ev.field(accepted, 'rule_ids', list, loc + '.output.guard_result')
            if allowed == UNKNOWN or rules == UNKNOWN:
                damaged.add('program_blocks')
            elif allowed is False:
                for rule in rules:
                    result['program_blocks']['events'].append({**details, 'rule': rule,
                        'feedback': ev.field(accepted, 'feedback', str, loc + '.output.guard_result'),
                        'proposal': proposal})
                    counts = result['guard_rejections_by_rule']
                    counts[rule] = counts.get(rule, 0) + 1
        else:
            ev.missing(f'{loc}: unsupported stage {stage}')
            return {key: UNKNOWN for key in result}
        previous = event
    result['review']['fallback_count'] = len(fallback_ids)
    result['program_blocks']['count'] = len(result['program_blocks']['events'])
    for key in damaged:
        result[key] = UNKNOWN
    return result


def metric(row, *keys):
    value = row
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return UNKNOWN
        value = value[key]
    return value


def summarize(rows):
    summed = lambda *keys: total(metric(row, *keys) for row in rows)
    experts = {key: summed('experts', key) for key in ('consults', 'refreshes', 'stale_dropped', 'unavailable', 'price_corrected', 'dropped_ungrounded')}
    experts['read_statuses'] = {s: summed('experts', 'read_statuses', s) for s in READ_STATUSES}
    consults = [metric(row, 'experts', 'consults') for row in rows]
    positive = [n for n in consults if n != UNKNOWN and n > 0]
    mean = UNKNOWN if UNKNOWN in consults else sum(positive) / len(positive) if positive else 0
    experts['mean_consults_among_consulted_tasks'] = mean
    result = {'experts': experts}
    for key in ('amount_rule', 'auth_reminder'):
        result[key] = {name: summed(key, name) for name in ('triggered', 'corrected', 'soft_unresolved', 'other_or_unknown')}
    result['review'] = {key: summed('review', key) for key in ('count', 'fallback_reason_count', 'fallback_count', 'failures', 'dropped_requests', 'order_dependency_conflict', 'executor_revision')}
    for category, name in (('review', 'verdicts'), ('review', 'resolutions'), (None, 'guard_rejections_by_rule')):
        maps = [metric(row, category, name) if category else metric(row, name) for row in rows]
        merged = UNKNOWN if UNKNOWN in maps else dict(sum((Counter(m) for m in maps), Counter()))
        (result[category] if category else result)[name] = merged
    result['program_blocks'] = {'status': 'needs_manual_review', 'count': summed('program_blocks', 'count'),
        'events': [event for row in rows if isinstance(row['program_blocks'], dict) for event in row['program_blocks']['events']]}
    result['billing'] = {key: summed('billing', key) for key in ('calls', 'known_total_cost_usd', 'total_cost_usd', 'unsettled_calls')}
    for key in ('calls_by_actor', 'cost_usd_by_actor'):
        maps = [metric(row, 'billing', key) for row in rows]
        result['billing'][key] = UNKNOWN if UNKNOWN in maps else {
            actor: total(m.get(actor, 0) for m in maps) for actor in sorted(set().union(*(m.keys() for m in maps)))}
    result['duration_seconds_sum'] = total(UNKNOWN if metric(row, 'outcome', 'duration_seconds') is None else metric(row, 'outcome', 'duration_seconds') for row in rows)
    result['completion_categories'] = dict(Counter(row['outcome']['category'] for row in rows))
    return result


def checks(rows, totals):
    finished = all_known([row['outcome']['normally_finished'] for row in rows] + [row['identity_valid'] for row in rows] + [
        UNKNOWN if row['billing']['unsettled_calls'] == UNKNOWN else row['billing']['unsettled_calls'] == 0 for row in rows])
    reads = any_known(UNKNOWN if metric(row, 'experts', 'read_statuses', 'executed') == UNKNOWN else metric(row, 'experts', 'read_statuses', 'executed') > 0 for row in rows)
    mean = totals['experts']['mean_consults_among_consulted_tasks']
    amount = totals['amount_rule']
    amount_ok = UNKNOWN if UNKNOWN in (amount['triggered'], amount['corrected']) else amount['triggered'] == 0 or amount['corrected'] > 0
    auth = all_known(UNKNOWN if metric(row, 'auth_reminder', 'triggered') == UNKNOWN else
                     True if metric(row, 'auth_reminder', 'triggered') == 0 else row['authentication_completed'] for row in rows)
    fallback = totals['review']['fallback_count']
    return {
        'all_finished': {'passed': len(rows) == 6 and finished, 'definition': 'Exactly six normal terminations, with zero unsettled calls.'},
        'expert_reads_used': {'passed': reads, 'definition': 'At least one task has an executed specialist read.'},
        'expert_consults_bounded': {'passed': UNKNOWN if mean == UNKNOWN else mean <= 3, 'mean': mean},
        'amount_rule': {'passed': amount_ok, 'definition': 'No amount_arithmetic trigger, or at least one immediately retried guard allowed.'},
        'program_blocks': {'passed': None, 'status': 'needs_manual_review', 'count': totals['program_blocks']['count']},
        'review_fallback': {'passed': UNKNOWN if fallback == UNKNOWN else fallback <= 1, 'count': fallback},
        'auth_reminder': {'passed': auth, 'definition': 'Every task with authentication_username eventually authenticates, or no trigger.'},
    }


def analyse(directory):
    directory = Path(directory).resolve()
    global_ev = Evidence()
    manifest = global_ev.file(directory / 'manifest.json')
    schedule = global_ev.field(manifest, 'schedule', list, 'manifest')
    if schedule == UNKNOWN or not schedule:
        raise ValueError('Cannot determine task population without a nonempty manifest.schedule')
    keys = []
    for item in schedule:
        if not isinstance(item, dict) or not isinstance(item.get('task_id'), str) or item.get('architecture') != 'M2':
            raise ValueError('Expected a unique M2 task schedule')
        keys.append(row_key(item))
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate task in schedule')
    ledger = billing(directory, keys, global_ev)
    rows = []
    for item, key in zip(schedule, keys):
        ev = Evidence()
        row = ev.file(directory / 'rows' / f'{key}.json')
        trace = ev.file(directory / 'official' / f'{key}.json')
        identity_valid = True
        for name in ('task_id', 'architecture'):
            if row.get(name) != item[name]:
                ev.missing(f'row.{name}: missing or identity mismatch')
                identity_valid = UNKNOWN
        authenticated, transfers = messages_info(trace, ev)
        rows.append({'task_id': item['task_id'], 'architecture': 'M2', 'identity_valid': identity_valid, 'outcome': outcome(row, ev),
                     'authentication_completed': authenticated, 'transfer_tool_calls': transfers,
                     **trace_metrics(trace, item['task_id'], ev),
                     'billing': costs(ledger[key] if ledger is not None else None, ev),
                     'unknown_fields': ev.unknown})
    totals = summarize(rows)
    return {'schema_version': 1, 'directory': str(directory), 'per_task': rows, 'totals': totals,
            'checks': checks(rows, totals), 'unknown_fields': global_ev.unknown,
            'interpretation': {
                'scope': 'Development diagnostics only; official rewards are recorded without success-rate comparison.',
                'steps': 'step is the inferred kernel step; event_index is the exact zero-based private_trace index, not an official turn number.',
                'guard_rejected': 'Current KernelRetailAgent preserves private_trace and propagates GuardRejected. run_row records error_type=GuardRejected. It does not automatically transfer to a human. transfer_tool_calls is separate and does not establish causality.',
                'fallback': 'Count distinct proposal reviews with nonempty fallback_reason OR associated review_resolution=review_unavailable; do not double-count the same review.',
                'failures': 'Length of proposal_review.output.failures; includes the final failed attempt, not only attempts followed by a retry.',
                'signals': 'Current consultation events do not record signal names; report unknown rather than infer from role.',
                'program_blocks': 'Each hard guard rejection, proposal_review blocking_reason=order_dependency_conflict and blocked_dependency resolution is listed separately, even within one step.',
                'amounts': 'reply_amounts are currency-marked amounts parsed with the kernel helper; uncheckable_amounts come from guard feedback.',
                'missing': 'Absent or malformed evidence is unknown. A missing population manifest is fatal. Zero denotes an observed absence of events.'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / '.runtime/kernel-v3-dev/run-v1')
    args = parser.parse_args()
    report = analyse(args.directory)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
