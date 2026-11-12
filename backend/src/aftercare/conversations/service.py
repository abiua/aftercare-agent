"""Conversation persistence and deterministic first-stage replies."""

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Case, Customer, Execution, Order
from aftercare.business.refund_lookup import locate_refund, resolve_refund_text, targeted_refund
from aftercare.business.rules import digest, utc
from aftercare.business.storage import MAX_BYTES, validate_image, verify_object
from aftercare.intake import _order_resolution, rule_intents

from . import analysis as chat_analysis
from . import decisions as chat_decisions
from . import memory as chat_memory
from .tables import Attachment, CaseLink, Conversation, Message

MAX_ATTACHMENTS = 6
STAGING_TTL = timedelta(hours=24)
NEW_ISSUE = re.compile(r"另开|另建|另一个(?:售后|问题)|新(?:建)?(?:售后|工单|事项)|另外一个问题")


def _id():
    return str(uuid4())


def _iso(value):
    return utc(value).isoformat()


def _conversation(row):
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "status": row.status,
        "revision": row.revision,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "active_case_id": row.active_case_id,
        "active_order_id": row.active_order_id,
    }


def _card(kind, **data):
    return {"type": kind, "data": data}


def _status(code, **data):
    return _card("status", code=code, **data)


def _customer(user):
    if user.role != "customer" or not user.customer_id:
        raise BusinessError("CONVERSATION_CUSTOMER_REQUIRED", "请使用客户身份确认售后对象。", 403)


def _scope(user):
    if user.role == "support":
        return ()
    _customer(user)
    return (Conversation.customer_id == user.customer_id,)


@dataclass(frozen=True)
class Photo:
    filename: str
    data: bytes
    content_type: str
    checksum: str


class ConversationService:
    def __init__(self, business):
        self.business = business
        self.engine, self.clock, self.storage = business.engine, business.clock, business.storage
        self.analysis = chat_analysis.scheduler(self.engine, business.settings, clock=self.clock)
        self.decisions_enabled = chat_decisions.enabled(business.settings)

    @staticmethod
    def _find(db, ident, user, *, lock=False):
        query = select(Conversation).where(Conversation.id == ident, *_scope(user))
        if lock:
            query = query.with_for_update()
        row = db.scalar(query)
        if row is None:
            raise BusinessError("NOT_FOUND", "会话不存在。", 404)
        return row

    @staticmethod
    def _check_revision(row, expected):
        if row.revision != expected:
            raise BusinessError("VERSION_CONFLICT", "会话已更新，请刷新后继续。")

    @staticmethod
    def _editable(row):
        if row.status == "CLOSED":
            raise BusinessError("CONVERSATION_CLOSED", "此会话已关闭。")

    def _touch(self, row):
        row.revision += 1
        row.updated_at = self.clock()

    def _publish(self, row, message):
        row.sequence += 1
        message.sequence = row.sequence
        message.created_at = self.clock()

    def _reply(self, db, row, text, cards, *, reply_to=None, actor_id=None):
        message = Message(
            id=_id(),
            conversation_id=row.id,
            role="assistant",
            actor_id=actor_id,
            text=text,
            cards=cards,
            reply_to_id=reply_to,
            created_at=self.clock(),
        )
        self._publish(row, message)
        db.add(message)
        return message

    def _messages(self, db, row, messages, *, support):
        items = {message.id: [] for message in messages}
        if items:
            for attachment in db.scalars(
                select(Attachment)
                .where(Attachment.message_id.in_(items))
                .order_by(Attachment.message_id, Attachment.position)
            ):
                items[attachment.message_id].append(
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "size": attachment.size,
                        "sha256": attachment.sha256,
                        "content_path": f"/api/conversations/{row.id}/attachments/{attachment.id}/content",
                    }
                )
        return [
            {
                "id": message.id,
                "conversation_id": row.id,
                "sequence": message.sequence,
                "role": message.role,
                "text": message.text,
                "created_at": _iso(message.created_at),
                "attachments": items[message.id],
                "cards": (
                    message.cards
                    if support
                    else [
                        _status("CORRECTION_CONFIRMED")
                        if card["data"].get("code") == "CORRECTION_CONFIRMED"
                        else card
                        for card in message.cards
                    ]
                ),
            }
            for message in messages
        ]

    def _response(self, db, row, messages, user):
        db.flush()
        support = user.role == "support"
        result = {
            "conversation": _conversation(row),
            "messages": self._messages(db, row, messages, support=support),
        }
        if snapshot := chat_analysis.snapshot(self.analysis, db, row):
            result["analysis"] = snapshot
        if memory := chat_memory.snapshot(db, row, chat_decisions._relevant, support=support):
            result["memory"] = memory
        if self.decisions_enabled:
            result["features"] = {"chat_decisions": True, "chat_material_bridge": True}
        return result

    def create(self, user, key):
        _customer(user)
        scope, signature = "chat:create", digest({})
        with Session(self.engine) as db, db.begin():
            customer = db.get(Customer, user.customer_id, with_for_update=True)
            if customer is None:
                raise BusinessError("NOT_FOUND", "客户不存在。", 404)
            if (replay := self.business._replay(db, user, scope, key, signature)) is not None:
                return replay
            stamp = self.clock()
            row = Conversation(
                id=_id(),
                customer_id=user.customer_id,
                status="OPEN",
                revision=1,
                sequence=0,
                line_ids=[],
                input_floor=0,
                require_new_case=False,
                created_at=stamp,
                updated_at=stamp,
            )
            db.add(row)
            return self.business._remember(db, user, scope, key, signature, _conversation(row))

    def list(self, user, *, limit, offset):
        with Session(self.engine) as db:
            rows = db.scalars(
                select(Conversation)
                .where(*_scope(user))
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(limit)
                .offset(offset)
            )
            return [_conversation(row) for row in rows]

    def unfinished(self, user, *, limit=30):
        # Continue the original thread, never copy or silently attach its materials.
        _customer(user)
        with Session(self.engine) as db:
            rows = db.scalars(
                select(Conversation).join(Case, Case.id == Conversation.active_case_id)
                .where(Conversation.customer_id == user.customer_id,
                       Case.customer_id == user.customer_id,
                       Conversation.status.in_(["OPEN", "HUMAN_HANDOFF"]),
                       Case.status.notin_(["RESOLVED", "EXECUTED", "CLOSED", "REJECTED", "CANCELLED", "COMPLETED"]))
                .order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit)
            )
            return [_conversation(row) for row in rows]

    def detail(self, ident, user, *, after, limit):
        with self.business._snapshot() as db:
            row = self._find(db, ident, user)
            rows = list(
                db.scalars(
                    select(Message)
                    .where(Message.conversation_id == ident, Message.sequence > after)
                    .order_by(Message.sequence)
                    .limit(limit + 1)
                )
            )
            page = rows[:limit]
            result = self._response(db, row, page, user)
            if self.decisions_enabled:
                result["execution_progress"] = self._execution_progress(db, row)
            from .investigation_delivery import enabled as investigation_enabled

            if user.role == "customer" and investigation_enabled(self.business.settings):
                result["investigation_progress"] = self._investigation_progress(db, row)
            return {
                **result,
                "has_more": len(rows) > limit,
                "next_after": page[-1].sequence if page else after,
            }

    def _investigation_progress(self, db, row):
        from aftercare.conversation_analysis.service import _scope as analysis_scope
        from aftercare.conversation_analysis.tables import ChatAnalysisTurn
        from aftercare.db import User
        from aftercare.decisions.tables import DecisionHead, DecisionSnapshot
        from aftercare.intelligence.decision_candidate import DECISION_READY_EVENT, load_binding
        from aftercare.intelligence.tables import AgentRun, RunEvent

        from .tables import DecisionProjection, InvestigationRequest

        if (
            row.status != "OPEN"
            or row.require_new_case
            or not row.active_case_id
            or not row.analysis_issue_id
        ):
            return None
        case = db.get(Case, row.active_case_id)
        if case is None or (
            case.customer_id,
            case.order_id,
            case.decision_conversation_id,
            case.decision_issue_id,
        ) != (row.customer_id, row.active_order_id, row.id, row.analysis_issue_id):
            return None
        if case.status in {"EXECUTION_PENDING", "RESOLVED", "EXECUTION_FAILED"}:
            return None
        head = db.get(DecisionHead, (row.id, row.analysis_issue_id))
        turn = db.scalar(
            select(ChatAnalysisTurn)
            .where(ChatAnalysisTurn.conversation_id == row.id)
            .order_by(ChatAnalysisTurn.generation.desc())
            .limit(1)
        )
        if turn is None or (turn.state, turn.revision, turn.customer_id, turn.scope_sha256) != (
            "PUBLISHED",
            row.revision,
            row.customer_id,
            analysis_scope(row),
        ):
            return None
        request = db.get(InvestigationRequest, turn.id)
        if request is None or (
            request.conversation_id,
            request.issue_id,
            request.case_id,
            request.actor_id,
        ) != (row.id, row.analysis_issue_id, row.active_case_id, turn.actor_id):
            return None
        owner = db.get(User, request.actor_id)
        if (
            owner is None
            or not owner.enabled
            or owner.role != "customer"
            or owner.customer_id != row.customer_id
        ):
            return None
        scope = request.scope
        lines = scope.get("line_ids") if isinstance(scope, dict) else None
        if (
            not isinstance(scope, dict)
            or digest(
                [
                    scope.get("customer_id"),
                    scope.get("order_id"),
                    scope.get("case_id"),
                    sorted(lines)
                    if isinstance(lines, list) and all(isinstance(line, str) for line in lines)
                    else None,
                ]
            )
            != turn.scope_sha256
            or (scope.get("conversation_id"), scope.get("issue_id"))
            != (row.id, row.analysis_issue_id)
        ):
            return (
                {"state": "NEEDS_SUPPORT"} if request.state == "BLOCKED" and scope is None else None
            )
        if head and (head.offer_id or head.review_id):
            reviewed = db.get(DecisionSnapshot, head.snapshot_id)
            dependency = scope.get("dependency_sha256")
            if (
                reviewed is None
                or not isinstance(dependency, str)
                or reviewed.input.get("dependency_sha256") == dependency
            ):
                return None
        if request.state == "PENDING":
            return {"state": "QUEUED"}
        if request.state == "BLOCKED":
            return {"state": "NEEDS_SUPPORT"}
        if request.state != "STARTED" or not request.run_id:
            return None
        run = db.get(AgentRun, request.run_id)
        if run is None or run.owner_id != request.actor_id or run.case_id != request.case_id:
            return None
        try:
            binding = load_binding(db, run)
        except BusinessError:
            return None
        if (
            not binding
            or binding.get("decision_turn_id") != turn.id
            or binding.get("scope") != scope
        ):
            return None
        projection = db.get(DecisionProjection, run.id)
        if projection and projection.state == "PUBLISHED":
            if not head or not projection.snapshot_id or head.snapshot_id != projection.snapshot_id:
                return None
            snapshot = db.get(DecisionSnapshot, projection.snapshot_id)
            if snapshot is None or (
                snapshot.conversation_id,
                snapshot.issue_id,
                snapshot.run_id,
            ) != (row.id, row.analysis_issue_id, run.id):
                return None
            return {
                "state": "PENDING_SUPPORT"
                if utc(snapshot.expires_at) > utc(self.business.clock())
                else "NEEDS_SUPPORT"
            }
        if (projection and projection.state == "BLOCKED") or run.status in {"FAILED", "CANCELED"}:
            return {"state": "NEEDS_SUPPORT"}
        if run.status == "QUEUED":
            return {"state": "QUEUED"}
        if run.status == "RUNNING":
            return {"state": "CHECKING"}
        if run.status in {"WAITING_INPUT", "COMPLETED", "NEEDS_REVIEW"}:
            ready = db.scalar(
                select(RunEvent.seq)
                .where(RunEvent.run_id == run.id, RunEvent.kind == DECISION_READY_EVENT)
                .limit(1)
            )
            return {"state": "CHECKING" if ready is not None else "NEEDS_SUPPORT"}
        return None

    @staticmethod
    def _execution_progress(db, row):
        if not row.active_case_id or not row.active_order_id or not row.analysis_issue_id:
            return None
        execution = db.scalar(
            select(Execution)
            .join(Case, Case.id == Execution.case_id)
            .join(CaseLink, CaseLink.case_id == Case.id)
            .where(
                CaseLink.conversation_id == row.id,
                Case.id == row.active_case_id,
                Case.customer_id == row.customer_id,
                Case.order_id == row.active_order_id,
                Case.decision_conversation_id == row.id,
                Case.decision_issue_id == row.analysis_issue_id,
                Execution.order_id == row.active_order_id,
                Execution.action.in_(("REFUND", "REPLACE")),
            )
            .order_by(Execution.updated_at.desc(), Execution.id.desc())
            .limit(1)
        )
        if execution is None:
            return None
        receipt = execution.receipt if isinstance(execution.receipt, dict) else {}
        receipt_id = receipt.get("id")
        terminal = execution.status in {"SUCCEEDED", "FAILED"}
        matching_receipt = (
            receipt.get("amount_cents") == execution.amount_cents
            if execution.action == "REFUND"
            else receipt.get("result") == "STOCK_RECORDED"
            and receipt.get("lines") == execution.lines
        )
        confirmed = (
            terminal
            and receipt.get("mode") == "SIMULATED"
            and isinstance(receipt_id, str)
            and bool(receipt_id.strip())
            and matching_receipt
        )
        measure = "amount_cents" if execution.action == "REFUND" else "quantity"
        return {
            "id": execution.id,
            "case_id": execution.case_id,
            "action": execution.action,
            "status": execution.status
            if confirmed or execution.status == "SUBMITTING"
            else "UNKNOWN",
            "lines": [
                {"line_id": line["line_id"], measure: line[measure]} for line in execution.lines
            ],
            "amount_cents": execution.amount_cents,
            "simulated": True,
            "receipt_confirmed": confirmed,
            "updated_at": _iso(execution.updated_at),
        }

    @staticmethod
    def _photos(files):
        if len(files) > MAX_ATTACHMENTS or sum(len(data) for _, data in files) > MAX_BYTES:
            raise BusinessError(
                "ATTACHMENTS_TOO_LARGE", "每条消息最多 6 张照片，总大小不超过 8 MiB。", 413
            )
        photos = []
        for filename, data in files:
            kind, checksum = validate_image(data)
            name = filename.replace("\\", "/").rsplit("/", 1)[-1]
            name = "".join(c for c in name if c.isprintable()).strip()[:160] or "photo"
            photos.append(Photo(name, data, kind, checksum))
        return photos

    def _message(self, db, ident, client_id):
        return db.scalar(
            select(Message).where(
                Message.conversation_id == ident, Message.client_message_id == client_id
            )
        )

    @staticmethod
    def _check_message(message, user, signature):
        if message.actor_id != user.user_id or message.request_hash != signature:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "同一消息编号已用于不同内容。")
        if message.discarded_at is not None:
            raise BusinessError("UPLOAD_EXPIRED", "暂存已过期，请重新发送消息。", 410)

    def _message_response(self, db, row, message, user):
        replies = list(
            db.scalars(
                select(Message).where(Message.reply_to_id == message.id).order_by(Message.sequence)
            )
        )
        return self._response(db, row, [message, *replies], user)

    def message(self, ident, user, client_id, text, files, key):
        # Check owner before decoding uploads or touching storage.
        with Session(self.engine) as db:
            self._find(db, ident, user)
        photos = self._photos(files)

        if len(text) > 2000 or (not text.strip() and not photos):
            raise BusinessError("INVALID_MESSAGE", "请输入不超过 2000 字的消息或添加照片。", 422)
        signature = digest([client_id, text, [(p.filename, p.checksum) for p in photos]])
        scope = f"chat:message:{ident}"
        staging_scope = f"chat:stage:{ident}"
        with Session(self.engine) as db, db.begin():
            row = self._find(db, ident, user, lock=True)
            replay = self.business._replay(db, user, scope, key, signature)
            if replay is not None:
                return replay
            stage = self.business._replay(db, user, staging_scope, key, signature)
            message = self._message(db, ident, client_id)
            if message is not None:
                self._check_message(message, user, signature)
                if message.sequence is not None:
                    return self.business._remember(
                        db,
                        user,
                        scope,
                        key,
                        signature,
                        self._message_response(db, row, message, user),
                    )
            else:
                self._editable(row)
                if self.analysis or self.decisions_enabled:
                    chat_analysis.issue(row)
                message = Message(
                    id=_id(),
                    conversation_id=ident,
                    client_message_id=client_id,
                    request_hash=signature,
                    actor_id=user.user_id,
                    role=user.role,
                    text=text,
                    cards=[],
                    created_at=self.clock(),
                    analysis_issue_id=row.analysis_issue_id,
                )
                db.add(message)
                db.flush()
                for position, photo in enumerate(photos):
                    db.add(
                        Attachment(
                            id=_id(),
                            message_id=message.id,
                            position=position,
                            filename=photo.filename,
                            object_key=self._object_key(
                                ident, message.id, position, photo.checksum
                            ),
                            content_type=photo.content_type,
                            size=len(photo.data),
                            sha256=photo.checksum,
                        )
                    )
            message_id = message.id
            if stage is None:
                self.business._remember(
                    db, user, staging_scope, key, signature, {"message_id": message_id}
                )

        with Session(self.engine) as db, db.begin():
            row = self._find(db, ident, user, lock=True)
            replay = self.business._replay(db, user, scope, key, signature)
            if replay is not None:
                return replay
            message = db.get_one(Message, message_id)
            self._check_message(message, user, signature)
            if message.sequence is None:
                self._editable(row)
                if (self.analysis or self.decisions_enabled) and (
                    message.analysis_issue_id is None
                    or message.analysis_issue_id != row.analysis_issue_id
                ):
                    raise BusinessError(
                        "MESSAGE_SCOPE_CHANGED",
                        "上传期间已切换售后事项。若照片属于当前事项，请重新发送。",
                    )
                # Cleanup takes the same conversation lock before deleting staged objects.
                for position, photo in enumerate(photos):
                    object_key = self._object_key(ident, message_id, position, photo.checksum)
                    self.storage.put(object_key, photo.data, photo.content_type)
                    verify_object(
                        self.storage.get(object_key, len(photo.data)),
                        len(photo.data),
                        photo.checksum,
                    )
                self._publish(row, message)
                self._touch(row)
                if user.role == "support":
                    row.status = "HUMAN_HANDOFF"
                    chat_analysis.invalidate(self.analysis, db, row)
                elif row.status == "OPEN":
                    if self.analysis and chat_analysis.wants_handoff(row, message):
                        row.status = "HUMAN_HANDOFF"
                        chat_analysis.invalidate(self.analysis, db, row)
                        reply = "已转客服处理。此前提交的文字和照片会保留。"
                        cards = [_status("HUMAN_HANDOFF")]
                    else:
                        reply, cards = self._customer_reply(db, row, user, message, bool(photos))
                        if self.analysis or self.decisions_enabled:
                            offers_new_issue = any(card["data"].get("new_issue") for card in cards)
                            if offers_new_issue and not chat_memory.is_correction(message.text):
                                # This specific request remains unassigned until confirmed.
                                message.analysis_issue_id = None
                            defer = (
                                not any(
                                    card["data"].get("code", "").startswith("REFUND_")
                                    or card["data"].get("code")
                                    in {
                                        "CORRECTION_NEEDS_SUPPORT",
                                        "INVALID_REFERENCE",
                                        "AMBIGUOUS_REFERENCE",
                                    }
                                    for card in cards
                                )
                                and not offers_new_issue
                            )
                            if (
                                defer
                                and row.status == "OPEN"
                                and not chat_memory.correction(db, row)
                                and not chat_memory.pending_scope(db, row)
                            ):
                                chat_analysis.stage(self.analysis, db, row)
                            else:
                                chat_analysis.invalidate(self.analysis, db, row)
                    self._reply(db, row, reply, cards, reply_to=message.id)
            return self.business._remember(
                db, user, scope, key, signature, self._message_response(db, row, message, user)
            )

    @staticmethod
    def _object_key(conversation_id, message_id, position, checksum):
        return f"conversations/{conversation_id}/{message_id}/{position}-{checksum}"

    def cleanup_staged(self, *, limit=100, execute=False):
        if not 1 <= limit <= 100:
            raise ValueError("cleanup limit must be 1..100")
        cutoff = self.clock() - STAGING_TTL
        with Session(self.engine) as db:
            candidates = db.execute(
                select(Message.id, Message.conversation_id)
                .where(
                    Message.sequence.is_(None),
                    (Message.discarded_at.is_(None) & (Message.created_at <= cutoff))
                    | (
                        Message.discarded_at.is_not(None)
                        & select(Attachment.id).where(Attachment.message_id == Message.id).exists()
                    ),
                )
                .order_by(Message.created_at, Message.id)
                .limit(limit)
            ).all()
        cleaned = 0
        if execute:
            for message_id, conversation_id in candidates:
                with Session(self.engine) as db, db.begin():
                    db.get_one(Conversation, conversation_id, with_for_update=True)
                    message = db.get_one(Message, message_id)
                    if message.sequence is not None or (
                        message.discarded_at is None and utc(message.created_at) > cutoff
                    ):
                        continue
                    attachments = list(
                        db.scalars(select(Attachment).where(Attachment.message_id == message_id))
                    )
                    for attachment in attachments:
                        expected = self._object_key(
                            conversation_id, message_id, attachment.position, attachment.sha256
                        )
                        if attachment.object_key != expected:
                            raise BusinessError(
                                "ATTACHMENT_BINDING_INVALID", "暂存照片登记不一致。", 503
                            )
                    for attachment in attachments:
                        self.storage.delete(attachment.object_key)
                        db.delete(attachment)
                    if message.discarded_at is None or attachments:
                        message.discarded_at = message.discarded_at or self.clock()
                        cleaned += 1
        return {"eligible": len(candidates), "cleaned": cleaned}

    def content(self, ident, attachment_id, user):
        with Session(self.engine) as db:
            self._find(db, ident, user)
            attachment = db.scalar(
                select(Attachment)
                .join(Message, Message.id == Attachment.message_id)
                .where(
                    Attachment.id == attachment_id,
                    Message.conversation_id == ident,
                    Message.sequence.is_not(None),
                )
            )
            if attachment is None:
                raise BusinessError("NOT_FOUND", "照片不存在。", 404)
            data = self.storage.get(attachment.object_key, attachment.size)
            verify_object(data, attachment.size, attachment.sha256)
            return data, attachment.content_type

    @staticmethod
    def _order_ids(db, user, *, customer_id=None):
        return [
            {"id": ident}
            for ident in db.scalars(
                select(Order.id)
                .where(Order.customer_id == (customer_id or user.customer_id))
                .order_by(Order.id)
            )
        ]

    def _candidates(self, db, user, text):
        owned = self._order_ids(db, user)
        match, reason = _order_resolution(text, None, owned)
        orders = (
            [self.business._order_json(db, self.business._order(db, match["id"], user))]
            if match is not None
            else self.business.orders(user, limit=20)
        )
        return _card(
            "order_candidates",
            orders=[self._order_card(order) for order in orders],
            new_issue=False,
        ), reason

    @staticmethod
    def _order_card(order):
        return {key: order[key] for key in ("id", "items", "products", "status", "version")}

    @staticmethod
    def _latest_request(db, row, orders, order_id, *, require_match=False):
        candidates = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == row.id,
                Message.role == "customer",
                Message.sequence > row.input_floor,
            )
            .order_by(Message.sequence.desc())
            .limit(20)
        )
        for message in candidates:
            intents = rule_intents(message.text)
            if (
                chat_memory.is_correction(message.text)
                or not intents
                and NEW_ISSUE.search(message.text)
            ):
                return None, None
            if intents:
                match, reason = _order_resolution(message.text, None, orders)
                if (
                    match
                    and match["id"] != order_id
                    or not match
                    and (require_match or reason in {"MULTIPLE_ORDERS", "ORDER_NOT_AVAILABLE"})
                ):
                    return None, None
                return (message, intents[0]) if len(intents) == 1 else (None, None)
        return None, None

    def _customer_reply(self, db, row, user, message, has_photos):
        card, order_reason = self._candidates(db, user, message.text)
        mentioned = card["data"]["orders"][0]["id"] if order_reason is None else None
        intents = rule_intents(message.text)
        if row.active_case_id and chat_memory.is_correction(message.text):
            cards = [_status("CORRECTION_NEEDS_SUPPORT")]
            if mentioned is not None and mentioned != row.active_order_id:
                card["data"]["new_issue"] = True
                cards.append(card)
            return "已保留你的更正说明。当前工单需要客服核对。", cards
        from .tau_policy import consultation_pending

        consultation = (
            consultation_pending(
                self.business.settings, message.text, available=self.analysis is not None
            )
            if not has_photos
            and not row.active_order_id
            and order_reason in {"ORDER_REQUIRED", "NO_ORDERS"}
            else None
        )
        if consultation is not None:
            return consultation
        if row.active_order_id is None:
            if has_photos and not message.text.strip():
                return "已收到照片。你可以继续补充发生了什么。", []
            if mentioned is None:
                return "请先确认要处理的订单。", [card]
            order = self.business._order(db, mentioned, user, lock=True)
            row.active_order_id = order.id
            items = card["data"]["orders"][0]["items"]
            if len(items) == 1:
                row.line_ids = [items[0]["line_id"]]
            if intents == ["refund_status"]:
                row.input_floor = message.sequence
                reply, cards = self._refund(db, order, message.text)
                return reply, [*cards, _status("ORDER_CONFIRMED", order_id=order.id)]
            if row.line_ids and len(intents) == 1:
                reply, cards = self._link(db, row, user, order, message, intents[0])
                return reply, [*cards, _status("ORDER_CONFIRMED", order_id=order.id)]
            if not row.line_ids:
                return "请确认涉及哪件商品。", [card]
            return "请说一下商品发生了什么，或你想查询什么。", [_status("CLARIFICATION_REQUIRED")]
        if order_reason in {"MULTIPLE_ORDERS", "ORDER_NOT_AVAILABLE"} or (
            mentioned is not None and mentioned != row.active_order_id
        ):
            card["data"]["new_issue"] = True
            return "请确认是否要处理另一笔订单。", [card]
        if intents == ["refund_status"]:
            row.input_floor = message.sequence
            return self._refund(
                db, self.business._order(db, row.active_order_id, user), message.text
            )
        if row.active_case_id is None and row.line_ids and len(intents) == 1:
            order = self.business._order(db, row.active_order_id, user, lock=True)
            return self._link(db, row, user, order, message, intents[0])
        if row.active_case_id:
            case = db.get_one(Case, row.active_case_id)
            if NEW_ISSUE.search(message.text) or (len(intents) == 1 and intents[0] != case.intent):
                order = self.business._order(db, row.active_order_id, user)
                return (
                    "如果这是同一订单的另一个问题，请确认是否另开售后事项。若是更正原描述，可以转客服核对。",
                    [
                        _card(
                            "order_candidates",
                            orders=[self._order_card(self.business._order_json(db, order))],
                            new_issue=True,
                        )
                    ],
                )
        if self.decisions_enabled and row.active_case_id:
            case, _ = self.business._case(db, row.active_case_id, user, lock=True)
            try:
                chat_decisions.enroll(case, row)
            except BusinessError as error:
                if error.code != "CASE_DECISION_ALREADY_MANAGED":
                    raise
                row.status = "HUMAN_HANDOFF"
                return "该工单已关联其他会话，已转客服核对。", [_status(error.code)]
        if has_photos:
            return "已收到照片，会保留在这段会话中。", []
        if row.active_case_id:
            return "已收到补充说明。", []
        if not row.line_ids:
            order = self.business._order(db, row.active_order_id, user)
            return "请确认涉及哪件商品。", [
                _card(
                    "order_candidates",
                    orders=[self._order_card(self.business._order_json(db, order))],
                    new_issue=False,
                )
            ]
        return "请说一下商品发生了什么，或你想查询什么。", [_status("CLARIFICATION_REQUIRED")]

    def _refund(self, db, order, text):
        rows = [record for record in self.business._ledger(db, order) if record.action == "REFUND"]
        if not rows:
            return "目前没查到这笔订单的退款处理记录。", [_status("REFUND_NOT_FOUND", refunds=[])]
        ledger = [
            {
                "id": r.id,
                "action": r.action,
                "status": r.status,
                "amount_cents": r.amount_cents,
                "created_at": _iso(r.created_at),
                "receipt": r.receipt,
            }
            for r in rows
        ]
        lookup = resolve_refund_text(ledger, text)
        if targeted_refund(lookup):
            if lookup["status"] in {"INVALID_REFERENCE", "AMBIGUOUS_REFERENCE"}:
                return "请提供一个完整的退款执行编号。", [_status(lookup["status"])]
            if lookup["status"] == "NOT_FOUND":
                return "未找到这笔订单中与该编号匹配的退款记录。", [
                    _status("REFUND_NOT_FOUND", refunds=[])
                ]
            rows = [record for record in rows if record.id == lookup["execution"]["id"]]
        labels = {
            "SUCCEEDED": "模拟退款执行成功",
            "FAILED": "模拟退款执行失败",
            "UNKNOWN": "结果待核对",
            "SUBMITTING": "提交中",
        }
        refunds = []
        for record in rows[-20:]:
            observed = locate_refund(ledger, record.id)["execution"]
            status = record.status
            if status in {"SUCCEEDED", "FAILED"} and not observed["receipt_confirmed"]:
                status = "UNKNOWN"
            refunds.append(
                {
                    "id": record.id,
                    "status": status,
                    "amount_cents": record.amount_cents,
                    "receipt_confirmed": observed["receipt_confirmed"],
                }
            )
        summaries = "；".join(
            f"{item['id']}：{labels.get(item['status'], '状态待核对')}，"
            f"{item['amount_cents'] // 100}.{item['amount_cents'] % 100:02d} 元"
            for item in refunds
        )
        return f"这笔订单的退款执行记录：{summaries}。实际到账需另行核对。", [
            _status("REFUND_LEDGER", refunds=refunds)
        ]

    def _link(self, db, row, user, order, message, intent):
        description = message.text.strip()
        if len(description) < 5:
            description = f"客户会话说明：{description}"
        try:
            case = self.business._create_case_for_order(
                db,
                user,
                order,
                intent,
                description,
                sorted(row.line_ids),
                None,
                allow_existing=not row.require_new_case,
                allow_closed=False,
            )
        except BusinessError as error:
            if error.code != "CASE_SCOPE_EXISTS":
                raise
            row.status = "HUMAN_HANDOFF"
            return "该商品已有同类工单，已转客服核对。", [_status("CASE_SCOPE_EXISTS")]
        if self.decisions_enabled:
            chat_analysis.issue(row)
            try:
                chat_decisions.enroll(case, row)
            except BusinessError as error:
                if error.code != "CASE_DECISION_ALREADY_MANAGED":
                    raise
                row.status = "HUMAN_HANDOFF"
                return "该工单已关联其他会话，已转客服核对。", [_status(error.code)]
        if (
            db.scalar(
                select(CaseLink.id).where(
                    CaseLink.conversation_id == row.id, CaseLink.case_id == case.id
                )
            )
            is None
        ):
            db.add(
                CaseLink(
                    id=_id(),
                    conversation_id=row.id,
                    case_id=case.id,
                    source_message_id=message.id,
                    created_at=self.clock(),
                )
            )
        row.active_case_id = case.id
        row.input_floor = message.sequence
        row.require_new_case = False
        return "已关联售后工单。你可以继续在这里补充信息。", [
            _card("case_link", case_id=case.id, order_id=order.id)
        ]

    def context(self, ident, user, *, order_id, line_ids, expected_revision, new_issue, key):
        _customer(user)
        scope = f"chat:context:{ident}"
        signature = digest([order_id, sorted(line_ids), expected_revision, new_issue])
        with Session(self.engine) as db, db.begin():
            row = self._find(db, ident, user, lock=True)
            replay = self.business._replay(db, user, scope, key, signature)
            if replay is not None:
                return replay
            self._editable(row)
            self._check_revision(row, expected_revision)
            order = self.business._order(db, order_id, user, lock=True)
            available = {item.line_id for item in self.business._lines(db, order.id)}
            if len(line_ids) != len(set(line_ids)) or not set(line_ids) <= available:
                raise BusinessError("INVALID_LINES", "所选商品不在订单内。", 422)
            claimed = sorted(line_ids or (available if len(available) == 1 else []))
            changing = row.active_order_id is not None and (
                row.active_order_id != order.id
                or bool(row.active_case_id and claimed != row.line_ids)
            )
            if changing and not new_issue:
                raise BusinessError("NEW_ISSUE_CONFIRMATION_REQUIRED", "请先确认开启新事项。")
            switching_order = row.active_order_id not in {None, order.id}
            message, intent = self._latest_request(
                db,
                row,
                self._order_ids(db, user),
                order.id,
                require_match=switching_order,
            )
            chat_analysis.invalidate(self.analysis, db, row)
            if new_issue:
                if self.analysis or self.decisions_enabled:
                    start = chat_analysis.confirmed_issue_start(db, row, message, order.id)
                    chat_analysis.issue(row, reset=True, input_from_seq=start)
                    if start is not None:
                        message.analysis_issue_id = row.analysis_issue_id
                row.active_case_id = None
                row.require_new_case = True
                if message is None:
                    row.input_floor = row.sequence
            if not new_issue and (pending := chat_memory.pending_scope(db, row)):
                row.input_floor = pending.sequence
            row.active_order_id, row.line_ids = order.id, claimed
            self._touch(row)
            if intent == "refund_status":
                response = self._refund(db, order, message.text)
                row.input_floor = message.sequence
            elif not claimed:
                response = (
                    "请选择要处理的商品。",
                    [
                        _card(
                            "order_candidates",
                            orders=[self._order_card(self.business._order_json(db, order))],
                            new_issue=False,
                        )
                    ],
                )
            elif row.active_case_id:
                response = (
                    "已确认当前售后工单。",
                    [_card("case_link", case_id=row.active_case_id, order_id=order.id)],
                )
            elif message and row.status == "OPEN":
                response = self._link(db, row, user, order, message, intent)
            else:
                response = (
                    "已确认订单。请补充说明需要处理的问题。",
                    [_status("CLARIFICATION_REQUIRED")],
                )
            if (
                self.analysis
                and intent != "refund_status"
                and row.active_case_id
                and not chat_memory.correction(db, row)
            ):
                chat_analysis.stage(self.analysis, db, row)
            text, cards = response
            if claimed or intent == "refund_status":
                cards.append(_status("ORDER_CONFIRMED", order_id=order.id))
            reply = self._reply(db, row, text, cards)
            return self.business._remember(
                db, user, scope, key, signature, self._response(db, row, [reply], user)
            )

    def confirm_correction(
        self,
        ident,
        user,
        *,
        message_id,
        expected_revision,
        retained_attachment_ids,
        evidence_reason,
        key,
    ):
        if user.role != "support":
            raise BusinessError("CORRECTION_FORBIDDEN", "请由客服确认更正。", 403)
        scope = f"chat:correction:{ident}"
        retained_ids = sorted(set(retained_attachment_ids))
        if retained_ids and not evidence_reason.strip():
            raise BusinessError(
                "CORRECTION_EVIDENCE_REASON_REQUIRED", "请说明保留照片的原因。", 422
            )
        signature = digest([message_id, expected_revision, retained_ids, evidence_reason])
        with Session(self.engine) as db, db.begin():
            row = self._find(db, ident, user, lock=True)
            if (replay := self.business._replay(db, user, scope, key, signature)) is not None:
                return replay
            self._editable(row)
            self._check_revision(row, expected_revision)
            message = chat_memory.correction(db, row)
            if message is None or message.id != message_id or chat_memory.pending_scope(db, row):
                raise BusinessError("CORRECTION_STALE", "更正信息已更新，请重新核对。")
            later_statements = db.scalars(
                select(Message)
                .where(
                    Message.conversation_id == row.id,
                    Message.analysis_issue_id == row.analysis_issue_id,
                    Message.role == "customer",
                    Message.sequence >= message.sequence,
                    Message.discarded_at.is_(None),
                )
                .order_by(Message.sequence.desc())
            )
            action_source = chat_memory.primary_statement(later_statements)
            if action_source and chat_memory.requested_action(action_source.text) in {
                "WITHDRAWN",
                "NEEDS_CLARIFICATION",
            }:
                raise BusinessError(
                    "CORRECTION_REQUEST_UNCLEAR", "请先在当前会话确认客户是否还有售后诉求。"
                )
            case, order = self.business._case(db, row.active_case_id, user, lock=True)
            if (
                case.customer_id != row.customer_id
                or order.id != row.active_order_id
                or sorted(case.line_ids) != sorted(row.line_ids)
                or case.status in {"APPROVED", "EXECUTION_PENDING", "RESOLVED", "EXECUTION_FAILED"}
                or db.scalar(select(Execution.id).where(Execution.case_id == case.id).limit(1))
            ):
                raise BusinessError("CORRECTION_SCOPE_CHANGED", "请核对工单状态和订单商品。")
            match, reason = _order_resolution(
                message.text, None, self._order_ids(db, user, customer_id=row.customer_id)
            )
            if (match and match["id"] != order.id) or reason in {
                "MULTIPLE_ORDERS",
                "ORDER_NOT_AVAILABLE",
            }:
                raise BusinessError("CORRECTION_SCOPE_CHANGED", "请先确认更正所涉及的订单。")
            retained = chat_memory.retained_attachments(db, row, retained_ids, message.sequence)
            if len(retained) != len(retained_ids):
                raise BusinessError(
                    "CORRECTION_EVIDENCE_SCOPE", "保留照片须来自当前事项的更正前消息。", 422
                )
            # The issue ID survives correction, so retire uploads staged against its old basis.
            db.execute(
                update(Message)
                .where(
                    Message.conversation_id == row.id,
                    Message.analysis_issue_id == row.analysis_issue_id,
                    Message.role == "customer",
                    Message.sequence.is_(None),
                    Message.discarded_at.is_(None),
                )
                .values(discarded_at=self.clock())
            )
            chat_analysis.invalidate(self.analysis, db, row)
            row.analysis_from_seq = message.sequence
            row.analysis_requests = 0
            row.status = "OPEN"
            self._touch(row)
            reply = self._reply(
                db,
                row,
                "客服已确认更正说明，继续处理当前工单。",
                [
                    _status(
                        "CORRECTION_CONFIRMED",
                        issue_id=row.analysis_issue_id,
                        source_message_id=message.id,
                        source_sequence=message.sequence,
                        retained_attachments=retained,
                        evidence_reason=evidence_reason.strip(),
                    )
                ],
                reply_to=message.id,
                actor_id=user.user_id,
            )
            chat_analysis.stage(self.analysis, db, row)
            return self.business._remember(
                db, user, scope, key, signature, self._response(db, row, [reply], user)
            )

    def handoff(self, ident, user, *, expected_revision, key):
        scope = f"chat:handoff:{ident}"
        signature = digest([expected_revision])
        with Session(self.engine) as db, db.begin():
            row = self._find(db, ident, user, lock=True)
            replay = self.business._replay(db, user, scope, key, signature)
            if replay is not None:
                return replay
            self._editable(row)
            self._check_revision(row, expected_revision)
            messages = []
            if row.status != "HUMAN_HANDOFF":
                row.status = "HUMAN_HANDOFF"
                self._touch(row)
                chat_analysis.invalidate(self.analysis, db, row)
                messages.append(self._reply(db, row, "已转客服处理。", [_status("HUMAN_HANDOFF")]))
            return self.business._remember(
                db, user, scope, key, signature, self._response(db, row, messages, user)
            )
