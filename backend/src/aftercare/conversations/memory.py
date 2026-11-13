"""Traceable customer statements derived from the current conversation scope."""

import re

from sqlalchemy import select

from aftercare.business.rules import digest

from .tables import Attachment, Message

CORRECTION = re.compile(
    r"说错|描述有误|更正|纠正|改一下之前|修改原(?:工单|描述)|我说的不是|"
    r"(?:不是|并非)[^。！？\n]{1,80}(?:而是|是另外)"
)


_ACTION = {
    "REFUND": re.compile(r"退款"),
    "RETURN": re.compile(r"退货"),
    "REPLACE": re.compile(r"换货|换新|补发|补寄"),
    "COMPENSATE": re.compile(r"赔偿"),
    "CANCEL": re.compile(r"取消(?:这笔)?订单"),
    "REPAIR": re.compile(r"维修"),
}
_REQUEST = re.compile(r"想要?|希望|要求|申请|请求|请|能否|能不能|麻烦|帮我|我要|给我|需要")
_NEGATED = re.compile(r"不(?:想|要|用|需要)|别|无需")
_WITHDRAWAL = re.compile(
    r"(?:不(?:想|要|用|需要)(?:再)?|无需|别再)(?:申请|办理)?"
    r"(?:退款|退货|退了|换货|换新|补发|赔偿|维修)|(?:撤回|撤销)(?:申请|诉求|退款|退货|换货)|取消(?:退款|退货|换货)申请"
)
_POLICY = re.compile(r"政策|规则|条款|规定")


def requested_action(text):
    withdrawal = bool(_WITHDRAWAL.search(text))
    actions = set()
    for clause in re.split(r"[，,。！？!?；;\n]", text):
        clause = clause.strip()
        if _WITHDRAWAL.search(clause) or _POLICY.search(clause):
            continue
        match = _REQUEST.search(clause)
        if match and not _NEGATED.search(clause[: match.end()]):
            request = clause[match.end() :]
        elif withdrawal and clause.startswith(
            ("要", "改成", "改为", "换货", "换新", "退货", "退款")
        ):
            request = clause
        else:
            continue
        # One compound request: refund with a return, not mutually exclusive remedies.
        # Keep the original customer statement unchanged for policy/return conditions.
        request = request.replace("退货退款", "退款")
        actions.update(action for action, pattern in _ACTION.items() if pattern.search(request))
    if len(actions) == 1:
        return next(iter(actions))
    if len(actions) > 1:
        return "NEEDS_CLARIFICATION"
    return "WITHDRAWN" if withdrawal else None


def requested_action_record(message):
    if message is None or (action := requested_action(message.text)) is None:
        return None
    return {
        "source": "CUSTOMER_STATEMENT",
        "action": action,
        "message_id": message.id,
        "sequence": message.sequence,
        "sha256": digest([message.sequence, message.text]),
    }


def primary_statement(messages, *, fallback=None):
    return next((item for item in messages if requested_action(item.text)), fallback)


def _statements(row):
    return select(Message).where(
        Message.conversation_id == row.id,
        Message.analysis_issue_id == row.analysis_issue_id,
        Message.role == "customer",
        Message.sequence.is_not(None),
        Message.discarded_at.is_(None),
    )


def confirmation(db, row):
    if not row.analysis_issue_id or not row.analysis_from_seq:
        return None
    replies = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == row.id,
            Message.role == "assistant",
            Message.sequence > row.analysis_from_seq,
        )
        .order_by(Message.sequence.desc())
    )
    for reply in replies:
        for card in reply.cards:
            data = card["data"]
            if (
                data.get("code") == "CORRECTION_CONFIRMED"
                and data.get("issue_id") == row.analysis_issue_id
                and data.get("source_sequence") == row.analysis_from_seq
            ):
                return {**data, "confirmation_message_id": reply.id}
    return None


def is_correction(text):
    return bool(
        CORRECTION.search(text)
        or _WITHDRAWAL.search(text)
        or requested_action(text) == "NEEDS_CLARIFICATION"
    )


def correction(db, row):
    if not row.analysis_issue_id or not row.active_case_id:
        return None
    confirmed = confirmation(db, row)
    statements = _statements(row)
    if confirmed:
        statements = statements.where(Message.sequence > confirmed["source_sequence"])
    for message in db.scalars(statements.order_by(Message.sequence.desc())):
        if is_correction(message.text):
            return message
    return None


def _statement(message):
    if message is None:
        return None
    return {
        "source": "CUSTOMER_STATEMENT",
        "message_id": message.id,
        "sequence": message.sequence,
        "text": message.text,
        "sha256": digest([message.sequence, message.text]),
    }


def retained_attachments(db, row, attachment_ids, correction_sequence):
    if not attachment_ids:
        return []
    items = db.execute(
        select(Attachment, Message.sequence)
        .join(Message, Message.id == Attachment.message_id)
        .where(
            Attachment.id.in_(attachment_ids),
            Message.id.in_(
                _statements(row)
                .where(Message.sequence < correction_sequence)
                .with_only_columns(Message.id)
            ),
        )
        .order_by(Attachment.id)
    )
    return [
        {
            "attachment_id": item.id,
            "message_id": item.message_id,
            "sequence": sequence,
            "sha256": item.sha256,
        }
        for item, sequence in items
    ]


def pending_scope(db, row):
    if not row.analysis_issue_id:
        return None
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == row.id,
            Message.role == "customer",
            Message.analysis_issue_id.is_(None),
            Message.sequence >= (row.analysis_from_seq or 1),
            Message.sequence > row.input_floor,
            Message.discarded_at.is_(None),
        )
        .order_by(Message.sequence.desc())
        .limit(1)
    )


def snapshot(db, row, relevant, *, support):
    if row.analysis_issue_id is None:
        return None
    amended = correction(db, row)
    confirmed = confirmation(db, row)
    statement_query = _statements(row).order_by(Message.sequence.desc()).limit(250)
    all_messages = list(db.scalars(statement_query))
    messages = [
        item
        for item in all_messages
        if not confirmed or item.sequence >= confirmed["source_sequence"]
    ]
    latest = next((message for message in messages if relevant(message.text)), None)
    primary = primary_statement(
        messages,
        fallback=next(
            (
                item
                for item in reversed(messages)
                if relevant(item.text) and not _POLICY.search(item.text)
            ),
            None,
        ),
    )
    pending = pending_scope(db, row)
    evidence_scope = _statements(row)
    evidence = db.execute(
        select(Attachment, Message.sequence)
        .join(Message, Message.id == Attachment.message_id)
        .where(Message.id.in_(evidence_scope.with_only_columns(Message.id)))
        .order_by(Message.sequence.desc(), Attachment.position)
        .limit(21)
    ).all()
    return {
        "schema_version": 1,
        "issue_id": row.analysis_issue_id,
        "conversation_revision": row.revision,
        "state": (
            "CORRECTION_NEEDS_SUPPORT"
            if amended
            else "PENDING_SCOPE_CONFIRMATION"
            if pending
            else "CURRENT"
        ),
        "confirmed_context": {
            "case_id": row.active_case_id,
            "order_id": row.active_order_id,
            "line_ids": sorted(row.line_ids),
        },
        "latest_request": _statement(pending or primary),
        "latest_update": _statement(latest),
        "requested_action": requested_action_record(primary_statement(all_messages))
        if not pending
        else None,
        "correction": _statement(amended),
        "confirmed_correction": (
            confirmed
            if support or confirmed is None
            else {key: value for key, value in confirmed.items() if key != "evidence_reason"}
        ),
        "recent_attachments": [
            {
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "message_id": attachment.message_id,
                "sequence": sequence,
                "sha256": attachment.sha256,
                "current": not confirmed
                or sequence >= confirmed["source_sequence"]
                or any(
                    item["attachment_id"] == attachment.id
                    for item in confirmed["retained_attachments"]
                ),
            }
            for attachment, sequence in evidence[:20]
        ],
        "has_more_attachments": len(evidence) > 20,
    }
