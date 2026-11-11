"""Input-first review for explicitly assigned, frozen test-profile chat cases."""

import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.refund_lookup import locate_refund
from aftercare.business.rules import digest, now, utc
from aftercare.business.storage import verify_object
from aftercare.conversations.service import ConversationService
from aftercare.conversations.tables import Attachment, Message
from aftercare.db import User
from aftercare.decisions.contracts import Candidate, DecisionScope
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.investigation.catalogue import CATALOGUE_REF
from aftercare.investigation.contracts import ORDER_OWNERSHIP_REF
from aftercare.investigation.input import build_input

from ..service import _mutate, _remember
from .contracts import BlindCandidate, BlindInput
from .tables import BlindExposure, BlindInitial, BlindLabels, BlindTask

_STATES = {1: "BLIND", 2: "SEALED", 3: "REVEALED", 4: "LABELED"}
_ACTIONS = {"initial": (1, BlindInitial), "reveal": (2, BlindExposure), "labels": (3, BlindLabels)}
_TEST_CASE_LABEL = re.compile(r"[A-Z0-9][A-Z0-9-]{0,79}")
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_FACT_FIELDS = {
    "ORDER_LINE": (
        "product_id",
        "sku",
        "title",
        "quantity",
        "paid_cents",
        "refunded_cents",
        "shipment_id",
        "package_id",
        "available_cents",
        "replace_available_quantity",
        "order_version",
        "delivery_status",
    ),
    "MATERIAL": ("id", "kind", "sha256", "content_type", "size", "created_at"),
    "OBSERVATION": (
        "attachment_id",
        "original_sha256",
        "sent_sha256",
        "transform_version",
        "candidate_kind",
        "certainty",
        "findings",
        "summary",
    ),
}
_CONDITION_IDS = {
    "order_status",
    "payment_status",
    "paid_at",
    "shipped_at",
    "delivered_at",
    "cancelled_at",
    "case:fulfillment_status",
    "case:fulfillment_order_version",
    "case:interception_status",
}
_POLICY_CONDITIONS = (
    "intents",
    "product_ids",
    "categories",
    "time_basis",
    "priority",
    "effective_at",
    "expires_at",
    "published_at",
    "withdrawn_at",
    "supersedes",
    "status",
)


def _fail(code, status=409):
    messages = {
        404: "未找到可复核的任务。",
        409: "复核状态已变化，请刷新后继续。",
        422: "请核对所填的订单行、事实、条款或候选。",
    }
    raise BusinessError(code, messages[status], status)


def _summary(row, version=None):
    version = row.version if version is None else version
    return {
        "id": row.id,
        "case_label": row.case_label,
        "input_sha256": row.input_sha256,
        "status": _STATES[version],
        "version": version,
    }


def _record(row):
    return {
        "sha256": row.sha256,
        "created_at": utc(row.created_at).isoformat(),
        "payload": row.payload,
    }


def _detail(db, task, *, version=None):
    result = _summary(task, version)
    version = result["version"]
    result.update(input=task.input, initial=None, exposure=None, labels=None)
    if version >= 2:
        result["initial"] = _record(db.get_one(BlindInitial, task.id))
    if version >= 3:
        exposure = db.get_one(BlindExposure, task.id)
        if exposure.candidates_sha256 != digest(task.candidates):
            _fail("BLIND_REVIEW_CANDIDATE_BINDING_INVALID", 422)
        result["exposure"] = {
            "sha256": exposure.sha256,
            "created_at": utc(exposure.created_at).isoformat(),
            "candidates": task.candidates,
        }
    if version >= 4:
        result["labels"] = _record(db.get_one(BlindLabels, task.id))
    return result


def _selected(row, keys):
    return {key: row[key] for key in keys if key in row}


def _fact_data(kind, ident, data, line_ids):
    if kind == "EXECUTION":
        return locate_refund([data], ident)["execution"]
    if kind == "CONDITION":
        if ident == ORDER_OWNERSHIP_REF:
            return _selected(data, ("value", "identity_scope", "version", "order_id"))
        if ident == "case:return_lines":
            return {
                "value": {
                    line_id: _selected(facts, ("return_receipt_status", "inspection_status"))
                    for line_id, facts in data["value"].items()
                    if line_id in line_ids
                }
            }
        if ident in _CONDITION_IDS:
            return _selected(data, ("value", "order_version"))
        return None
    if kind not in _FACT_FIELDS:
        return None
    result = _selected(data, _FACT_FIELDS[kind])
    if kind == "ORDER_LINE":
        result["line_id"] = ident
    return result


def _frozen_public(scope):
    data = scope.frozen_context
    evidence_projection = (data or {}).get("evidence_projection")
    value = build_input(scope, evidence_projection=evidence_projection)
    facts = []
    for row in value.evidence:
        kind, ident = row.ref.source_kind, row.ref.source_id
        selected = _fact_data(kind, ident, row.data, scope.line_ids)
        if selected is not None:
            facts.append(
                {"id": f"{kind}:{ident}", "kind": kind, "trust": row.trust, "data": selected}
            )
    observations = {
        row.ref.source_id: row.data["observations"]
        for row in value.evidence
        if row.ref.source_kind == "REVIEW" and "observations" in row.data
    }
    for review in data["reviews"]:
        review_facts = _selected(
            review, ("decision", "material_ids", "materials_version", "created_at", "effective")
        )
        if review["id"] in observations:
            review_facts["observations"] = observations[review["id"]]
        facts.append(
            {
                "id": f"REVIEW:{review['id']}",
                "kind": "REVIEW",
                "trust": "VERIFIED_RECORD",
                "data": review_facts,
            }
        )
    return BlindInput.model_validate(
        {
            **({"schema_version": "chat-blind-input-v2"} if evidence_projection else {}),
            "intent": value.intent,
            "messages": data["messages"],
            "images": [
                {
                    "id": image["id"],
                    "message_id": image["message_id"],
                    "sha256": image["sha256"],
                    "size": image["size"],
                    "content_type": image["content_type"],
                }
                for image in data["attachments"]
            ],
            "facts": facts,
            "policy_coverage": (
                "COMPLETE_CATALOGUE" if value.policy_catalogue_version else "SCOPED_ONLY"
            ),
            "policy_context": value.policy_context or None,
            "policies": [
                {
                    "id": row.data["clause"]["id"],
                    "version": row.data["clause"]["version"],
                    "title": row.data["clause"].get("title", ""),
                    "text": row.data["clause"]["text"],
                    "parameters": row.data["clause"].get("parameters", {}),
                    "conditions": _selected(row.data["clause"], _POLICY_CONDITIONS),
                }
                for row in value.evidence
                if row.ref.source_kind == "POLICY"
            ],
        }
    )


def _image_objects(db, scope, public):
    result = {}
    for image in public.images:
        row = db.execute(
            select(Attachment, Message)
            .join(Message, Message.id == Attachment.message_id)
            .where(Attachment.id == image.id)
        ).one_or_none()
        if row is None:
            _fail("BLIND_REVIEW_SOURCE_UNAVAILABLE", 422)
        attachment, message = row
        expected = ConversationService._object_key(
            scope.conversation_id, image.message_id, attachment.position, image.sha256
        )
        if (
            message.conversation_id != scope.conversation_id
            or message.analysis_issue_id != scope.issue_id
            or message.sequence is None
            or message.sequence > scope.input_through_seq
            or message.role != "customer"
            or message.discarded_at is not None
            or (attachment.message_id, attachment.sha256, attachment.size, attachment.content_type)
            != (image.message_id, image.sha256, image.size, image.content_type)
            or attachment.object_key != expected
        ):
            _fail("BLIND_REVIEW_SOURCE_UNAVAILABLE", 422)
        result[image.id] = expected
    return result


def _references(ids, catalogue):
    return [
        {
            "id": ident,
            "status": "RESOLVED" if ident in catalogue else "UNRESOLVED",
            "resolved_ids": catalogue.get(ident, []),
        }
        for ident in ids
    ]


def _candidate(snapshot, scope, public):
    if (
        snapshot is None
        or snapshot.origin != "AGENT"
        or not snapshot.run_id
        or snapshot.conversation_id != scope.conversation_id
        or snapshot.issue_id != scope.issue_id
        or snapshot.input != scope.model_dump(mode="json")
    ):
        _fail("BLIND_REVIEW_CANDIDATE_BINDING_INVALID", 422)
    candidate = Candidate.model_validate(snapshot.candidate)
    if candidate.run_id != snapshot.run_id:
        _fail("BLIND_REVIEW_CANDIDATE_BINDING_INVALID", 422)
    evidence = {item.id.split(":", 1)[1]: [item.id] for item in public.facts}
    evidence.update({item.id: [item.id] for item in [*public.messages, *public.images]})
    policies = {}
    for policy in public.policies:
        versioned = f"{policy.id}:{policy.version}"
        policies.setdefault(policy.id, []).append(versioned)
        policies[versioned] = [versioned]
        evidence[versioned] = [versioned]
    evidence["effective-policy-snapshot"] = sorted(
        f"{policy['id']}:{policy['version']}"
        for policy in scope.frozen_context["policies"]["documents"]
    )
    if public.policy_coverage == "COMPLETE_CATALOGUE":
        evidence[CATALOGUE_REF] = sorted(
            f"{policy.id}:{policy.version}" for policy in public.policies
        )
    single_policy_version = {
        ident: versions for ident, versions in policies.items() if len(versions) == 1
    }
    options = []
    for option in candidate.options:
        item = option.model_dump(mode="json", exclude_none=True)
        options.append(
            {
                "id": digest([snapshot.id, item["id"]])[:32],
                "action": item["action"],
                "lines": item.get("lines")
                or [{"line_id": ident} for ident in item["target_line_ids"]],
                "required_evidence": item.get("missing_evidence", []),
                "evidence_ids": item["evidence_ids"],
                "policy_ids": item.get("policy_ids", []),
                "rationale": item["rationale"],
                "alternatives": item.get("alternatives", []),
                "evidence_references": _references(item["evidence_ids"], evidence),
                "policy_references": _references(item.get("policy_ids", []), single_policy_version),
            }
        )
    return BlindCandidate.model_validate(
        {
            "id": _blind_candidate_id(scope, snapshot),
            "status": "AVAILABLE",
            "options": options,
            "unresolved": list(candidate.unresolved),
        }
    ).model_dump(mode="json", exclude_none=True)


def _blind_candidate_id(scope, snapshot):
    return digest([scope.input_sha256, snapshot.id])[:32]


class BlindReviewService:
    def __init__(self, business):
        self.business = business
        self.engine = business.engine

    def _profile(self):
        if self.business.settings.profile != "test":
            _fail("BLIND_REVIEW_NOT_FOUND", 404)

    def _reviewer(self, db, reviewer_id, *, exclusive=False):
        self._profile()
        reviewer = db.get(User, reviewer_id, with_for_update=True if exclusive else {"read": True})
        if reviewer is None or not reviewer.enabled or reviewer.role != "tester":
            _fail("BLIND_REVIEW_NOT_FOUND", 404)
        return reviewer

    def _actor(self, db, actor):
        if actor.role != "tester":
            _fail("BLIND_REVIEW_NOT_FOUND", 404)
        return self._reviewer(db, actor.user_id)

    def _task(self, db, actor, task_id, *, lock=False):
        self._actor(db, actor)
        stmt = select(BlindTask).where(
            BlindTask.id == task_id, BlindTask.reviewer_id == actor.user_id
        )
        row = db.scalar(stmt.with_for_update() if lock else stmt)
        if row is None:
            _fail("BLIND_REVIEW_NOT_FOUND", 404)
        return row

    def assign(self, *, reviewer_id, case_label, input_snapshot_id, candidate_snapshot_ids):
        """Trusted local test runner entry; deliberately absent from HTTP."""
        if not _TEST_CASE_LABEL.fullmatch(case_label) or not (
            1 <= len(candidate_snapshot_ids) <= 12
            and len(set(candidate_snapshot_ids)) == len(candidate_snapshot_ids)
        ):
            _fail("BLIND_REVIEW_ASSIGNMENT_INVALID", 422)
        with Session(self.engine) as db, db.begin():
            self._reviewer(db, reviewer_id, exclusive=True)
            source = db.get(DecisionSnapshot, input_snapshot_id)
            if source is None or source.origin != "AGENT" or source.actor_id == reviewer_id:
                _fail("BLIND_REVIEW_SOURCE_UNAVAILABLE", 422)
            try:
                scope = DecisionScope.model_validate(source.input)
                public = _frozen_public(scope)
                if sum(image.size for image in public.images) > _MAX_IMAGE_BYTES:
                    _fail("BLIND_REVIEW_SOURCE_TOO_LARGE", 422)
                source_sha = digest([scope.input_sha256, source.id])
                images = _image_objects(db, scope, public)
                snapshots = tuple(
                    db.get(DecisionSnapshot, ident) for ident in candidate_snapshot_ids
                )
                return self._persist_assignment(
                    db,
                    reviewer_id=reviewer_id,
                    case_label=case_label,
                    scope=scope,
                    public=public,
                    snapshots=snapshots,
                    images=images,
                    source_sha=source_sha,
                )
            except ValueError:
                _fail("BLIND_REVIEW_SOURCE_UNAVAILABLE", 422)

    def _persist_assignment(
        self, db, *, reviewer_id, case_label, scope, public, snapshots, images, source_sha
    ):
        """Caller has locked reviewer and verified native snapshot and image provenance."""
        if not 1 <= len(snapshots) <= 12 or set(images) != {image.id for image in public.images}:
            _fail("BLIND_REVIEW_ASSIGNMENT_INVALID", 422)
        if sum(image.size for image in public.images) > _MAX_IMAGE_BYTES:
            _fail("BLIND_REVIEW_SOURCE_TOO_LARGE", 422)
        frozen = public.model_dump(mode="json")
        input_sha = digest(frozen)
        candidates = sorted(
            (_candidate(snapshot, scope, public) for snapshot in snapshots),
            key=lambda item: item["id"],
        )
        existing = db.scalar(
            select(BlindTask).where(
                BlindTask.reviewer_id == reviewer_id,
                BlindTask.input_sha256 == input_sha,
            )
        )
        if existing is not None:
            if (existing.source_sha256, existing.case_label, existing.candidates) != (
                source_sha,
                case_label,
                candidates,
            ):
                _fail("BLIND_REVIEW_ASSIGNMENT_EXISTS")
            return _summary(existing)
        for image in public.images:
            original = self.business.storage.get(images[image.id], image.size)
            verify_object(original, image.size, image.sha256)
        row = BlindTask(
            id=str(uuid4()),
            reviewer_id=reviewer_id,
            case_label=case_label,
            input_sha256=input_sha,
            source_sha256=source_sha,
            input=frozen,
            image_objects=images,
            candidates=candidates,
            version=1,
            created_at=now(),
        )
        db.add(row)
        return _summary(row)

    def list(self, actor, *, after=None, limit=25):
        with Session(self.engine) as db, db.begin():
            self._actor(db, actor)
            stmt = select(BlindTask).where(BlindTask.reviewer_id == actor.user_id)
            if after is not None:
                stmt = stmt.where(BlindTask.id > after)
            rows = list(db.scalars(stmt.order_by(BlindTask.id).limit(limit + 1)))
            return {
                "items": [_summary(row) for row in rows[:limit]],
                "next_after": rows[limit - 1].id if len(rows) > limit else None,
            }

    def get(self, actor, task_id):
        with Session(self.engine) as db, db.begin():
            return _detail(db, self._task(db, actor, task_id))

    def image(self, actor, task_id, image_id):
        with Session(self.engine) as db, db.begin():
            row = self._task(db, actor, task_id)
            image = next((item for item in row.input["images"] if item["id"] == image_id), None)
            key = row.image_objects.get(image_id)
            if not image or not key:
                _fail("BLIND_REVIEW_NOT_FOUND", 404)
            content = self.business.storage.get(key, image["size"])
            verify_object(content, image["size"], image["sha256"])
            return content, image["content_type"]

    def write(self, actor, task_id, operation, body, key):
        before, table = _ACTIONS[operation]
        request = body.model_dump(mode="json", exclude={"expected_version"})
        scope = f"chat-blind:{operation}:{task_id}"
        with Session(self.engine) as db, db.begin():
            task = self._task(db, actor, task_id, lock=True)
            replay, checksum = _mutate(db, actor, scope, key, [body.expected_version, request])
            if replay is not None:
                return _detail(db, task, version=before + 1)
            if task.version != body.expected_version or task.version != before:
                _fail("BLIND_REVIEW_STAGE_CHANGED")
            initial = db.get(BlindInitial, task.id)
            exposure = db.get(BlindExposure, task.id)
            if operation == "initial":
                self._check_initial(task.input, body)
                values = {"payload": request, "sha256": digest([task.input_sha256, request])}
            elif operation == "reveal":
                if initial is None or not task.candidates:
                    _fail("BLIND_REVIEW_CANDIDATES_UNAVAILABLE")
                bundle = digest(task.candidates)
                values = {
                    "initial_sha256": initial.sha256,
                    "candidates_sha256": bundle,
                    "sha256": digest([task.id, initial.sha256, bundle]),
                }
            else:
                if exposure is None:
                    _fail("BLIND_REVIEW_EXPOSURE_REQUIRED")
                self._check_labels(task.candidates, body)
                values = {
                    "payload": request,
                    "exposure_sha256": exposure.sha256,
                    "sha256": digest([exposure.sha256, request]),
                }
            db.add(table(task_id=task.id, created_at=now(), **values))
            task.version += 1
            _remember(db, actor, scope, key, checksum, task.id)
            db.flush()
            return _detail(db, task)

    @staticmethod
    def _check_initial(frozen, body):
        lines = {
            item["data"]["line_id"] for item in frozen["facts"] if item["kind"] == "ORDER_LINE"
        }
        facts = {item["id"] for item in frozen["facts"]}
        policies = {(item["id"], item["version"]) for item in frozen["policies"]}
        if (
            any(line.line_id not in lines for plan in body.acceptable_plans for line in plan.lines)
            or not set(body.required_fact_ids) <= facts
            or any((row.policy_id, row.version) not in policies for row in body.policy_assessments)
        ):
            _fail("BLIND_REVIEW_REFERENCE_OUT_OF_SCOPE", 422)

    @staticmethod
    def _check_labels(candidates, body):
        expected = {row["id"]: row for row in candidates}
        if (
            len(body.items) != len(expected)
            or {row.candidate_id for row in body.items} != expected.keys()
        ):
            _fail("BLIND_REVIEW_LABEL_COVERAGE_REQUIRED", 422)
        if any(
            expected[row.candidate_id]["status"] == "UNAVAILABLE" and row.decision != "UNDETERMINED"
            for row in body.items
        ):
            _fail("BLIND_REVIEW_CANDIDATE_UNAVAILABLE", 422)
