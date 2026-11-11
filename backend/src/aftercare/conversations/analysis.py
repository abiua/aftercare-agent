"""Join committed chat input, the analysis outbox and authorized photo evidence."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.models import Case
from aftercare.business.rules import utc
from aftercare.business.storage import MAX_BYTES
from aftercare.conversation_analysis.audit import ChatAnalysisAudit
from aftercare.conversation_analysis.service import AnalysisLimits, AnalysisService
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, ChatModelAttempt, Observation
from aftercare.intake import rule_intents
from aftercare.intelligence.models import ModelFailure
from aftercare.intelligence.vision import PhotoSource, observe, prepare_photos

from .tables import Attachment, Message

MATERIAL_REVIEW_CHANGED = "MATERIAL_REVIEW_CHANGED"
CANNOT_PROVIDE = ("暂时无法提供", "无法提供", "没法补拍", "无法补拍", "没有照片", "不能拍")
MAX_REQUESTS = 2
EVIDENCE_PROMPTS = {
    "damaged": "如果方便，请补一张能看清损坏位置的商品照片。也可以先用文字描述。",
    "wrong_item": "如果方便，请补收到的商品照片，以及包裹外侧标签照片。可先发手边已有的照片。",
}
LABEL_PROMPT = "照片里可见商品。若方便，只需再发一张包裹外侧标签照片。"
DAMAGE_DETAIL_PROMPT = "这张照片还看不清你说的损坏位置。可以发一张局部照片，或用文字描述具体位置。"
DAMAGE_FINDINGS = {"POSSIBLE_CRACK", "POSSIBLE_DENT", "POSSIBLE_TEAR", "POSSIBLE_LEAK"}
ANALYSIS_LIMIT_ERRORS = {
    "ANALYSIS_BUDGET_EXCEEDED",
    "ANALYSIS_BUDGET_VIOLATION",
    "INVESTIGATION_ISSUE_CALL_LIMIT",
    "INVESTIGATION_ISSUE_COST_LIMIT",
    "INVESTIGATION_ISSUE_COST_UNKNOWN",
    "INVESTIGATION_ISSUE_BUDGET_VIOLATION",
    "MODEL_COST_BUDGET_EXHAUSTED",
    "MODEL_RUN_COST_BUDGET_EXHAUSTED",
}


def scheduler(engine, settings, *, clock=None):
    if not getattr(settings, "chat_analysis_enabled", False):
        return None
    limits = AnalysisLimits(
        max_calls=settings.chat_analysis_max_calls,
        max_microusd=settings.chat_analysis_max_microusd,
    )
    from aftercare.investigation.issue_budget import IssueBudget

    return AnalysisService(
        engine,
        limits,
        issue_budget=IssueBudget.from_settings(settings),
        **({"clock": clock} if clock else {}),
    )


def _id():
    return str(uuid4())


def issue(row, *, reset=False, input_from_seq=None):
    if reset or row.analysis_issue_id is None:
        row.analysis_issue_id = _id()
        row.analysis_from_seq = input_from_seq or row.sequence + 1
        row.analysis_requests = 0


def confirmed_issue_start(db, row, message, order_id):
    if (
        message is None
        or message.analysis_issue_id is not None
        or row.analysis_from_seq is None
        or message.sequence < row.analysis_from_seq
    ):
        return None
    if db.scalar(
        select(Observation.id)
        .join(Attachment, Attachment.id == Observation.attachment_id)
        .where(Attachment.message_id == message.id)
        .limit(1)
    ):
        return None
    replies = db.scalars(
        select(Message).where(Message.conversation_id == row.id, Message.reply_to_id == message.id)
    )
    if any(
        card["type"] == "order_candidates"
        and card["data"].get("new_issue")
        and any(item["id"] == order_id for item in card["data"]["orders"])
        for reply in replies
        for card in reply.cards
    ):
        return message.sequence
    return None


def invalidate(service, db, row):
    if service:
        service.invalidate(db, row)


def stage(service, db, row):
    if not service or row.status != "OPEN" or not row.sequence:
        return
    issue(row)
    db.flush()
    start = row.analysis_from_seq
    if start > row.sequence:
        return
    service.stage(db, row, input_from_seq=start)


def wants_handoff(row, message):
    return bool(row.analysis_requests and any(phrase in message.text for phrase in CANNOT_PROVIDE))


def _latest_public_card(db, row, turn):
    messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == row.id,
            Message.role == "assistant",
            Message.sequence > turn.input_through_seq,
        )
        .order_by(Message.sequence.desc())
        .limit(8)
    )
    for message in messages:
        for card in message.cards:
            data = card["data"]
            if (
                data.get("generation") == turn.generation
                and data.get("issue_id") == row.analysis_issue_id
            ):
                return card
    return None


def snapshot(service, db, row):
    if not service or row.analysis_issue_id is None:
        return None
    turn = service._latest(db, row.id)
    if turn is None and row.status != "HUMAN_HANDOFF":
        return None
    if turn is None or turn.revision != row.revision:
        state = "HUMAN_HANDOFF" if row.status == "HUMAN_HANDOFF" else "COMPLETE"
        return {
            "issue_id": row.analysis_issue_id,
            "generation": (turn.generation + 1) if turn else 1,
            "state_version": 0,
            "state": state,
            "input_through_seq": row.sequence,
            "pending_uploads": 0,
            "wait_until": None,
        }
    pending = 0
    wait = None
    state_version = 3
    if row.status == "HUMAN_HANDOFF":
        state = "HUMAN_HANDOFF"
    elif turn.state == "PENDING":
        cutoff = utc(turn.created_at) + timedelta(seconds=service.limits.upload_wait_seconds)
        if service.clock() < cutoff:
            pending = db.scalar(
                select(func.count(distinct(Message.id)))
                .join(Attachment, Attachment.message_id == Message.id)
                .where(
                    Message.conversation_id == row.id,
                    Message.role == "customer",
                    Message.actor_id == turn.actor_id,
                    Message.analysis_issue_id == row.analysis_issue_id,
                    Message.sequence.is_(None),
                    Message.discarded_at.is_(None),
                    Message.created_at >= turn.created_at,
                )
            )
        state = "WAITING_UPLOADS" if pending else "COLLECTING"
        wait = (cutoff if pending else utc(turn.quiet_until)).isoformat()
        state_version = 1
    elif turn.state == "RUNNING":
        state, state_version = "CHECKING_PHOTOS", 2
        wait = utc(turn.lease_until).isoformat()
    elif turn.state == "PUBLISHED":
        card = _latest_public_card(db, row, turn)
        if card and card["type"] == "evidence_request":
            state = "WAITING_CUSTOMER"
        elif card and card["data"].get("code") == "PHOTO_ANALYSIS_UNAVAILABLE":
            state = "UNAVAILABLE"
        else:
            state = "COMPLETE"
    else:
        state = "UNAVAILABLE"
    return {
        "issue_id": row.analysis_issue_id,
        "generation": turn.generation,
        "state_version": state_version,
        "state": state,
        "input_through_seq": turn.input_through_seq,
        "pending_uploads": pending,
        "wait_until": wait,
    }


def _marker(row, binding):
    return {"issue_id": row.analysis_issue_id, "generation": binding["generation"]}


def _evidence_reply(chat, db, row, binding, prompt, *, once=False):
    count = row.analysis_requests or 0
    already_asked = once and db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == row.id,
            Message.role == "assistant",
            Message.sequence >= row.analysis_from_seq,
            Message.text == prompt,
        )
        .limit(1)
    )
    if count >= MAX_REQUESTS or already_asked:
        _handoff(chat, db, row, binding)
        return
    row.analysis_requests = count + 1
    chat._reply(
        db,
        row,
        prompt,
        [
            {
                "type": "evidence_request",
                "data": {
                    **_marker(row, binding),
                    "request_id": _id(),
                    "prompt": prompt,
                    "alternatives": ["回复文字", "发送已有照片", "暂时无法提供"],
                },
            }
        ],
    )


def _handoff(chat, db, row, binding, *, code="HUMAN_HANDOFF", message=None):
    row.status = "HUMAN_HANDOFF"
    chat._touch(row)
    chat._reply(
        db,
        row,
        message or "已转客服处理。此前提交的文字和照片会保留。",
        [{"type": "status", "data": {"code": code, **_marker(row, binding)}}],
    )


def _text_reply(chat, db, row, binding):
    if not row.active_order_id:
        return
    if getattr(chat.business.settings, "chat_investigation_enabled", False):
        from .decisions import _relevant

        latest = db.scalar(
            select(Message.text).where(
                Message.conversation_id == row.id,
                Message.analysis_issue_id == row.analysis_issue_id,
                Message.role == "customer",
                Message.sequence == binding["input_through_seq"],
            )
        )
        if latest is not None and not _relevant(latest):
            return
    case = db.get(Case, row.active_case_id) if row.active_case_id else None
    intent = case.intent if case else None
    if intent is None:
        texts = db.scalars(
            select(Message.text)
            .where(
                Message.conversation_id == row.id,
                Message.role == "customer",
                Message.sequence.between(binding["input_from_seq"], binding["input_through_seq"]),
            )
            .order_by(Message.sequence.desc())
            .limit(20)
        )
        for text in texts:
            intents = rule_intents(text)
            if intents:
                intent = intents[0] if len(intents) == 1 else None
                break
    prompt = EVIDENCE_PROMPTS.get(intent)
    if prompt is None:
        return
    _evidence_reply(chat, db, row, binding, prompt)


def _photos(chat, binding, refs):
    if len(refs) > 6:
        raise BusinessError("VISION_INPUT_LIMIT", "待查看照片超出单次分析范围。")
    user = UserContext(
        user_id=binding["actor_id"], role="customer", customer_id=binding["customer_id"]
    )
    with Session(chat.engine) as db:
        chat._find(db, binding["conversation_id"], user)
        rows = {
            row.id: row
            for row in db.scalars(
                select(Attachment).where(Attachment.id.in_([ref["id"] for ref in refs]))
            )
        }
        photos = []
        size = 0
        for ref in refs:
            row = rows.get(ref["id"])
            if row is None or row.sha256 != ref["sha256"] or row.message_id != ref["message_id"]:
                raise BusinessError("VISION_INPUT_UNBOUND", "照片来源已变化。")
            if size + row.size > MAX_BYTES:
                raise BusinessError("VISION_INPUT_LIMIT", "待查看照片超出单次分析范围。")
            size += row.size
            photos.append(
                PhotoSource(row.id, row.message_id, row.sha256, row.content_type, row.size)
            )
    return prepare_photos(
        tuple(photos),
        lambda ident: chat.content(binding["conversation_id"], ident, user)[0],
    )


def process(chat, turn_id, settings, model):
    service = chat.analysis
    if service is None:
        return
    lease = service.claim(turn_id)
    if lease is None:
        return
    binding = None
    try:
        binding = service.input_snapshot(lease)
        if not binding["attachments"]:
            with Session(chat.engine) as db:
                turn = db.get_one(ChatAnalysisTurn, turn_id)
                material_review_changed = turn.reason == MATERIAL_REVIEW_CHANGED
            if material_review_changed:
                service.publish_status(lease, lambda *_: None)
            else:
                service.publish_status(lease, lambda db, row: _text_reply(chat, db, row, binding))
            return
        user = UserContext(
            user_id=binding["actor_id"], role="customer", customer_id=binding["customer_id"]
        )
        with Session(chat.engine) as db:
            row = chat._find(db, binding["conversation_id"], user)
            case = db.get(Case, row.active_case_id) if row.active_case_id else None
            scoped = row.active_order_id is not None and case and case.intent in EVIDENCE_PROMPTS
            attempts = list(
                db.scalars(
                    select(ChatModelAttempt)
                    .where(ChatModelAttempt.conversation_id == row.id)
                    .limit(service.limits.max_calls)
                )
            )
            history = list(
                db.execute(
                    select(
                        Observation.attachment_id, Observation.original_sha256, Observation.findings
                    )
                    .join(ChatAnalysisTurn, ChatAnalysisTurn.id == Observation.turn_id)
                    .where(
                        ChatAnalysisTurn.conversation_id == row.id,
                        ChatAnalysisTurn.scope_sha256 == binding["scope_sha256"],
                        ChatAnalysisTurn.state == "PUBLISHED",
                    )
                ).all()
            )
        observed = {(item.attachment_id, item.original_sha256) for item in history}
        new_photos = [
            ref for ref in binding["attachments"] if (ref["id"], ref["sha256"]) not in observed
        ]
        if not scoped:
            service.publish_status(lease, lambda *_: None)
            return
        if not new_photos:
            service.publish_status(lease, lambda *_: None)
            return
        if len(attempts) >= service.limits.max_calls or any(
            attempt.state in {"STARTED", "UNKNOWN"}
            or attempt.estimated_cost_microusd is None
            or attempt.budget_violation
            for attempt in attempts
        ):
            service.publish_status(lease, lambda db, row: _handoff(chat, db, row, binding))
            return
        if not settings.chat_vision_enabled or model is None:
            service.publish_status(
                lease,
                lambda db, row: chat._reply(
                    db,
                    row,
                    "照片已保存。当前无法自动查看照片，你可以转客服处理。",
                    [
                        {
                            "type": "status",
                            "data": {"code": "PHOTO_ANALYSIS_UNAVAILABLE", **_marker(row, binding)},
                        }
                    ],
                ),
            )
            return
        try:
            photos = _photos(chat, binding, new_photos)
        except BusinessError as error:
            if error.code != "VISION_INPUT_LIMIT":
                raise
            service.publish_status(
                lease,
                lambda db, row: _handoff(
                    chat,
                    db,
                    row,
                    binding,
                    code="PHOTO_BATCH_UNANALYZED",
                    message="待查看照片超过单次处理上限。本批待查看照片尚未分析，已保留原图并转客服核对。",
                ),
            )
            return
        limited = model.with_evaluation_limits(
            calls=1, budget=Decimal(settings.chat_analysis_max_microusd) / 1_000_000
        )
        result = observe(
            limited,
            ChatAnalysisAudit(service, lease),
            photos,
            preferred=settings.chat_vision_alias,
            input_binding=binding["turn_id"],
            data_scope="customer_attachment",
        )

        def emit(db, row, public):
            cards = [
                {
                    "type": "photo_observation",
                    "data": {
                        **_marker(row, binding),
                        "attachment_refs": [item["attachment_id"]],
                        "summary": item["summary"],
                        "certainty": item["certainty"],
                    },
                }
                for item in public
            ]
            chat._reply(db, row, "以下为本次查看照片的观察，仍需核对。", cards)
            findings = {value for item in history for value in item.findings}
            findings.update(value for item in result["observations"] for value in item["findings"])
            if (
                case.intent == "wrong_item"
                and "ITEM_VISIBLE" in findings
                and "LABEL_VISIBLE" not in findings
            ):
                _evidence_reply(chat, db, row, binding, LABEL_PROMPT, once=True)
            elif (
                case.intent == "damaged"
                and "NO_VISIBLE_DAMAGE" in findings
                and not DAMAGE_FINDINGS & findings
            ):
                _evidence_reply(chat, db, row, binding, DAMAGE_DETAIL_PROMPT, once=True)
            elif public and all(item["certainty"] == "UNCERTAIN" for item in public):
                _evidence_reply(
                    chat, db, row, binding, "如果方便，请补一张更清楚的照片。也可以用文字说明。"
                )

        service.publish(lease.turn_id, lease.token, result["observations"], emit)
    except (BusinessError, ModelFailure) as error:
        try:
            if binding is None:
                service.fail_turn(lease, error.code)
                return
            reason = (
                "本次照片未能在自动查看额度内完成，已转客服核对。原图会保留。"
                if error.code in ANALYSIS_LIMIT_ERRORS
                else "本次未能得到可用的照片观察，已转客服核对。原图会保留。"
            )
            service.fail_turn(
                lease,
                error.code,
                emit=lambda db, row: _handoff(
                    chat, db, row, binding, code="PHOTO_ANALYSIS_HANDOFF", message=reason
                ),
            )
        except BusinessError as stale:
            if stale.code != "ANALYSIS_STALE":
                raise
