"""Transactional decision records. The caller supplies authorized, locked business context."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest, now, utc

from .contracts import Candidate, DecisionRef, NonExecutingOption, Option
from .tables import CustomerDecision, DecisionHead, DecisionSnapshot, Offer, SupportDecision


def _fail(code, status=409):
    raise BusinessError(code, "当前售后决定无法继续。", status)


def _id():
    return str(uuid4())


def _json(model):
    return model.model_dump(mode="json")


def _ref(row):
    return DecisionRef(id=row.id, version=row.version, sha256=row.sha256)


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or value != value.strip():
        _fail("DECISION_REQUEST_KEY_INVALID", 422)
    return value


def _same_scope(frozen, current):
    fields = ("conversation_id", "issue_id", "case_id", "order_id", "customer_id", "line_ids")
    return all(frozen[key] == current[key] for key in fields)


def _same_dependencies(frozen, current):
    fields = ("policy_catalogue_sha256", "dependency_sha256")
    versions = (
        {key: value for key, value in item["business_versions"].items() if key != "case"}
        for item in (frozen, current)
    )
    before, after = versions
    return (
        _same_scope(frozen, current)
        and all(frozen[key] == current[key] for key in fields)
        and before == after
    )


class DecisionService:
    """resolve must authorize and lock conversation before any decision rows are locked."""

    def __init__(self, resolve, validate, *, clock=now, lifetime=timedelta(hours=24)):
        if lifetime <= timedelta(0):
            raise ValueError("Decision lifetime must be positive")
        self.resolve, self.validate, self.clock, self.lifetime = resolve, validate, clock, lifetime

    def _scope(self, db, actor, conversation_id, issue_id, role):
        if actor.role != role:
            _fail("DECISION_FORBIDDEN", 403)
        scope = self.resolve(db, actor, conversation_id, issue_id)
        if (
            scope.conversation_id != conversation_id
            or scope.issue_id != issue_id
            or (role == "customer" and actor.customer_id != scope.customer_id)
        ):
            _fail("DECISION_NOT_FOUND", 404)
        return scope

    @staticmethod
    def _head(db, scope, *, create=False):
        key = (scope.conversation_id, scope.issue_id)
        row = db.get(DecisionHead, key, with_for_update=True)
        if row is None and create:
            row = DecisionHead(conversation_id=key[0], issue_id=key[1], version=0)
            db.add(row)
            db.flush()
        if row is None:
            _fail("DECISION_NOT_FOUND", 404)
        return row

    def _check(self, db, scope, option):
        target = (
            set(option.target_line_ids)
            if isinstance(option, NonExecutingOption)
            else {row.line_id for row in option.lines}
        )
        if not target <= set(scope.line_ids):
            return {"status": "BLOCKED", "code": "DECISION_LINES_OUT_OF_SCOPE"}
        if not set(option.evidence_ids) <= set(scope.evidence_ids):
            return {"status": "BLOCKED", "code": "DECISION_EVIDENCE_OUT_OF_SCOPE"}
        if isinstance(option, NonExecutingOption):
            return {"status": "NON_EXECUTING", "action": option.action}
        try:
            verified = self.validate(db, scope, option)
        except BusinessError as error:
            return {"status": "BLOCKED", "code": error.code}
        if not set(option.policy_ids) <= set(verified["policy_ids"]):
            return {"status": "BLOCKED", "code": "DECISION_POLICY_OUT_OF_SCOPE"}
        return {"status": "PASSED", "policy_ids": sorted(set(verified["policy_ids"]))}

    def _snapshot(self, db, scope, head, actor, candidate, checks, *, parent=None, run_id=None):
        stamp = self.clock()
        snapshot_id = _id()
        inputs, content = _json(scope), _json(candidate)
        version = head.version + 1
        expiry = stamp + self.lifetime
        origin = "AGENT" if run_id is not None else "SUPPORT_MODIFICATION"
        checksum = digest(
            {
                "id": snapshot_id,
                "version": version,
                "parent_id": parent,
                "run_id": run_id,
                "origin": origin,
                "actor_id": actor.user_id,
                "input": inputs,
                "candidate": content,
                "checks": checks,
                "expires_at": expiry.isoformat(),
            }
        )
        row = DecisionSnapshot(
            id=snapshot_id,
            conversation_id=scope.conversation_id,
            issue_id=scope.issue_id,
            version=version,
            parent_id=parent,
            run_id=run_id,
            origin=origin,
            actor_id=actor.user_id,
            sha256=checksum,
            input=inputs,
            candidate=content,
            checks=checks,
            created_at=stamp,
            expires_at=expiry,
        )
        db.add(row)
        db.flush()
        head.version, head.snapshot_id = version, row.id
        head.offer_id = head.review_id = None
        return row

    def record_candidate(self, db, actor, frozen, candidate, *, expected_previous=None):
        """Internal runner hook. frozen comes from the analysis turn's authorized input."""
        if actor.role not in {"customer", "support"}:
            _fail("DECISION_FORBIDDEN", 403)
        scope = self._scope(db, actor, frozen.conversation_id, frozen.issue_id, actor.role)
        head = self._head(db, scope, create=True)
        existing = db.scalar(
            select(DecisionSnapshot).where(
                DecisionSnapshot.conversation_id == scope.conversation_id,
                DecisionSnapshot.issue_id == scope.issue_id,
                DecisionSnapshot.run_id == candidate.run_id,
            )
        )
        if existing is not None:
            if existing.input != _json(frozen) or existing.candidate != _json(candidate):
                _fail("DECISION_IDEMPOTENCY_CONFLICT")
            return _ref(existing)
        if not _same_dependencies(_json(frozen), _json(scope)):
            _fail("DECISION_STALE")
        previous = db.get(DecisionSnapshot, head.snapshot_id) if head.snapshot_id else None
        if (_ref(previous) if previous else None) != expected_previous:
            _fail("DECISION_STALE")
        if previous and head.review_id and _same_dependencies(previous.input, _json(scope)):
            _fail("DECISION_ALREADY_REVIEWED")
        checks = {
            option.id: (
                self._check(db, scope, option)
                if set(option.evidence_ids) <= set(frozen.evidence_ids)
                else {"status": "BLOCKED", "code": "DECISION_EVIDENCE_OUT_OF_SCOPE"}
            )
            for option in candidate.options
        }
        return _ref(
            self._snapshot(db, frozen, head, actor, candidate, checks, run_id=candidate.run_id)
        )

    def _current_snapshot(self, db, head, scope, ref=None, *, new_evidence=()):
        row = db.get(DecisionSnapshot, head.snapshot_id) if head.snapshot_id else None
        current = _json(scope)
        if (
            row is None
            or (ref is not None and _ref(row) != ref)
            or utc(row.expires_at) <= self.clock()
            or not _same_scope(row.input, current)
        ):
            _fail("DECISION_STALE")
        if not _same_dependencies(row.input, current) and not (
            set(new_evidence) - set(row.input["evidence_ids"])
        ):
            _fail("DECISION_STALE")
        return row

    @staticmethod
    def _replay(db, table, scope, actor, key, signature):
        row = db.scalar(
            select(table).where(
                table.conversation_id == scope.conversation_id,
                table.issue_id == scope.issue_id,
                table.actor_id == actor.user_id,
                table.request_key == key,
            )
        )
        if row is not None and row.request_sha256 != signature:
            _fail("DECISION_IDEMPOTENCY_CONFLICT")
        return row

    def review(
        self, db, actor, conversation_id, issue_id, ref, review, *, key, expected_review_id=None
    ):
        scope = self._scope(db, actor, conversation_id, issue_id, "support")
        head = self._head(db, scope)
        signature = digest([_json(ref), _json(review), expected_review_id])
        existing = self._replay(db, SupportDecision, scope, actor, _key(key), signature)
        if existing is not None:
            return self._review_result(db, existing)
        if head.review_id != expected_review_id:
            _fail("DECISION_REVIEW_STALE")
        original = self._current_snapshot(
            db,
            head,
            scope,
            ref,
            new_evidence=review.new_evidence_ids if review.action == "MODIFY" else (),
        )
        if not set(review.new_evidence_ids) <= set(scope.evidence_ids):
            _fail("DECISION_EVIDENCE_OUT_OF_SCOPE", 422)
        candidate = Candidate.model_validate(original.candidate)
        selected = None
        result = original
        check = None
        if review.action == "ACCEPT":
            selected = next(
                (option for option in candidate.options if option.id == review.option_id), None
            )
            if (
                selected is None
                or (isinstance(selected, Option) and candidate.unresolved)
                or original.checks[selected.id]["status"] not in {"PASSED", "NON_EXECUTING"}
            ):
                _fail("DECISION_OPTION_UNAVAILABLE")
            check = self._check(db, scope, selected)
        elif review.action == "MODIFY":
            selected = review.replacement
            check = self._check(db, scope, selected)
            if not set(review.resolved_items) <= set(candidate.unresolved):
                _fail("DECISION_RESOLUTION_UNKNOWN", 422)
            unresolved = tuple(
                item for item in candidate.unresolved if item not in review.resolved_items
            )
            if unresolved and check["status"] == "PASSED":
                check = {"status": "BLOCKED", "code": "DECISION_UNRESOLVED"}
            revised = candidate.model_copy(
                update={"options": (selected,), "unresolved": unresolved}
            )
            result = self._snapshot(
                db, scope, head, actor, revised, {selected.id: check}, parent=original.id
            )

        stamp = self.clock()
        row = SupportDecision(
            id=_id(),
            conversation_id=conversation_id,
            issue_id=issue_id,
            snapshot_id=original.id,
            resulting_snapshot_id=result.id,
            actor_id=actor.user_id,
            request_key=key,
            request_sha256=signature,
            review={**_json(review), "program_check": check},
            created_at=stamp,
        )
        db.add(row)
        db.flush()
        head.offer_id = None
        head.review_id = row.id
        if isinstance(selected, Option) and check["status"] == "PASSED":
            option = {
                "action": selected.action,
                "lines": selected.business_lines(),
                "policy_ids": check["policy_ids"],
            }
            offer_id = _id()
            checksum = digest(
                [offer_id, _json(_ref(result)), row.id, option, utc(result.expires_at).isoformat()]
            )
            offer = Offer(
                id=offer_id,
                snapshot_id=result.id,
                support_decision_id=row.id,
                version=result.version,
                sha256=checksum,
                option=option,
                expires_at=result.expires_at,
                created_at=stamp,
            )
            db.add(offer)
            db.flush()
            head.offer_id = offer.id
        return self._review_result(db, row)

    @staticmethod
    def _offer(row):
        return {
            **_json(_ref(row)),
            **deepcopy(row.option),
            "expires_at": utc(row.expires_at).isoformat(),
        }

    def _review_result(self, db, review):
        offer = db.scalar(select(Offer).where(Offer.support_decision_id == review.id))
        snapshot = db.get_one(DecisionSnapshot, review.resulting_snapshot_id)
        return {
            "id": review.id,
            "snapshot": _json(_ref(snapshot)),
            "action": review.review["action"],
            "program_check": deepcopy(review.review["program_check"]),
            "offer": self._offer(offer) if offer else None,
        }

    def _current_offer(self, db, head, scope, ref):
        snapshot = self._current_snapshot(db, head, scope)
        offer = db.get(Offer, head.offer_id) if head.offer_id else None
        if (
            offer is None
            or (ref is not None and _ref(offer) != ref)
            or offer.snapshot_id != snapshot.id
            or utc(offer.expires_at) <= self.clock()
        ):
            _fail("DECISION_OFFER_STALE")
        return offer

    def current_offer(self, db, actor, conversation_id, issue_id):
        scope = self._scope(db, actor, conversation_id, issue_id, "customer")
        head = self._head(db, scope)
        offer = self._current_offer(db, head, scope, None)
        response = self.latest_customer_response(db, offer.id)
        return {
            **self._offer(offer),
            "customer_decision": {"id": response.id, "action": response.response["action"]}
            if response
            else None,
        }

    def snapshot(self, db, actor, conversation_id, issue_id, snapshot_id):
        scope = self._scope(db, actor, conversation_id, issue_id, "support")
        row = db.get(DecisionSnapshot, snapshot_id)
        if row is None or (row.conversation_id, row.issue_id) != (
            scope.conversation_id,
            scope.issue_id,
        ):
            _fail("DECISION_NOT_FOUND", 404)
        return self.snapshot_payload(row)

    @staticmethod
    def snapshot_payload(row):
        return {
            **_json(_ref(row)),
            "parent_id": row.parent_id,
            "origin": row.origin,
            "actor_id": row.actor_id,
            "original_run_id": row.candidate["run_id"],
            "input": deepcopy(row.input),
            "candidate": deepcopy(row.candidate),
            "checks": deepcopy(row.checks),
            "expires_at": utc(row.expires_at).isoformat(),
        }

    @staticmethod
    def latest_customer_response(db, offer_id):
        return db.scalar(
            select(CustomerDecision)
            .where(CustomerDecision.offer_id == offer_id)
            .order_by(CustomerDecision.version.desc())
            .limit(1)
        )

    def respond(self, db, actor, conversation_id, issue_id, ref, response, *, key):
        scope = self._scope(db, actor, conversation_id, issue_id, "customer")
        head = self._head(db, scope)
        signature = digest([_json(ref), _json(response)])
        existing = self._replay(db, CustomerDecision, scope, actor, _key(key), signature)
        if existing is not None:
            return {"id": existing.id, "action": existing.response["action"], "offer": _json(ref)}
        offer = self._current_offer(db, head, scope, ref)
        previous = self.latest_customer_response(db, offer.id)
        if previous and (previous.response["action"] != "ACCEPT" or response.action == "ACCEPT"):
            _fail("DECISION_OFFER_ALREADY_ANSWERED")
        row = CustomerDecision(
            id=_id(),
            conversation_id=conversation_id,
            issue_id=issue_id,
            offer_id=offer.id,
            version=previous.version + 1 if previous else 1,
            actor_id=actor.user_id,
            request_key=key,
            request_sha256=signature,
            response=_json(response),
            created_at=self.clock(),
        )
        db.add(row)
        db.flush()
        return {"id": row.id, "action": response.action, "offer": _json(ref)}

    def require_accepted_offer(self, db, scope, ref, *, action, lines, policy_ids):
        """For the future BusinessService transaction, after resolving its locked scope."""
        head = self._head(db, scope)
        offer = self._current_offer(db, head, scope, ref)
        accepted = self.latest_customer_response(db, offer.id)
        requested = {
            "action": action,
            "lines": sorted(lines, key=lambda line: line["line_id"]),
            "policy_ids": sorted(policy_ids),
        }
        actual = {
            **offer.option,
            "lines": sorted(offer.option["lines"], key=lambda line: line["line_id"]),
        }
        if (
            accepted is None
            or accepted.response["action"] != "ACCEPT"
            or digest(requested) != digest(actual)
        ):
            _fail("DECISION_CUSTOMER_CONSENT_REQUIRED")
        return {
            "offer": _json(ref),
            "customer_decision_id": accepted.id,
            "snapshot_id": offer.snapshot_id,
        }
