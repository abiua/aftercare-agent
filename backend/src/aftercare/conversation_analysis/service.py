"""Durable scheduling and publish fences for private conversation analysis."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest, now, utc
from aftercare.conversations.tables import Attachment, Conversation, Message
from aftercare.db import User

from .tables import ChatAnalysisTurn, ChatModelAttempt, Observation

_PHOTO_KINDS = {"ITEM", "PARCEL", "WRONG_ITEM", "LABEL", "OTHER", "UNDETERMINED"}
_FINDING_LABELS = {
    "POSSIBLE_CRACK": "疑似裂纹",
    "POSSIBLE_DENT": "疑似凹痕",
    "POSSIBLE_TEAR": "疑似破口",
    "POSSIBLE_LEAK": "疑似渗漏",
    "LABEL_VISIBLE": "可见标签",
    "PRINTED_MARKINGS": "可见印刷标记",
    "ITEM_VISIBLE": "可见商品",
    "PARCEL_VISIBLE": "可见包裹",
    "NO_VISIBLE_DAMAGE": "未看见明显破损",
    "BLUR": "画面模糊",
    "OCCLUDED": "画面有遮挡",
    "UNCERTAIN_CONTENT": "照片内容不明确",
}


def _fail(code):
    raise BusinessError(code, "当前会话调查无法继续。")


def _scope(conversation):
    return digest(
        [
            conversation.customer_id,
            conversation.active_order_id,
            conversation.active_case_id,
            sorted(conversation.line_ids),
        ]
    )


def _hex(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class AnalysisLimits:
    max_calls: int
    max_microusd: int
    quiet_seconds: int = 3
    window_seconds: int = 10
    upload_wait_seconds: int = 30
    execution_seconds: int = 60
    lease_seconds: int = 60

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("Analysis limits must be positive integers")
        if not self.quiet_seconds <= self.window_seconds <= self.upload_wait_seconds:
            raise ValueError("Invalid analysis scheduling windows")


@dataclass(frozen=True)
class TurnLease:
    turn_id: str
    token: str


class AnalysisService:
    def __init__(self, engine, limits: AnalysisLimits, *, clock=now):
        self.engine, self.limits, self.clock = engine, limits, clock

    @staticmethod
    def _latest(db, conversation_id):
        return db.scalar(
            select(ChatAnalysisTurn)
            .where(
                ChatAnalysisTurn.conversation_id == conversation_id,
            )
            .order_by(ChatAnalysisTurn.generation.desc())
            .limit(1)
        )

    @staticmethod
    def _attempt(db, turn_id, *, lock=False):
        stmt = select(ChatModelAttempt).where(ChatModelAttempt.turn_id == turn_id)
        return db.scalar(stmt.with_for_update() if lock else stmt)

    @staticmethod
    def _authorized(db, conversation, turn):
        user = db.get(User, turn.actor_id, with_for_update={"read": True})
        return bool(
            user
            and user.enabled
            and user.role == "customer"
            and user.customer_id == conversation.customer_id
        )

    def _current(self, db, conversation, turn):
        return (
            conversation.status == "OPEN"
            and conversation.customer_id == turn.customer_id
            and conversation.revision == turn.revision
            and _scope(conversation) == turn.scope_sha256
            and self._latest(db, conversation.id).id == turn.id
            and self._authorized(db, conversation, turn)
        )

    def _locked(self, db, turn_id):
        ident = db.scalar(
            select(ChatAnalysisTurn.conversation_id).where(ChatAnalysisTurn.id == turn_id)
        )
        if ident is None:
            _fail("ANALYSIS_NOT_FOUND")
        conversation = db.get_one(Conversation, ident, with_for_update=True)
        turn = db.get_one(ChatAnalysisTurn, turn_id, with_for_update=True)
        return conversation, turn

    def stage(self, db, conversation, *, input_from_seq: int):
        """Register committed input in the caller's conversation-locked transaction."""
        if conversation.status != "OPEN":
            return None
        if not 1 <= input_from_seq <= conversation.sequence:
            raise ValueError("Invalid input sequence")
        previous = self._latest(db, conversation.id)
        if previous is not None and previous.revision == conversation.revision:
            if previous.input_from_seq != input_from_seq or previous.scope_sha256 != _scope(
                conversation
            ):
                _fail("ANALYSIS_INPUT_CONFLICT")
            return previous
        inputs = (
            Message.conversation_id == conversation.id,
            Message.role == "customer",
            Message.sequence.between(input_from_seq, conversation.sequence),
            Message.discarded_at.is_(None),
            Message.analysis_issue_id == conversation.analysis_issue_id,
        )
        author = db.scalar(
            select(Message.actor_id).where(*inputs).order_by(Message.sequence.desc()).limit(1)
        )
        if not author:
            return None
        refs = [
            {
                "id": attachment.id,
                "message_id": attachment.message_id,
                "sequence": sequence,
                "position": attachment.position,
                "sha256": attachment.sha256,
            }
            for attachment, sequence in db.execute(
                select(Attachment, Message.sequence)
                .join(Message, Message.id == Attachment.message_id)
                .where(*inputs)
                .order_by(Message.sequence, Attachment.position)
            )
        ]
        stamp = self.clock()
        collecting = bool(
            previous
            and previous.state == "PENDING"
            and previous.scope_sha256 == _scope(conversation)
            and previous.input_from_seq == input_from_seq
        )
        first = utc(previous.created_at) if collecting else stamp
        wait = first + timedelta(seconds=self.limits.window_seconds)
        upload = first + timedelta(seconds=self.limits.upload_wait_seconds)
        turn = ChatAnalysisTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            customer_id=conversation.customer_id,
            actor_id=author,
            generation=previous.generation + 1 if previous else 1,
            revision=conversation.revision,
            input_from_seq=input_from_seq,
            input_through_seq=conversation.sequence,
            scope_sha256=_scope(conversation),
            attachment_sha256=digest(refs),
            attachments=refs,
            state="PENDING",
            quiet_until=min(stamp + timedelta(seconds=self.limits.quiet_seconds), wait),
            wait_until=wait,
            deadline=upload + timedelta(seconds=self.limits.execution_seconds),
            created_at=first,
        )
        if previous and previous.state in {"PENDING", "RUNNING"}:
            previous.state, previous.reason = "STALE", "INPUT_CHANGED"
        db.add(turn)
        return turn

    def invalidate(self, db, conversation):
        """Invalidate active work during a conversation-locked scope or handoff write."""
        turn = self._latest(db, conversation.id)
        if turn and turn.state in {"PENDING", "RUNNING"}:
            turn.state, turn.reason = "STALE", "CONTEXT_CHANGED"

    def _pending_upload(self, turn):
        issue_id = (
            select(Conversation.analysis_issue_id)
            .where(Conversation.id == turn.conversation_id)
            .correlate(ChatAnalysisTurn)
            .scalar_subquery()
        )
        return exists(
            select(Attachment.id)
            .join(Message, Message.id == Attachment.message_id)
            .where(
                Message.conversation_id == turn.conversation_id,
                Message.actor_id == turn.actor_id,
                Message.role == "customer",
                Message.analysis_issue_id.is_not_distinct_from(issue_id),
                Message.sequence.is_(None),
                Message.discarded_at.is_(None),
                Message.created_at >= turn.created_at,
            )
        )

    def due(self, limit=100):
        stamp = self.clock()
        upload_cutoff = stamp - timedelta(seconds=self.limits.upload_wait_seconds)
        turn = ChatAnalysisTurn
        with Session(self.engine) as db:
            return list(
                db.scalars(
                    select(turn.id)
                    .where(
                        or_(
                            (turn.state == "PENDING")
                            & (turn.quiet_until <= stamp)
                            & (~self._pending_upload(turn) | (turn.created_at <= upload_cutoff)),
                            (turn.state == "RUNNING") & (turn.lease_until <= stamp),
                        )
                    )
                    .order_by(turn.quiet_until, turn.id)
                    .limit(limit)
                )
            )

    def claim(self, turn_id):
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._locked(db, turn_id)
            stamp = self.clock()
            if turn.state == "RUNNING" and utc(turn.lease_until) > stamp:
                return None
            if turn.state not in {"PENDING", "RUNNING"}:
                return None
            attempt = self._attempt(db, turn.id, lock=True)
            if attempt:
                if attempt.state == "STARTED":
                    attempt.state, attempt.finished_at = "UNKNOWN", stamp
                turn.state, turn.reason = "FAILED", "ATTEMPT_NOT_REPLAYED"
                return None
            if not self._current(db, conversation, turn):
                turn.state, turn.reason = "STALE", "CONTEXT_CHANGED"
                return None
            if utc(turn.deadline) <= stamp:
                turn.state, turn.reason = "FAILED", "ANALYSIS_EXPIRED"
                return None
            if stamp < utc(turn.quiet_until) or (
                stamp < utc(turn.created_at) + timedelta(seconds=self.limits.upload_wait_seconds)
                and db.scalar(select(self._pending_upload(turn)))
            ):
                return None
            turn.state, turn.lease_token = "RUNNING", str(uuid4())
            turn.lease_until = min(
                stamp + timedelta(seconds=self.limits.lease_seconds), utc(turn.deadline)
            )
            return TurnLease(turn.id, turn.lease_token)

    def _active(self, db, turn_id, token):
        conversation, turn = self._locked(db, turn_id)
        stamp = self.clock()
        if (
            turn.state != "RUNNING"
            or turn.lease_token != token
            or utc(turn.lease_until) <= stamp
            or utc(turn.deadline) <= stamp
            or not self._current(db, conversation, turn)
        ):
            _fail("ANALYSIS_STALE")
        return conversation, turn

    def confirm(self, turn_id, token):
        with Session(self.engine) as db, db.begin():
            self._active(db, turn_id, token)

    def input_snapshot(self, lease):
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._active(db, lease.turn_id, lease.token)
            return {
                "turn_id": turn.id,
                "conversation_id": conversation.id,
                "actor_id": turn.actor_id,
                "customer_id": turn.customer_id,
                "generation": turn.generation,
                "revision": turn.revision,
                "input_from_seq": turn.input_from_seq,
                "input_through_seq": turn.input_through_seq,
                "scope_sha256": turn.scope_sha256,
                "attachment_sha256": turn.attachment_sha256,
                "attachments": turn.attachments,
            }

    def bind_vision_input(self, turn_id, token, payload):
        with Session(self.engine) as db, db.begin():
            _, turn = self._active(db, turn_id, token)
            refs = {ref["id"]: ref for ref in turn.attachments}
            photos = payload["photos"]
            if (
                not photos
                or len(photos) > 6
                or len({p["attachment_id"] for p in photos}) != len(photos)
            ):
                _fail("VISION_INPUT_UNBOUND")
            for photo in photos:
                ref = refs.get(photo["attachment_id"])
                if (
                    not ref
                    or ref["sha256"] != photo["original_sha256"]
                    or ref["message_id"] != photo["message_id"]
                    or not _hex(photo["sent_sha256"])
                ):
                    _fail("VISION_INPUT_UNBOUND")
            if turn.vision_input is not None and turn.vision_input != payload:
                _fail("VISION_INPUT_UNBOUND")
            turn.vision_input = payload

    def reserve(self, turn_id, token, reserve_microusd, model_evidence):
        if (
            type(reserve_microusd) is not int
            or not 0 <= reserve_microusd <= self.limits.max_microusd
        ):
            _fail("ANALYSIS_BUDGET_EXCEEDED")
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._active(db, turn_id, token)
            if not turn.vision_input or turn.vision_input["logical_id"] != model_evidence.get(
                "logical_id"
            ):
                _fail("VISION_INPUT_UNBOUND")
            if self._attempt(db, turn_id) is not None:
                _fail("ANALYSIS_ATTEMPT_EXISTS")
            attempts = list(
                db.scalars(
                    select(ChatModelAttempt).where(
                        ChatModelAttempt.conversation_id == conversation.id,
                    )
                )
            )
            if any(item.budget_violation for item in attempts):
                _fail("ANALYSIS_BUDGET_VIOLATION")
            charged = sum(
                max(item.reserved_microusd, item.estimated_cost_microusd or 0) for item in attempts
            )
            if (
                len(attempts) >= self.limits.max_calls
                or charged + reserve_microusd > self.limits.max_microusd
            ):
                _fail("ANALYSIS_BUDGET_EXCEEDED")
            stamp = self.clock()
            remaining = (utc(turn.lease_until) - stamp).total_seconds()
            if remaining <= 0:
                _fail("ANALYSIS_STALE")
            attempt = ChatModelAttempt(
                id=str(uuid4()),
                turn_id=turn_id,
                conversation_id=conversation.id,
                state="STARTED",
                reserved_microusd=reserve_microusd,
                model_evidence=model_evidence,
                started_at=stamp,
            )
            db.add(attempt)
            return attempt.id, remaining

    def finish_attempt(
        self,
        attempt_id,
        *,
        status,
        usage,
        estimated_cost_microusd,
        response_evidence,
        error_code=None,
        validated_result=None,
    ):
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("Invalid attempt status")
        if estimated_cost_microusd is not None and (
            type(estimated_cost_microusd) is not int or estimated_cost_microusd < 0
        ):
            raise ValueError("Invalid estimated cost")
        with Session(self.engine) as db, db.begin():
            turn_id = db.scalar(
                select(ChatModelAttempt.turn_id).where(ChatModelAttempt.id == attempt_id)
            )
            if turn_id is None:
                _fail("ANALYSIS_ATTEMPT_NOT_FOUND")
            self._locked(db, turn_id)
            attempt = db.get_one(ChatModelAttempt, attempt_id, with_for_update=True)
            if attempt.state not in {"STARTED", "UNKNOWN"}:
                return
            stamp = self.clock()
            evidence = dict(response_evidence or {})
            if status == "SUCCEEDED" and validated_result is not None:
                evidence["validated_result_sha256"] = digest(validated_result)
            if attempt.state == "UNKNOWN":
                original = attempt.response_evidence or {}
                if "late_settlement" in original:
                    return
                attempt.response_evidence = {
                    **original,
                    "late_settlement": {
                        "status": status,
                        "observed_at": stamp.isoformat(),
                        "error_code": error_code,
                        "usage": usage,
                        "estimated_cost_microusd": estimated_cost_microusd,
                        "response_evidence": evidence,
                    },
                }
            else:
                attempt.state, attempt.finished_at, attempt.error_code = status, stamp, error_code
                attempt.usage = usage
                attempt.estimated_cost_microusd = estimated_cost_microusd
                attempt.response_evidence = evidence
            if (
                estimated_cost_microusd is not None
                and estimated_cost_microusd > attempt.reserved_microusd
            ):
                attempt.budget_violation = True

    def publish(self, turn_id, token, observations, emit):
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._active(db, turn_id, token)
            attempt = self._attempt(db, turn_id)
            if not attempt or attempt.state != "SUCCEEDED":
                _fail("ANALYSIS_ATTEMPT_NOT_SETTLED")
            if attempt.budget_violation:
                _fail("ANALYSIS_BUDGET_VIOLATION")
            if (
                attempt.estimated_cost_microusd is None
                or not isinstance(attempt.usage, dict)
                or any(
                    type(attempt.usage.get(key)) is not int or attempt.usage[key] < 0
                    for key in ("input_tokens", "output_tokens")
                )
            ):
                _fail("ANALYSIS_COST_UNKNOWN")
            refs = {
                row["attachment_id"]: (index, row)
                for index, row in enumerate(turn.vision_input["photos"], 1)
            }
            if not observations or len(observations) != len(refs):
                _fail("ANALYSIS_OBSERVATIONS_INVALID")
            seen = set()
            projected = []
            normalized = []
            for row in observations:
                attachment_id = row["attachment_id"]
                index, ref = refs.get(attachment_id, (None, None))
                findings = row.get("findings")
                if (
                    attachment_id in seen
                    or not ref
                    or any(
                        ref[key] != row[key]
                        for key in (
                            "original_sha256",
                            "sent_sha256",
                            "transform_version",
                        )
                    )
                    or row.get("source_kind") != "MODEL_OBSERVATION"
                    or row.get("verification_state") != "UNVERIFIED"
                    or row.get("logical_id") != turn.vision_input["logical_id"]
                    or row.get("image_index", index) != index
                    or row.get("candidate_kind") not in _PHOTO_KINDS
                    or row.get("uncertainty") not in {"CLEAR", "LIMITED", "UNDETERMINED"}
                    or not isinstance(findings, list)
                    or len(findings) > 8
                    or any(value not in _FINDING_LABELS for value in findings)
                ):
                    _fail("ANALYSIS_OBSERVATIONS_INVALID")
                seen.add(attachment_id)
                normalized.append(
                    {
                        "image_index": index,
                        "candidate_kind": row["candidate_kind"],
                        "uncertainty": row["uncertainty"],
                        "findings": findings,
                    }
                )
                summary = (
                    "、".join(_FINDING_LABELS[value] for value in findings) or "暂未得到明确观察"
                )
                public = {
                    "attachment_id": attachment_id,
                    "certainty": "OBSERVED" if row["uncertainty"] == "CLEAR" else "UNCERTAIN",
                    "summary": summary,
                }
                projected.append(public)
                db.add(
                    Observation(
                        id=str(uuid4()),
                        turn_id=turn_id,
                        attempt_id=attempt.id,
                        original_sha256=ref["original_sha256"],
                        sent_sha256=ref["sent_sha256"],
                        transform_version=ref["transform_version"],
                        candidate_kind=row["candidate_kind"],
                        findings=findings,
                        created_at=self.clock(),
                        **public,
                    )
                )
            verified = {
                "result": {"photos": sorted(normalized, key=lambda row: row["image_index"])}
            }
            if (attempt.response_evidence or {}).get("validated_result_sha256") != digest(verified):
                _fail("ANALYSIS_RESULT_UNBOUND")
            result = emit(db, conversation, projected)
            turn.state, turn.reason = "PUBLISHED", None
            return result

    def publish_status(self, lease, emit):
        """Publish a deterministic status under the same fence, without a model attempt."""
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._active(db, lease.turn_id, lease.token)
            if self._attempt(db, turn.id) is not None:
                _fail("ANALYSIS_ATTEMPT_EXISTS")
            result = emit(db, conversation)
            turn.state, turn.reason = "PUBLISHED", None
            return result

    def fail_turn(self, lease, reason, emit=None):
        with Session(self.engine) as db, db.begin():
            conversation, turn = self._active(db, lease.turn_id, lease.token)
            attempt = self._attempt(db, turn.id, lock=True)
            if attempt is not None and attempt.state == "STARTED":
                attempt.state, attempt.finished_at = "UNKNOWN", self.clock()
            turn.state, turn.reason = "FAILED", reason
            if emit is not None:
                emit(db, conversation)

    def expire_stale_attempts(self, limit=100):
        stamp = self.clock()
        with Session(self.engine) as db:
            ids = list(
                db.scalars(
                    select(ChatAnalysisTurn.id)
                    .join(ChatModelAttempt)
                    .where(
                        ChatModelAttempt.state == "STARTED",
                        ChatAnalysisTurn.lease_until <= stamp,
                    )
                    .order_by(ChatAnalysisTurn.lease_until, ChatAnalysisTurn.id)
                    .limit(limit)
                )
            )
        count = 0
        for turn_id in ids:
            with Session(self.engine) as db, db.begin():
                _, turn = self._locked(db, turn_id)
                attempt = self._attempt(db, turn_id, lock=True)
                if attempt and attempt.state == "STARTED" and utc(turn.lease_until) <= stamp:
                    attempt.state, attempt.finished_at = "UNKNOWN", stamp
                    if turn.state == "RUNNING":
                        turn.state, turn.reason = "FAILED", "ATTEMPT_NOT_REPLAYED"
                    count += 1
        return count
