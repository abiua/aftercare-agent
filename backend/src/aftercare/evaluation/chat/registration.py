"""Trusted registration of original P3 snapshots from an owned paired run."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from aftercare.business.rules import digest, utc
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.runner import _namespace

from .manifest import ARMS, PROTOCOL, bound_rows
from .pg_runtime import _PgRuntime
from .runner import _audit
from .service import (
    _TEST_CASE_LABEL,
    BlindReviewService,
    _blind_candidate_id,
    _fail,
    _frozen_public,
    _image_objects,
)

_NATIVE_FIELDS = (
    "status",
    "reason",
    "run_id",
    "thread_id",
    "route",
    "input_comparable",
    "snapshot_id",
    "candidate_status",
    "program_checks",
    "checkpoint_id",
    "checkpoint_persisted",
)
_STATUSES = {
    "NOT_RUN",
    "UNKNOWN",
    "COMPLETED",
    "NEEDS_REVIEW",
    "WAITING_INPUT",
    "FAILED",
    "CANCELED",
}


def _snapshot_checksum(snapshot, clock_zone):
    return digest(
        {
            "id": snapshot.id,
            "version": snapshot.version,
            "parent_id": snapshot.parent_id,
            "run_id": snapshot.run_id,
            "origin": snapshot.origin,
            "actor_id": snapshot.actor_id,
            "input": snapshot.input,
            "candidate": snapshot.candidate,
            "checks": snapshot.checks,
            "expires_at": utc(snapshot.expires_at).astimezone(clock_zone).isoformat(),
        }
    )


def _plan_rows(runtime, manifest, source):
    plan = manifest.read("plan.json")
    if not plan or plan.get("protocol") != PROTOCOL or plan.get("campaign") != runtime.campaign:
        _fail("BLIND_REVIEW_PAIR_PLAN_INVALID", 422)
    if plan["controls"].get("latency_version") not in {None, "runtime-utc-v1"} or any(
        plan["controls"].get(key) != value for key, value in runtime.control_fields().items()
    ):
        _fail("BLIND_REVIEW_PAIR_PLAN_INVALID", 422)
    case = source.definition

    def matches(row):
        return (row["case_id"], row["repetition"]) == (case.case_id, case.repetition)

    records = [row for row in plan["cases"] if matches(row)]
    rows = [row for row in bound_rows(plan) if matches(row)]
    if (
        records != [source.record]
        or tuple(row["arm"] for row in rows) != ARMS
        or len({row["id"] for row in rows}) != len(ARMS)
        or any(row["input_sha256"] != source.record["scope_sha256"] for row in rows)
    ):
        _fail("BLIND_REVIEW_PAIR_PLAN_INVALID", 422)
    return plan, rows


def _arm_result(runtime, manifest, source, controls, row):
    receipt = manifest.result(row)
    started, ready = manifest.started(row), manifest.ready(row)
    if (not started and (receipt is not None or ready is not None)) or (
        ready is not None and ready["namespace"] != _namespace(row["id"])
    ):
        _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
    record = {
        "row_id": row["id"],
        "arm": row["arm"],
        "status": receipt["status"] if receipt else "UNKNOWN" if started else "NOT_RUN",
        "reason": receipt.get("reason")
        if receipt
        else ("STARTED_WITHOUT_RESULT" if started else "PLANNED_NOT_STARTED"),
        "candidate_status": receipt["candidate_status"] if receipt else "MISSING",
        "run_id": receipt.get("run_id") if receipt else None,
        "snapshot_id": receipt.get("snapshot_id") if receipt else None,
        "snapshot_sha256": None,
        "blind_candidate_id": None,
        "receipt_sha256": digest(receipt) if receipt else None,
    }
    if record["status"] not in _STATUSES or record["candidate_status"] not in {
        "AVAILABLE",
        "MISSING",
        "UNUSABLE",
    }:
        _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
    if not record["run_id"]:
        if (
            record["snapshot_id"]
            or record["candidate_status"] == "AVAILABLE"
            or record["status"] in {"COMPLETED", "NEEDS_REVIEW", "WAITING_INPUT"}
        ):
            _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
        return record, None
    if ready is None or (record["candidate_status"] == "MISSING" and record["snapshot_id"]):
        _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
    with runtime.arm(manifest, row, source, create=False) as (engine, storage, _, _):
        observed = _audit(engine, storage, row, source, controls)
        if observed is None:
            _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
        if any(
            receipt.get(key) is not None and observed.get(key) != receipt[key]
            for key in ("run_id", "thread_id", "snapshot_id")
        ):
            _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
        if record["candidate_status"] != "AVAILABLE":
            return record, None
        observed.update(runtime.checkpoint(row, observed))
        if any(observed.get(key) != receipt.get(key) for key in _NATIVE_FIELDS):
            _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
        if record["status"] not in {"COMPLETED", "NEEDS_REVIEW", "WAITING_INPUT"} or not (
            observed["input_comparable"] and observed["checkpoint_persisted"]
        ):
            _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
        with Session(engine) as db:
            snapshot = db.get(DecisionSnapshot, record["snapshot_id"])
            decision_at = source.definition.decision_at
            runtime_clock = controls.get("latency_version") == "runtime-utc-v1"
            clock_zone = UTC if runtime_clock else decision_at.tzinfo
            if (
                snapshot is None
                or snapshot.sha256 != _snapshot_checksum(snapshot, clock_zone)
                or (
                    utc(snapshot.created_at) < utc(datetime.fromisoformat(observed["started_at"]))
                    if runtime_clock
                    else utc(snapshot.created_at) != decision_at
                )
                or snapshot.run_id != record["run_id"]
                or snapshot.input != source.scope.model_dump(mode="json")
            ):
                _fail("BLIND_REVIEW_PAIR_ARM_INVALID", 422)
            record["snapshot_sha256"] = snapshot.sha256
            record["blind_candidate_id"] = _blind_candidate_id(source.scope, snapshot)
    return record, snapshot


def _save_binding(manifest, name, binding):
    saved = manifest.read(name)
    if saved is None:
        manifest._write(name, binding)
    elif saved != binding:
        _fail("BLIND_REVIEW_PAIR_MAPPING_INVALID")


def register_pg_blind(runtime, manifest, case, *, reviewer_id, case_label):
    """Internal attested entry point. Candidate data comes only from owned native ledgers."""
    if not isinstance(runtime, _PgRuntime) or not _TEST_CASE_LABEL.fullmatch(case_label):
        _fail("BLIND_REVIEW_ASSIGNMENT_INVALID", 422)
    with manifest.lock():
        target = runtime.registration_business()
        source = runtime.freeze(case)
        plan, rows = _plan_rows(runtime, manifest, source)
        records, snapshots = [], []
        for row in rows:
            record, snapshot = _arm_result(runtime, manifest, source, plan["controls"], row)
            records.append(record)
            if snapshot is not None:
                snapshots.append(snapshot)
        private = {
            "plan_sha256": digest(plan),
            "controls_sha256": digest(plan["controls"]),
            "scope_sha256": source.record["scope_sha256"],
            "arms": records,
        }
        if not snapshots:
            outcome = {"status": "NO_REVIEWABLE_CANDIDATE", **private}
            _save_binding(manifest, f"blind-unavailable-{digest(outcome)}.json", outcome)
            return outcome
        if any(snapshot.actor_id == reviewer_id for snapshot in snapshots):
            _fail("BLIND_REVIEW_SOURCE_UNAVAILABLE", 422)
        public = _frozen_public(source.scope)
        with Session(case.source.engine.execution_options(postgresql_readonly=True)) as db:
            logical = _image_objects(db, source.scope, public)
        prefix = f"evaluation/{source.record['source_namespace']}/"
        images = {ident: prefix + key for ident, key in logical.items()}
        source_sha = digest(
            [
                "chat-blind-pg-registration-v1",
                private,
                source.record["scope_sha256"],
                digest(public.model_dump(mode="json")),
                images,
            ]
        )
        service = BlindReviewService(target)
        with Session(service.engine) as db, db.begin():
            service._reviewer(db, reviewer_id, exclusive=True)
            assignment = service._persist_assignment(
                db,
                reviewer_id=reviewer_id,
                case_label=case_label,
                scope=source.scope,
                public=public,
                snapshots=snapshots,
                images=images,
                source_sha=source_sha,
            )
        name = f"blind-{assignment['id']}.json"
        binding = {"task_id": assignment["id"], "source_sha256": source_sha, **private}
        _save_binding(manifest, name, binding)
        return assignment
