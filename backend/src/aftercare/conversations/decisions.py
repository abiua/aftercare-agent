"""Bind conversation decisions to the same transactions as business authority."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Case, Order
from aftercare.business.rules import digest, utc
from aftercare.decisions.contracts import DecisionRef, DecisionScope
from aftercare.decisions.service import DecisionService
from aftercare.decisions.tables import (
    DecisionHead,
    DecisionSnapshot,
    Offer,
    SupportDecision,
)
from aftercare.investigation.catalogue import (
    CATALOGUE_REF,
    catalogue_capsule,
    exact_policy_refs_match,
)
from aftercare.investigation.contracts import ORDER_OWNERSHIP_REF, account_order_ownership
from aftercare.investigation.material_observations import (
    HUMAN_MATERIAL_PROJECTION_V1,
    validate_review_observations,
)

from .tables import Attachment, ChatMaterialSource, Conversation, Message

MAX_DECISION_INPUT_REFS = 250
ACKNOWLEDGEMENT = re.compile(
    r"^(?:好(?:的|了)?|收到|知道了|明白了?|了解|谢谢(?:你|您)?|多谢|辛苦了|你好|您好|嗯|ok)[\s。！!，,]*$",
    re.IGNORECASE,
)
EVIDENCE_GAPS = {
    "ITEM": "商品照片",
    "PARCEL": "包裹外观照片",
    "WRONG_ITEM": "收到商品的照片",
    "LABEL": "包裹外侧标签照片",
    "ISSUE_DESCRIPTION": "问题发生的位置或具体表现",
}


def enabled(settings):
    return getattr(settings, "chat_decisions_enabled", False)


def _fail(code, status=409):
    raise BusinessError(code, "当前会话的售后决定无法继续。", status)


def enroll(case, conversation):
    """Caller owns conversation and order locks, including for an existing case."""
    if case.decision_conversation_id is None:
        case.decision_conversation_id = conversation.id
        case.decision_issue_id = conversation.analysis_issue_id
        case.decision_legacy_through_version = case.version
    elif (
        case.decision_conversation_id != conversation.id
        or case.decision_issue_id != conversation.analysis_issue_id
    ):
        _fail("CASE_DECISION_ALREADY_MANAGED")


def lock_case(business, db, case_id, actor):
    binding = db.execute(
        select(Case.decision_conversation_id, Case.decision_issue_id).where(Case.id == case_id)
    ).one_or_none()
    if binding is None:
        return (*business._case(db, case_id, actor, lock=True), None)
    conversation = None
    if binding[0]:
        conversation = db.get(Conversation, binding[0], with_for_update=True)
        if conversation is None:
            _fail("DECISION_BINDING_INVALID")
    case, order = business._case(db, case_id, actor, lock=True)
    if tuple(binding) != (case.decision_conversation_id, case.decision_issue_id):
        _fail("DECISION_ENROLLMENT_CHANGED")
    return case, order, conversation


def _relevant(text):
    value = text.strip()
    return bool(value and not ACKNOWLEDGEMENT.fullmatch(value))


def _ref(kind, ident, value):
    return {"kind": kind, "id": ident, "sha256": digest(value)}


def _freeze_human_reviews(db, row, case, context, attachments, messages):
    reviews = []
    materials = {item["id"]: item for item in context["materials"]}
    original = {
        item.id: {
            "message_id": item.message_id,
            "sha256": item.sha256,
            "size": item.size,
            "content_type": item.content_type,
        }
        for item in attachments
    }
    active_messages = {item.id for item in messages if item.discarded_at is None}
    quantities = {
        item["line_id"]: item["quantity"]
        for item in context["order"]["items"]
        if item["line_id"] in case.line_ids
    }
    for item in context["reviews"]:
        review = dict(item)
        observations = review.pop("observations", None)
        if observations is not None and review["effective"]:
            try:
                ids = {entry["material_id"] for entry in observations}
                sources = {ident: [] for ident in ids}
                for source in db.scalars(
                    select(ChatMaterialSource)
                    .where(ChatMaterialSource.material_id.in_(ids))
                    .order_by(ChatMaterialSource.attachment_id)
                ):
                    attachment = original.get(source.attachment_id)
                    material = materials[source.material_id]
                    if (
                        (source.case_id, source.conversation_id, source.issue_id)
                        != (case.id, row.id, row.analysis_issue_id)
                        or attachment is None
                        or source.message_id not in active_messages
                        or (source.message_id, source.sha256)
                        != (attachment["message_id"], attachment["sha256"])
                        or (source.kind, source.sha256) != (material["kind"], material["sha256"])
                    ):
                        raise ValueError("INVALID_HUMAN_MATERIAL_SOURCE")
                    sources[source.material_id].append(
                        {
                            "attachment_id": source.attachment_id,
                            "message_id": source.message_id,
                            "sha256": source.sha256,
                        }
                    )
                review["observations"] = [
                    {
                        **entry,
                        "material_sha256": materials[entry["material_id"]]["sha256"],
                        "original_attachments": sources[entry["material_id"]],
                    }
                    for entry in observations
                ]
                review["observations"] = validate_review_observations(
                    review,
                    materials,
                    original,
                    quantities,
                    case.materials_version,
                )
            except (KeyError, TypeError, ValueError):
                _fail("DECISION_HUMAN_MATERIAL_OBSERVATIONS_INVALID", 422)
        reviews.append(review)
    return reviews


class ConversationDecisions:
    def __init__(self, business, *, evidence_projection=None):
        if evidence_projection not in (None, HUMAN_MATERIAL_PROJECTION_V1):
            raise ValueError("DECISION_EVIDENCE_PROJECTION_UNSUPPORTED")
        self.evidence_projection = evidence_projection
        self.business = business
        self.engine = business.engine
        self.records = DecisionService(self.resolve, self.validate, clock=business.clock)

    def _conversation(self, db, actor, ident):
        query = select(Conversation).where(Conversation.id == ident)
        if actor.role == "customer":
            query = query.where(Conversation.customer_id == actor.customer_id)
        elif actor.role not in {"support", "approver"}:
            _fail("DECISION_FORBIDDEN", 403)
        row = db.scalar(query.with_for_update())
        if row is None:
            _fail("DECISION_NOT_FOUND", 404)
        return row

    def resolve(self, db, actor, conversation_id, issue_id):
        row = self._conversation(db, actor, conversation_id)
        if row.analysis_issue_id != issue_id or row.active_case_id is None:
            _fail("DECISION_STALE")
        case, order = self.business._case(db, row.active_case_id, actor, lock=True)
        if (
            (case.decision_conversation_id, case.decision_issue_id) != (row.id, issue_id)
            or case.customer_id != row.customer_id
            or order.id != row.active_order_id
            or sorted(case.line_ids) != sorted(row.line_ids)
        ):
            _fail("DECISION_BINDING_INVALID")
        return self.freeze(db, row, case, order)

    def freeze(self, db, row, case, order):
        from aftercare.conversation_analysis.tables import ChatAnalysisTurn, Observation

        raw_catalogue = None
        review_options = {"include_review_observations": True} if self.evidence_projection else {}
        if getattr(self.business.settings, "chat_investigation_enabled", False):
            decision_at = self.business.clock()
            documents = self.business._all_policy_versions(db)
            context = self.business._context(
                db,
                case,
                order,
                policy_documents=documents,
                decision_at=decision_at,
                **review_options,
            )
            ids = {
                item["product_id"]
                for item in context["order"]["items"]
                if item["line_id"] in case.line_ids
            }
            by_id = {item["id"]: item for item in context["order"]["products"]}
            products = [
                {"id": key, "category": by_id.get(key, {}).get("category")} for key in sorted(ids)
            ]
            try:
                raw_catalogue = catalogue_capsule(documents, decision_at, products)
            except ValueError as error:
                _fail(str(error), 422)
        else:
            context = self.business._context(db, case, order, **review_options)
        messages = list(
            db.scalars(
                select(Message)
                .where(
                    Message.conversation_id == row.id,
                    Message.analysis_issue_id == row.analysis_issue_id,
                    Message.role == "customer",
                    Message.sequence.is_not(None),
                )
                .order_by(Message.sequence)
                .limit(MAX_DECISION_INPUT_REFS + 1)
            )
        )
        if not messages:
            _fail("DECISION_INPUT_REQUIRED")
        ownership = None
        if (
            raw_catalogue is not None
            and row.customer_id == case.customer_id == order.customer_id
            and all(
                item.actor_id
                and item.client_message_id
                and item.request_hash
                and item.discarded_at is None
                for item in messages
            )
        ):
            ownership = account_order_ownership(order.id)
        ids = [item.id for item in messages]
        attachments = list(
            db.scalars(
                select(Attachment)
                .where(Attachment.message_id.in_(ids))
                .order_by(Attachment.message_id, Attachment.position)
                .limit(101)
            )
        )
        observation_rows = list(
            db.scalars(
                select(Observation)
                .join(ChatAnalysisTurn, ChatAnalysisTurn.id == Observation.turn_id)
                .where(
                    ChatAnalysisTurn.conversation_id == row.id,
                    ChatAnalysisTurn.state == "PUBLISHED",
                    Observation.attachment_id.in_([item.id for item in attachments]),
                )
                .order_by(Observation.id)
                .limit(101)
            )
        )
        reviews = (
            _freeze_human_reviews(db, row, case, context, attachments, messages)
            if self.evidence_projection
            else context["reviews"]
        )
        message_refs = [_ref("MESSAGE", item.id, [item.sequence, item.text]) for item in messages]
        evidence = [
            *[
                _ref(
                    "ATTACHMENT",
                    item.id,
                    [item.message_id, item.sha256, item.size, item.content_type],
                )
                for item in attachments
            ],
            *[
                _ref(
                    "OBSERVATION",
                    item.id,
                    [
                        item.attachment_id,
                        item.original_sha256,
                        item.sent_sha256,
                        item.certainty,
                        item.findings,
                    ],
                )
                for item in observation_rows
            ],
            *[_ref("FACT", item["id"], item) for item in context["materials"]],
            *[_ref("FACT", item["id"], item) for item in reviews if item["effective"]],
        ]
        if case.facts:
            evidence.append(_ref("FACT", "case-facts", case.facts))
        if ownership is not None:
            evidence.append(_ref("FACT", ORDER_OWNERSHIP_REF, ownership))
        policy_ref = _ref("POLICY", "effective-policy-snapshot", context["policy_resolution"])
        evidence.append(policy_ref)
        dependencies = list(evidence)
        if raw_catalogue is not None:
            evidence.append(_ref("POLICY", CATALOGUE_REF, raw_catalogue))
            balances = {item["line_id"]: item for item in context["eligibility"]["lines"]}
            evidence.extend(
                _ref("FACT", item["line_id"], [item, balances[item["line_id"]]])
                for item in context["order"]["items"]
                if item["line_id"] in case.line_ids
            )
            evidence.extend(
                _ref("POLICY", f"{item['id']}:{item['version']}", item)
                for item in raw_catalogue["documents"]
            )
        refs = [*message_refs, *evidence]
        if (
            len(refs) > MAX_DECISION_INPUT_REFS
            or len(evidence) > 100
            or len({item["id"] for item in evidence}) != len(evidence)
            or any(len(item["id"]) > 80 for item in evidence)
        ):
            _fail("DECISION_INPUT_TOO_LARGE", 422)
        versions = dict(context["versions"])
        versions["ledger"] = digest(
            [
                [item.id, item.action, item.status, item.amount_cents, item.lines, item.receipt]
                for item in self.business._ledger(db, order)
            ]
        )
        versions["balances"] = digest(context["eligibility"]["lines"])
        versions["facts_digest"] = digest(case.facts)
        related = [
            ref for item, ref in zip(messages, message_refs, strict=True) if _relevant(item.text)
        ]
        order_fields = (
            "id",
            "status",
            "payment_status",
            "created_at",
            "paid_at",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
            "paid_cents",
            "refunded_cents",
            "version",
        )
        agent_message = next(
            (item.text.strip() for item in reversed(messages) if _relevant(item.text)),
            case.description,
        )
        frozen = {
            "legacy_agent_input": {
                "message": agent_message,
                "business_source": "BusinessService.context",
                "archived_chat_attachments_sent": False,
                "archived_observations_sent": False,
            },
            "messages": [
                {"id": item.id, "sequence": item.sequence, "text": item.text} for item in messages
            ],
            "attachments": [
                {
                    "id": item.id,
                    "message_id": item.message_id,
                    "sha256": item.sha256,
                    "content_type": item.content_type,
                    "size": item.size,
                }
                for item in attachments
            ],
            "observations": [
                {
                    "id": item.id,
                    "attachment_id": item.attachment_id,
                    "turn_id": item.turn_id,
                    "attempt_id": item.attempt_id,
                    "original_sha256": item.original_sha256,
                    "sent_sha256": item.sent_sha256,
                    "transform_version": item.transform_version,
                    "candidate_kind": item.candidate_kind,
                    "certainty": item.certainty,
                    "findings": item.findings,
                    "summary": item.summary,
                }
                for item in observation_rows
            ],
            "case": context["case"],
            "order": {
                **{key: context["order"][key] for key in order_fields if key in context["order"]},
                "items": [
                    item for item in context["order"]["items"] if item["line_id"] in case.line_ids
                ],
            },
            "facts": case.facts,
            **({"authenticated_order_ownership": ownership} if ownership is not None else {}),
            "materials": context["materials"],
            "reviews": reviews,
            **(
                {"evidence_projection": self.evidence_projection}
                if self.evidence_projection
                else {}
            ),
            "policies": context["policy_resolution"],
            **({"policy_catalogue": raw_catalogue} if raw_catalogue is not None else {}),
            "eligibility": context["eligibility"],
            "ledger": context["ledger"],
        }
        return DecisionScope(
            conversation_id=row.id,
            issue_id=row.analysis_issue_id,
            case_id=case.id,
            order_id=order.id,
            customer_id=row.customer_id,
            line_ids=tuple(sorted(case.line_ids)),
            input_through_seq=row.sequence,
            input_sha256=digest([refs, frozen]),
            input_refs=tuple(refs),
            evidence_graph_sha256=digest(evidence),
            evidence_ids=tuple(item["id"] for item in evidence),
            policy_catalogue_sha256=versions["policies"],
            dependency_sha256=digest(
                [
                    {key: value for key, value in versions.items() if key != "case"},
                    related,
                    dependencies,
                    *([raw_catalogue["documents_sha256"]] if raw_catalogue is not None else []),
                ]
            ),
            business_versions=versions,
            frozen_context=frozen,
        )

    def validate(self, db, scope, option):
        catalogue = scope.frozen_context.get("policy_catalogue")
        versioned = catalogue and set(option.evidence_ids).intersection(
            f"{doc['id']}:{doc['version']}" for doc in catalogue["documents"]
        )
        if versioned and not exact_policy_refs_match(scope.frozen_context, option):
            _fail("INVESTIGATION_POLICY_VERSION_NOT_APPLICABLE")
        case = db.get_one(Case, scope.case_id)
        order = db.get_one(Order, scope.order_id)
        lines = self.business._normalise_lines(option.action, option.business_lines())
        self.business._editable(case)
        self.business._reject_duplicate_matter(db, case, order, lines)
        return self.business._validate_plan(
            db,
            case,
            order,
            option.action,
            lines,
            policy_ids=list(option.policy_ids),
            expected_policy_snapshot=scope.policy_catalogue_sha256 if versioned else None,
        )

    def _current(self, db, actor, ident, role):
        if not enabled(self.business.settings) or actor.role != role:
            _fail("DECISION_NOT_FOUND", 404)
        row = self._conversation(db, actor, ident)
        return row, row.analysis_issue_id

    @staticmethod
    def _snapshot_ref(db, head):
        snapshot = db.get(DecisionSnapshot, head.snapshot_id) if head and head.snapshot_id else None
        return (
            DecisionRef(id=snapshot.id, version=snapshot.version, sha256=snapshot.sha256)
            if snapshot
            else None
        )

    def current(self, actor, ident):
        with Session(self.engine) as db, db.begin():
            row, issue_id = self._current(db, actor, ident, "support")
            head = db.get(DecisionHead, (row.id, issue_id)) if issue_id else None
            reference = self._snapshot_ref(db, head)
            snapshot = (
                self.records.snapshot(db, actor, row.id, issue_id, reference.id)
                if reference
                else None
            )
            review = db.get(SupportDecision, head.review_id) if head and head.review_id else None
            offer = db.get(Offer, head.offer_id) if head and head.offer_id else None
            response = self.records.latest_customer_response(db, offer.id) if offer else None
            return {
                "issue_id": issue_id,
                "snapshot": snapshot,
                "expected_review_id": review.id if review else None,
                "latest_review": {
                    "id": review.id,
                    "action": review.review["action"],
                    "reason": review.review["reason"],
                    "program_check": review.review["program_check"],
                    "evidence_gaps": review.review.get("evidence_gaps", []),
                    "created_at": utc(review.created_at).isoformat(),
                }
                if review
                else None,
                "customer_decision": {
                    "id": response.id,
                    "action": response.response["action"],
                    "reason": response.response.get("reason"),
                    "created_at": utc(response.created_at).isoformat(),
                    "offer": {"id": offer.id, "version": offer.version, "sha256": offer.sha256},
                }
                if response
                else None,
            }

    def history(self, actor, ident, snapshot_id):
        with Session(self.engine) as db, db.begin():
            row, _ = self._current(db, actor, ident, "support")
            snapshot = db.get(DecisionSnapshot, snapshot_id)
            if snapshot is None or snapshot.conversation_id != row.id:
                _fail("DECISION_NOT_FOUND", 404)
            return self.records.snapshot_payload(snapshot)

    def run(self, actor, ident, key, agent):
        from aftercare.intelligence.tables import RunMutation

        from .investigation_delivery import PROFILE, _covered
        from .investigation_delivery import enabled as investigation_enabled

        route = f"chat:decision-run:{ident}"
        signature = digest({})
        agent_key = digest([route, key])
        with Session(self.engine) as db, db.begin():
            row, issue_id = self._current(db, actor, ident, "support")
            frozen = self.business._replay(db, actor, route, key, signature)
            if frozen is None:
                scope = self.resolve(db, actor, row.id, issue_id)
                head = db.get(DecisionHead, (row.id, issue_id))
                previous = self._snapshot_ref(db, head)
                p4_enabled = investigation_enabled(self.business.settings)
                if (
                    p4_enabled
                    and previous
                    and _covered(
                        db.get(DecisionSnapshot, previous.id), scope, self.business.clock()
                    )
                ):
                    _fail("INVESTIGATION_ALREADY_COVERED")
                frozen = {
                    "scope": scope.model_dump(mode="json"),
                    "previous": previous.model_dump(mode="json") if previous else None,
                    "graph_profile": PROFILE if p4_enabled else None,
                }
                self.business._remember(db, actor, route, key, signature, frozen)
        scope = DecisionScope.model_validate(frozen["scope"])
        previous = DecisionRef.model_validate(frozen["previous"]) if frozen["previous"] else None
        with Session(self.engine) as db:
            started = db.get(RunMutation, (scope.case_id, actor.user_id, agent_key))
            if started is not None:
                return agent.get(scope.case_id, started.run_id, actor)
        return agent.start(
            scope.case_id,
            actor,
            expected_version=scope.business_versions["case"],
            idempotency_key=agent_key,
            mode="single",
            strategy="routed",
            message=scope.frozen_context["legacy_agent_input"]["message"],
            decision_scope=scope,
            decision_previous=previous,
            **(
                {"graph_profile": frozen["graph_profile"], "investigation_arm": "auto"}
                if frozen.get("graph_profile")
                else {}
            ),
        )

    def review(self, actor, ident, ref, review, *, key, expected_review_id, evidence_gaps, chat):
        with Session(self.engine) as db, db.begin():
            row, issue_id = self._current(db, actor, ident, "support")
            scope = f"chat:decision-review:{ident}:{issue_id}"
            signature = digest(
                [
                    ref.model_dump(mode="json"),
                    review.model_dump(mode="json"),
                    expected_review_id,
                    evidence_gaps,
                ]
            )
            previous = self.business._replay(db, actor, scope, key, signature)
            if previous is not None:
                return previous
            result = self.records.review(
                db,
                actor,
                row.id,
                issue_id,
                ref,
                review,
                key=key,
                expected_review_id=expected_review_id,
            )
            action = result["action"]
            selected = None
            if result["program_check"] and result["program_check"]["status"] == "NON_EXECUTING":
                snapshot = db.get_one(DecisionSnapshot, result["snapshot"]["id"])
                selected = next(
                    item
                    for item in snapshot.candidate["options"]
                    if item["id"]
                    == (review.option_id if action == "ACCEPT" else review.replacement.id)
                )
            disposition = selected["action"] if selected else action
            if evidence_gaps and disposition != "REQUEST_EVIDENCE":
                _fail("DECISION_EVIDENCE_GAP_UNEXPECTED", 422)
            if disposition in {"TAKE_OVER", "HUMAN_HANDOFF"}:
                from .analysis import invalidate

                if row.status == "OPEN":
                    row.status = "HUMAN_HANDOFF"
                    chat._touch(row)
                    invalidate(chat.analysis, db, row)
                chat._reply(
                    db,
                    row,
                    "当前需要客服人工核对处理方式，已由客服接手。",
                    [{"type": "status", "data": {"code": "HUMAN_HANDOFF"}}],
                )
            elif disposition == "REQUEST_EVIDENCE":
                current = self.resolve(db, actor, row.id, issue_id)
                codes = evidence_gaps or current.frozen_context["eligibility"]["missing_materials"]
                gaps = [EVIDENCE_GAPS[code] for code in codes if code in EVIDENCE_GAPS]
                if not gaps and selected:
                    gaps = [
                        item
                        for item in selected.get("missing_evidence", [])
                        if item in EVIDENCE_GAPS.values()
                    ]
                gaps = list(dict.fromkeys(gaps))
                if not gaps:
                    _fail("DECISION_EVIDENCE_GAP_REQUIRED", 422)
                decision = db.get_one(SupportDecision, result["id"])
                decision.review = {**decision.review, "evidence_gaps": gaps}
                chat._reply(
                    db, row, f"客服需要核对以下信息：{'、'.join(gaps)}。可以在这里继续补充。", []
                )
            elif disposition == "NOT_SUPPORTED":
                chat._reply(
                    db,
                    row,
                    "客服已核对当前事实和适用规则，暂无法为这些商品办理退款或换发。你仍可补充新情况。",
                    [{"type": "status", "data": {"code": "DECISION_NOT_SUPPORTED"}}],
                )
            elif result["offer"]:
                chat._reply(db, row, "客服已提交处理建议。请查看方案并确认是否接受。", [])
            return self.business._remember(db, actor, scope, key, signature, result)

    def offer(self, actor, ident):
        with Session(self.engine) as db, db.begin():
            row, issue_id = self._current(db, actor, ident, "customer")
            if row.active_case_id is None or not issue_id:
                return {"issue_id": issue_id, "offer": None}
            try:
                offer = self.records.current_offer(db, actor, row.id, issue_id)
            except BusinessError as error:
                if error.code not in {
                    "DECISION_STALE",
                    "DECISION_NOT_FOUND",
                    "DECISION_OFFER_STALE",
                    "DECISION_BINDING_INVALID",
                    "DECISION_INPUT_REQUIRED",
                }:
                    raise
                offer = None
            return {"issue_id": issue_id, "offer": offer}

    def respond(self, actor, ident, ref, response, *, key):
        with Session(self.engine) as db, db.begin():
            row, issue_id = self._current(db, actor, ident, "customer")
            return self.records.respond(db, actor, row.id, issue_id, ref, response, key=key)

    def accepted(
        self, db, actor, case, order, conversation, *, action, lines, policy_ids, stored=None
    ):
        if conversation is None or (
            conversation.active_case_id != case.id
            or conversation.analysis_issue_id != case.decision_issue_id
        ):
            _fail("DECISION_STALE")
        scope = self.freeze(db, conversation, case, order)
        if stored is None:
            head = db.get(
                DecisionHead, (scope.conversation_id, scope.issue_id), with_for_update=True
            )
            offer = db.get(Offer, head.offer_id) if head and head.offer_id else None
            if offer is None:
                _fail("DECISION_CUSTOMER_CONSENT_REQUIRED")
            ref = DecisionRef(id=offer.id, version=offer.version, sha256=offer.sha256)
        else:
            ref = DecisionRef.model_validate(stored["offer"])
        result = self.records.require_accepted_offer(
            db, scope, ref, action=action, lines=lines, policy_ids=policy_ids
        )
        if stored is not None and result != stored:
            _fail("DECISION_STALE")
        return result


def check_proposal(business, db, actor, case, order, conversation, proposal):
    if proposal.decision_binding is None:
        if case.decision_conversation_id is not None:
            _fail("DECISION_CUSTOMER_CONSENT_REQUIRED")
        return
    return ConversationDecisions(business).accepted(
        db,
        actor,
        case,
        order,
        conversation,
        action=proposal.action,
        lines=proposal.lines,
        policy_ids=proposal.policy_ids,
        stored=proposal.decision_binding,
    )
