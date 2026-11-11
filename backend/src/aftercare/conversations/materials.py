"""Staff confirmation of one scoped chat original as a typed business material."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import MATERIAL_KINDS, digest
from aftercare.business.storage import verify_object
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, Observation
from aftercare.db import User

from .decisions import ConversationDecisions
from .tables import Attachment, ChatMaterialSource, Message


def _error(code, message, status=409):
    return BusinessError(code, message, status)


def _active(db, business, actor, conversation_id):
    if not getattr(business.settings, "chat_decisions_enabled", False) or actor.role != "support":
        raise _error("CHAT_MATERIAL_NOT_FOUND", "未找到可处理的聊天材料。", 404)
    # The caller acquires the conversation before the case (order -> case).
    conversation = ConversationDecisions(business)._conversation(db, actor, conversation_id)
    owner = db.get(User, actor.user_id, with_for_update={"read": True})
    if owner is None or not owner.enabled or owner.role != "support":
        raise _error("CHAT_MATERIAL_NOT_FOUND", "未找到可处理的聊天材料。", 404)
    if (
        conversation.active_case_id is None
        or conversation.analysis_issue_id is None
        or conversation.require_new_case
        or conversation.status == "CLOSED"
    ):
        raise _error("CHAT_MATERIAL_SCOPE_CHANGED", "当前会话的售后事项已变化。")
    return conversation


def _attachments(conversation):
    return (
        select(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .where(
            Message.conversation_id == conversation.id,
            Message.analysis_issue_id == conversation.analysis_issue_id,
            Message.role == "customer",
            Message.sequence.is_not(None),
            Message.discarded_at.is_(None),
        )
    )


def _case(db, business, actor, conversation):
    case, _ = business._case(db, conversation.active_case_id, actor, lock=True)
    if (
        (case.decision_conversation_id, case.decision_issue_id)
        != (conversation.id, conversation.analysis_issue_id)
        or case.customer_id != conversation.customer_id
        or case.order_id != conversation.active_order_id
        or sorted(case.line_ids) != sorted(conversation.line_ids)
    ):
        raise _error("CHAT_MATERIAL_SCOPE_CHANGED", "当前会话的售后事项已变化。")
    return case


def candidates(business, actor, conversation_id, *, after=0, limit=50):
    with Session(business.engine) as db, db.begin():
        row = _active(db, business, actor, conversation_id)
        case = _case(db, business, actor, row)
        # Page by whole messages, so selecting a next cursor cannot skip sibling photos.
        sequences = list(
            db.scalars(
                select(Message.sequence)
                .where(
                    Message.conversation_id == row.id,
                    Message.analysis_issue_id == row.analysis_issue_id,
                    Message.role == "customer",
                    Message.sequence > after,
                    Message.discarded_at.is_(None),
                    select(Attachment.id).where(Attachment.message_id == Message.id).exists(),
                )
                .order_by(Message.sequence)
                .limit(limit + 1)
            )
        )
        attachments = list(
            db.scalars(
                _attachments(row)
                .where(Message.sequence.in_(sequences[:limit]))
                .order_by(Message.sequence, Attachment.position)
            )
        )
        suggestions = {}
        ids = [item.id for item in attachments]
        for observation in db.scalars(
            select(Observation)
            .join(ChatAnalysisTurn)
            .where(
                ChatAnalysisTurn.conversation_id == row.id,
                ChatAnalysisTurn.state == "PUBLISHED",
                Observation.attachment_id.in_(ids),
            )
            .order_by(Observation.created_at, Observation.id)
        ):
            suggestions[observation.attachment_id] = observation
        linked = {
            binding.attachment_id: binding
            for binding in db.scalars(
                select(ChatMaterialSource).where(
                    ChatMaterialSource.case_id == case.id, ChatMaterialSource.attachment_id.in_(ids)
                )
            )
        }
        items = []
        for item in attachments:
            hint = suggestions.get(item.id)
            existing = linked.get(item.id)
            items.append(
                {
                    "attachment_id": item.id,
                    "message_id": item.message_id,
                    "sha256": item.sha256,
                    "content_type": item.content_type,
                    "size": item.size,
                    "suggested_kind": (
                        hint.candidate_kind
                        if hint
                        and hint.original_sha256 == item.sha256
                        and hint.candidate_kind in MATERIAL_KINDS
                        else None
                    ),
                    "material_id": existing.material_id if existing else None,
                    "confirmed_kind": existing.kind if existing else None,
                }
            )
        return {
            "issue_id": row.analysis_issue_id,
            "case_id": case.id,
            "materials_version": case.materials_version,
            "items": items,
            "next_after": sequences[limit - 1] if len(sequences) > limit else None,
        }


class _Source:
    def __init__(
        self, business, actor, conversation_id, *, attachment_id, case_id, issue_id, kind, version
    ):
        self.business, self.actor = business, actor
        self.conversation_id, self.attachment_id, self.case_id = (
            conversation_id,
            attachment_id,
            case_id,
        )
        self.issue_id, self.kind, self.version = issue_id, kind, version
        self.original = None
        self.scope_identity = None
        self.mutation_scope = f"chat-material:{conversation_id}"
        self.mutation_signature = digest([attachment_id, case_id, issue_id, kind, version])

    def lock(self, db):
        row = _active(db, self.business, self.actor, self.conversation_id)
        if (row.active_case_id, row.analysis_issue_id) != (self.case_id, self.issue_id):
            raise _error("CHAT_MATERIAL_SCOPE_CHANGED", "当前会话的售后事项已变化。")
        current = (row.customer_id, row.active_order_id, tuple(sorted(row.line_ids)))
        if self.scope_identity is not None and current != self.scope_identity:
            raise _error("CHAT_MATERIAL_SCOPE_CHANGED", "当前会话的售后事项已变化。")
        self.scope_identity = current
        attachment = db.scalar(_attachments(row).where(Attachment.id == self.attachment_id))
        if attachment is None:
            raise _error("CHAT_MATERIAL_NOT_FOUND", "未找到可处理的聊天材料。", 404)
        from .service import ConversationService

        canonical = ConversationService._object_key(
            row.id, attachment.message_id, attachment.position, attachment.sha256
        )
        if attachment.object_key != canonical:
            raise _error("CHAT_MATERIAL_SOURCE_CHANGED", "原照片记录已变化，请重新核对。")
        identity = (
            attachment.message_id,
            attachment.object_key,
            attachment.sha256,
            attachment.content_type,
            attachment.size,
        )
        if self.original is not None and identity != self.original:
            raise _error("CHAT_MATERIAL_SOURCE_CHANGED", "原照片记录已变化，请重新核对。")
        self.original = identity
        return row

    def validate(self, db, case):
        if (case.decision_conversation_id, case.decision_issue_id) != (
            self.conversation_id,
            self.issue_id,
        ) or (case.customer_id, case.order_id, tuple(sorted(case.line_ids))) != self.scope_identity:
            raise _error("CHAT_MATERIAL_SCOPE_CHANGED", "当前会话的售后事项已变化。")
        self.business._editable(case)
        if case.materials_version != self.version:
            raise _error("CHAT_MATERIAL_VERSION_CHANGED", "材料列表已变化，请刷新后确认。")
        binding = db.get(ChatMaterialSource, (self.case_id, self.attachment_id))
        if binding is not None and binding.kind != self.kind:
            raise _error(
                "CHAT_MATERIAL_KIND_CONFIRMED", "该原照片已确认材料类型，请由客服核对原记录。"
            )

    def verify_bytes(self, sha256, content_type, size):
        if (sha256, content_type, size) != self.original[2:]:
            raise _error("CHAT_MATERIAL_SOURCE_CHANGED", "原照片校验失败，请重新核对。")

    def record(self, db, case, material):
        message_id, _, sha256, _, _ = self.original
        source = {
            "attachment_id": self.attachment_id,
            "message_id": message_id,
            "issue_id": self.issue_id,
            "sha256": sha256,
        }
        existing = db.get(ChatMaterialSource, (self.case_id, self.attachment_id))
        if existing is None:
            db.flush()
            db.add(
                ChatMaterialSource(
                    **source,
                    case_id=case.id,
                    material_id=material["id"],
                    conversation_id=self.conversation_id,
                    kind=self.kind,
                    actor_id=self.actor.user_id,
                    created_at=self.business.clock(),
                )
            )
        elif existing.material_id != material["id"] or existing.sha256 != sha256:
            raise _error("CHAT_MATERIAL_SOURCE_CHANGED", "原照片关联已变化，请重新核对。")
        return {"material": material, "materials_version": case.materials_version, "source": source}


def confirm(
    business, actor, conversation_id, *, attachment_id, kind, case_id, issue_id, version, key
):
    if kind not in MATERIAL_KINDS:
        raise _error("INVALID_MATERIAL_KIND", "请选择有效的材料类型。", 422)
    source = _Source(
        business,
        actor,
        conversation_id,
        attachment_id=attachment_id,
        kind=kind,
        case_id=case_id,
        issue_id=issue_id,
        version=version,
    )
    with Session(business.engine) as db, db.begin():
        row = source.lock(db)
        case = _case(db, business, actor, row)
        replay = business._replay(db, actor, source.mutation_scope, key, source.mutation_signature)
        if replay is not None:
            return replay
        source.validate(db, case)
        case_version = case.version
    _, object_key, sha256, _, size = source.original
    content = business.storage.get(object_key, size)
    verify_object(content, size, sha256)
    return business._upload(
        case_id,
        actor,
        kind=kind,
        content=content,
        expected_version=case_version,
        idempotency_key=key,
        chat_source=source,
    )
