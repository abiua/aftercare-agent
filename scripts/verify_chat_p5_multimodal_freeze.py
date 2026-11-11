"""Freeze an existing human-reviewed 5202 chat as a private V7 evaluation source."""

import argparse
import json
import os
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from unittest.mock import patch
from uuid import UUID

import offline_demo
from aftercare.business.rules import digest
from verify_chat_backup_restore import private_json
from verify_chat_p2_offline import owned_container
from verify_chat_p5_multimodal_source import BACKEND, FRONTEND, ROOT, STATE, URL
from verify_chat_p5_paid import CALLS, CASE, _check_v3_vision, _paid_topology, _v3_commitments
from verify_fresh_bootstrap import VerificationError, private_write, require
from verify_material_review_migration import _docker, _hash
from verify_streaming_compatibility import _save

PROTOCOL = "p5-v7-live-source-v1"
CAMPAIGN = "p5-v7-live-source-20261111"
VISION_SHA = "d4467fcd6c4ecd6a44b5756c3b86ac5799e18bff2eb86517e45b34d63cbe94ef"
VISION = ROOT / "reports/chat-p5-v7-multimodal-vision-20261111.json"
STAGE = "multimodal-source-freeze-v1"
MOUNT = Path("/source-freeze")
PROVENANCE = ("HUMAN_OPERATOR_REVIEW",)
COUNTS = (
    "agent_runs",
    "agent_model_attempts",
    "chat_decision_snapshots",
    "intake_model_audits",
    "business_approvals",
    "business_executions",
    "business_simulated_payments",
    "chat_model_attempts",
    "chat_observations",
)


def _vision(path):
    report = private_json(path)
    require(
        _hash(path) == VISION_SHA
        and report["status"] == "PASSED"
        and digest(report["plan"]) == report["plan_sha256"]
        and report["paid_worker_removed"]
        and report["private_worker_key_removed"]
        and len(report["ordered_observations"]) == 2,
        "P5_MULTIMODAL_FREEZE_VISION_REPORT_CHANGED",
    )
    return report


def _review_preflight(stack, identifiers):
    code = (
        f"P={identifiers!r}\nTABLES={COUNTS!r}\n"
        + """
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine=guarded_engine(load_settings('test'),test_only=True)
with engine.begin() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()'))==P['system_identifier']
    c=db.execute(text(
        'SELECT c.active_case_id,c.active_order_id,c.analysis_issue_id,c.status,'
        'b.materials_version FROM chat_conversations c JOIN business_cases b ON b.id=c.active_case_id '
        'WHERE c.id=:conversation_id'
    ),P).one()
    assert tuple(c)[:3]==(P['case_id'],'O9902',P['issue_id'])
    rows=[dict(row) for row in db.execute(text(
        'SELECT id,decision,reviewer_id,created_at::text,observations,materials_version '
        'FROM business_material_reviews WHERE case_id=:case_id AND materials_version=:materials_version'
    ),{**P,'materials_version':c.materials_version}).mappings()]
    links=[dict(row) for row in db.execute(text(
        'SELECT material_id,attachment_id,sha256,kind FROM chat_material_sources '
        'WHERE conversation_id=:conversation_id AND case_id=:case_id AND issue_id=:issue_id '
        'ORDER BY attachment_id'
    ),P).mappings()]
    counts={name:db.scalar(text('SELECT count(*) FROM public.'+name)) for name in TABLES}
    eval_schemas=db.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname ~ '^eval_[a-f0-9]{32}$'"))
    actor=db.scalar(text("SELECT EXISTS(SELECT 1 FROM users WHERE id='support' AND role='support' AND enabled)"))
print(json.dumps(dict(reviews=rows,links=links,counts=counts,eval_schemas=eval_schemas,support_active=actor)))
engine.dispose()
"""
    )
    return stack.json_exec("P5_MULTIMODAL_FREEZE_PREFLIGHT", owned_container(stack, "api"), code)


def _check_counts(counts):
    require(
        counts
        == {
            name: {"chat_model_attempts": 1, "chat_observations": 2}.get(name, 0) for name in COUNTS
        },
        "P5_MULTIMODAL_FREEZE_LEDGER_CHANGED",
    )


def _review_binding(data, vision_plan, review_id, decision_at):
    _check_counts(data["counts"])
    require(
        data["eval_schemas"] == 0 and data["support_active"],
        "P5_MULTIMODAL_FREEZE_SCOPE_NOT_PRISTINE",
    )
    require(len(data["links"]) == 2, "P5_MULTIMODAL_FREEZE_TWO_CONFIRMED_MATERIALS_REQUIRED")
    by_attachment = {item["attachment_id"]: item for item in data["links"]}
    require(
        set(by_attachment) == set(vision_plan["attachment_ids"])
        and [
            (by_attachment[ident]["sha256"], by_attachment[ident]["kind"])
            for ident in vision_plan["attachment_ids"]
        ]
        == list(zip(vision_plan["original_image_sha256s"], ("ITEM", "PARCEL"), strict=True)),
        "P5_MULTIMODAL_FREEZE_MATERIAL_ORIGINAL_MISMATCH",
    )
    require(len(data["reviews"]) == 1, "P5_MULTIMODAL_FREEZE_EFFECTIVE_REVIEW_REQUIRED")
    review = data["reviews"][0]
    observations = review.get("observations")
    require(
        review["id"] == review_id
        and review["reviewer_id"] == "support"
        and datetime.fromisoformat(review["created_at"]) <= decision_at
        and isinstance(observations, list)
        and len(observations) == 2
        and {item["material_id"] for item in observations}
        == {item["material_id"] for item in data["links"]}
        and all(item["source"] == "HUMAN_MATERIAL_REVIEW" for item in observations),
        "P5_MULTIMODAL_FREEZE_TYPED_REVIEW_REQUIRED",
    )
    return {
        "review_id": review_id,
        "review_decision": review["decision"],
        "review_sha256": digest(review),
        "material_links_sha256": digest(data["links"]),
    }


def _plan(review_id, provenance, decision_at):
    state = private_json(STATE)
    vision = _vision(VISION)
    vp = vision["plan"]
    require(
        state["run_id"] == vision["owned_stack"]
        and state["url"] == vision["owned_url"] == URL
        and state["profile"] == "test"
        and state["status"] == "READY"
        and vp["owned_stack"] == state["run_id"]
        and vp["scope"] == "P2_ISOLATED_P5_EXACT_TWO_IMAGE_V1",
        "P5_MULTIMODAL_FREEZE_STACK_CHANGED",
    )
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "P5_MULTIMODAL_FREEZE_LOCAL_DOCKER_REQUIRED",
    )
    stack = offline_demo.restore(state)
    network, identity = _paid_topology(stack, {"backend": BACKEND, "frontend": FRONTEND}, v3=True)
    require(
        identity == vp["system_identifier"] and not (stack.directory / STAGE).exists(),
        "P5_MULTIMODAL_FREEZE_OWNED_SOURCE_UNAVAILABLE",
    )
    ident = {
        key: vp[key] for key in ("conversation_id", "case_id", "issue_id", "system_identifier")
    }
    data = _review_preflight(stack, ident)
    return (
        {
            "protocol": PROTOCOL,
            "source_campaign": CAMPAIGN,
            "images": {"backend": BACKEND, "frontend": FRONTEND},
            "owned_stack": stack.run_id,
            "owned_url": URL,
            "network": network,
            **ident,
            **_review_binding(data, vp, review_id, decision_at),
            "review_provenance": provenance,
            "decision_at": decision_at.isoformat(),
            "vision_report_sha256": VISION_SHA,
            "script_sha256": _hash(Path(__file__)),
            "maximum_model_calls": 0,
            "maximum_financial_writes": 0,
            "public_source_preserved": True,
        },
        stack,
        state,
    )


def _validate_source(record, plan, vision):
    frozen = record["scope"]["frozen_context"]
    _check_v3_vision(record, vision)
    reviews = [row for row in frozen["reviews"] if row["effective"]]
    require(
        len(reviews) == 1
        and reviews[0]["id"] == plan["review_id"]
        and reviews[0]["decision"] == plan["review_decision"],
        "P5_MULTIMODAL_FREEZE_EFFECTIVE_REVIEW_CHANGED",
    )
    observation_rows = reviews[0].get("observations", [])
    require(
        len(observation_rows) == 2
        and all(len(row["original_attachments"]) == 1 for row in observation_rows)
        and {
            (row["original_attachments"][0]["attachment_id"], row["material_sha256"])
            for row in observation_rows
        }
        == set(
            zip(
                vision["plan"]["attachment_ids"],
                vision["plan"]["original_image_sha256s"],
                strict=True,
            )
        ),
        "P5_MULTIMODAL_FREEZE_TYPED_ORIGINAL_MISMATCH",
    )
    commitments = _v3_commitments(record)
    require(
        len(commitments["observation_commitments"]) == 2
        and len(commitments["human_review_commitments"]) == 1,
        "P5_MULTIMODAL_FREEZE_COMMITMENTS_INCOMPLETE",
    )
    commitments.pop("role_evidence")
    return commitments


def _worker():
    from aftercare.auth import UserContext
    from aftercare.business.errors import BusinessError
    from aftercare.business.service import BusinessService
    from aftercare.business.storage import S3ObjectStore, verify_object
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import FrozenCase, _freeze_case, _populate
    from sqlalchemy import text

    plan = private_json(MOUNT / "plan.json")
    vision = _vision(MOUNT / "vision.json")
    require(
        plan["protocol"] == PROTOCOL
        and plan["source_campaign"] == CAMPAIGN
        and plan["script_sha256"] == _hash(Path(__file__)),
        "P5_MULTIMODAL_FREEZE_WORKER_PLAN_MISMATCH",
    )
    raw = load_settings("test")
    require(
        raw.container
        and raw.chat_decisions_enabled
        and raw.chat_analysis_enabled
        and raw.chat_investigation_protocol == "compact-v7"
        and not any(
            (
                raw.model_enabled,
                raw.embedding_enabled,
                raw.chat_vision_enabled,
                raw.chat_investigation_enabled,
            )
        ),
        "P5_MULTIMODAL_FREEZE_OFFLINE_TEST_REQUIRED",
    )
    settings = raw.model_copy(
        update={
            "chat_analysis_enabled": False,
            "chat_investigation_enabled": True,
            "chat_investigation_issue_max_calls": CALLS,
            "chat_investigation_issue_max_microusd": 400_000,
        }
    )
    engine = guarded_engine(settings, test_only=True)
    storage = S3ObjectStore(settings)
    at = datetime.fromisoformat(plan["decision_at"])
    actor = UserContext(user_id="support", role="support")

    def counts():
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            identity = db.execute(
                text(
                    "SELECT current_database(),current_user,session_user,current_setting('cluster_name'),"
                    "system_identifier::text FROM pg_control_system()"
                )
            ).one()
            require(
                tuple(identity)
                == (
                    "ac_test",
                    "ac_test",
                    "ac_test",
                    "aftercare-v2-test",
                    plan["system_identifier"],
                ),
                "P5_MULTIMODAL_FREEZE_DATABASE_IDENTITY_MISMATCH",
            )
            values = {
                name: db.scalar(text("SELECT count(*) FROM public." + name)) for name in COUNTS
            }
            _check_counts(values)
            review = (
                db.execute(
                    text(
                        "SELECT id,decision,reviewer_id,created_at::text,observations,materials_version "
                        "FROM business_material_reviews WHERE id=:id AND case_id=:case"
                    ),
                    {"id": plan["review_id"], "case": plan["case_id"]},
                )
                .mappings()
                .one()
            )
            links = list(
                db.execute(
                    text(
                        "SELECT material_id,attachment_id,sha256,kind FROM chat_material_sources "
                        "WHERE conversation_id=:conversation_id AND case_id=:case_id AND issue_id=:issue_id "
                        "ORDER BY attachment_id"
                    ),
                    plan,
                ).mappings()
            )
            require(
                digest(dict(review)) == plan["review_sha256"]
                and digest([dict(row) for row in links]) == plan["material_links_sha256"],
                "P5_MULTIMODAL_FREEZE_REVIEW_DRIFT",
            )
            return values

    try:
        before = counts()
        case = FrozenCase(
            CASE,
            1,
            BusinessService(engine, settings, storage=storage, clock=lambda: at),
            actor,
            plan["conversation_id"],
            plan["issue_id"],
            at,
            "Synthetic cup and parcel; real P2 observations; " + plan["review_provenance"],
        )
        original = _freeze_case(case)
        commitments = _validate_source(original.record, plan, vision)
        key = sentinel_key(plan["owned_stack"])
        try:
            storage.get(key, 64)
        except BusinessError as error:
            require(
                error.code == "MATERIAL_OBJECT_MISSING", "P5_MULTIMODAL_FREEZE_SENTINEL_READ_FAILED"
            )
        else:
            raise ValueError("P5_MULTIMODAL_FREEZE_SENTINEL_ALREADY_PRESENT")
        sentinel = token_bytes(64)
        fingerprint = sha256(sentinel).hexdigest()
        storage.put(key, sentinel, "application/octet-stream")
        verify_object(storage.get(key, 64), 64, fingerprint)
        stamp = PgAttestation(
            plan["owned_stack"], plan["system_identifier"], storage.bucket, fingerprint, 64
        )
        source_sha = digest(
            [PROTOCOL, plan["vision_report_sha256"], plan["review_sha256"], original.record]
        )
        with open_pg_runtime(settings, stamp, CAMPAIGN) as runtime:
            with runtime.source(CASE, 1, source_sha) as (target, objects, fresh):
                require(fresh, "P5_MULTIMODAL_FREEZE_SOURCE_ALREADY_PRESENT")
                _populate(original, target, objects)
                clone = replace(
                    case,
                    source=BusinessService(target, settings, storage=objects, clock=lambda: at),
                )
                compared = _freeze_case(clone)
                require(compared.record == original.record, "P5_MULTIMODAL_FREEZE_CLONE_DRIFT")
            source = runtime.freeze(clone).record
            require(
                _validate_source(source, plan, vision) == commitments,
                "P5_MULTIMODAL_FREEZE_CLONE_COMMITMENT_DRIFT",
            )
        require(
            counts() == before and _freeze_case(case).record == original.record,
            "P5_MULTIMODAL_FREEZE_PUBLIC_SOURCE_CHANGED",
        )
        private_write(
            MOUNT / "result.json",
            json.dumps(
                {
                    "source": source,
                    "source_record_sha256": digest(source),
                    "acceptance": {
                        "system_identifier": stamp.system_identifier,
                        "object_sentinel_sha256": fingerprint,
                        "public_counts": before,
                    },
                    **commitments,
                }
            ),
        )
    finally:
        engine.dispose()


def _execute(plan, stack, state, report):
    stage = stack.directory / STAGE
    stage.mkdir(mode=0o700)
    private_write(stage / "plan.json", json.dumps(plan))
    private_write(stage / "vision.json", VISION.read_text())
    name = f"{stack.prefix}-multimodal-source-verifier"
    require(stack.owned("container", name) is None, "P5_MULTIMODAL_FREEZE_VERIFIER_ALREADY_EXISTS")
    stack.resources.append(("container", name))
    state["resources"] = stack.resources
    offline_demo.save(state)
    try:
        result = stack.docker(
            "P5_MULTIMODAL_FREEZE_VERIFIER",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            stack.label,
            "--network",
            plan["network"],
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={stage},target=/source-freeze",
            "--mount",
            f"type=bind,source={ROOT / 'scripts'},target=/workspace/scripts,readonly",
            "--entrypoint",
            "python",
            BACKEND,
            "/workspace/scripts/verify_chat_p5_multimodal_freeze.py",
            "_worker",
            timeout=120,
            required=False,
            log_output=False,
        )
        if result is None or result.returncode:
            last = (
                result.stderr.strip().splitlines()[-1] if result and result.stderr.strip() else ""
            )
            raise ValueError(
                last
                if re.fullmatch(r"P5_MULTIMODAL_FREEZE_[A-Z0-9_]+", last)
                else "P5_MULTIMODAL_FREEZE_VERIFIER_FAILED"
            )
        outcome = private_json(stage / "result.json")
        require(
            _hash(VISION) == VISION_SHA
            and outcome["source_record_sha256"] == digest(outcome["source"]),
            "P5_MULTIMODAL_FREEZE_SOURCE_REPORT_CHANGED",
        )
        report.update(outcome)
    finally:
        owned = stack.owned("container", name)
        require(owned is not False, "P5_MULTIMODAL_FREEZE_VERIFIER_UNOWNED")
        if owned:
            stack.docker(
                "P5_MULTIMODAL_FREEZE_VERIFIER_CLEANUP", "rm", "-f", name, log_output=False
            )
        require(stack.owned("container", name) is None, "P5_MULTIMODAL_FREEZE_VERIFIER_REMAINS")
        stack.resources.remove(("container", name))
        offline_demo.save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-id", type=UUID, required=True)
    parser.add_argument("--review-provenance", choices=PROVENANCE, required=True)
    parser.add_argument("--decision-at", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    at = datetime.fromisoformat(args.decision_at)
    require(
        at.utcoffset() is not None and at <= datetime.now(UTC),
        "P5_MULTIMODAL_FREEZE_DECISION_TIME_INVALID",
    )
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    with (
        offline_demo.lifecycle_lock(STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", STATE),
    ):
        plan, stack, state = _plan(str(args.review_id), args.review_provenance, at.astimezone(UTC))
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}, indent=2
                )
            )
            return 0
        require(checksum == args.expected_plan_sha256, "P5_MULTIMODAL_FREEZE_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "protocol": PROTOCOL,
            "plan": plan,
            "plan_sha256": checksum,
            "owned_stack": stack.run_id,
            "owned_url": URL,
            "source_campaign": CAMPAIGN,
            "vision_report_path": str(VISION),
            "vision_report_sha256": VISION_SHA,
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _execute(plan, stack, state, report)
            report["status"] = "PASSED"
        except Exception as error:  # noqa: BLE001 - retain safe failure without leaking review notes
            code = str(error)
            report.update(
                status="FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "_worker":
            _worker()
        else:
            raise SystemExit(main())
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(
            code if re.fullmatch(r"[A-Z0-9_]+", code) else "P5_MULTIMODAL_FREEZE_BLOCKED",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
