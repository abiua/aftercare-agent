"""Verify blind registration from the existing owned P5 PostgreSQL campaign."""

import argparse
import json
import os
import re
import stat
import sys
from hashlib import sha256
from pathlib import Path

SCRIPT = Path(__file__).resolve()
VERIFIER_IMAGE = "sha256:22ca76e1dfcf2fba79734280dc298fc065b16493e408f4e2e34d540ee1d7e07a"
CASES = ("DAMAGE-ORDINARY", "DAMAGE-TWO-LINE-PACKAGE-CONFLICT")
BLIND_TABLES = {
    "eval_chat_blind_tasks": "id",
    "eval_chat_blind_initials": "task_id",
    "eval_chat_blind_exposures": "task_id",
    "eval_chat_blind_labels": "task_id",
}
DELTAS = {
    "public.eval_chat_blind_tasks": 2,
    "public.eval_chat_blind_initials": 1,
    "public.eval_chat_blind_exposures": 1,
    "public.eval_chat_blind_labels": 1,
    "public.eval_mutations": 3,
}


def _check(condition, code):
    if not condition:
        raise ValueError(code)


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _journal_hashes(directory):
    result = {}
    for file in sorted(directory.glob("*.json")):
        if not file.name.startswith("blind-"):
            info = file.lstat()
            _check(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0,
                "P5_REGISTRATION_MANIFEST_NOT_PRIVATE",
            )
            result[file.name] = _hash(file)
    return result


def _prepare(report_path, verifier_image, exercise):
    from verify_chat_backup_restore import _p5_pg_evidence, private_json, source_resources
    from verify_chat_p5_pg import BACKEND, STATE, _topology

    endpoint_override = os.environ.get("DOCKER_HOST")
    _check(
        not endpoint_override or endpoint_override.startswith("unix://"),
        "P5_REGISTRATION_LOCAL_DOCKER_REQUIRED",
    )
    state, stack, names = source_resources(STATE)
    endpoint = stack.docker(
        "P5_REGISTRATION_LOCAL_CONTEXT",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        log_output=False,
    ).stdout.strip()
    _check(endpoint.startswith("unix://"), "P5_REGISTRATION_LOCAL_DOCKER_REQUIRED")
    report = private_json(report_path)
    evidence = _p5_pg_evidence(report_path, STATE, state, stack, names)
    images = report["plan"]["images"]
    network, system_id = _topology(stack, images)
    image = stack.docker(
        "P5_REGISTRATION_VERIFIER_IMAGE",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        verifier_image,
        log_output=False,
    ).stdout.strip()
    _check(
        image == verifier_image
        and image != images[BACKEND]
        and system_id == evidence["system_identifier"],
        "P5_REGISTRATION_TOPOLOGY_MISMATCH",
    )
    directory = Path(report["private_manifest_directory"])
    plan = {
        "scope": "OWNED_P5_EXISTING_PG_CROSS_SCHEMA_BLIND_REGISTRATION",
        "stack_id": state["run_id"],
        "source_url": state["url"],
        "network": network,
        "system_identifier": system_id,
        "verifier_image": image,
        "verifier_script_sha256": _hash(SCRIPT),
        "recovered_report_sha256": evidence["report_sha256"],
        "campaign": report["plan"]["campaign"],
        "manifest_directory": str(directory),
        "paired_plan_sha256": evidence["plan_sha256"],
        "journal_sha256": _journal_hashes(directory),
        "sentinel_sha256": evidence["sentinel_sha256"],
        "cases": list(CASES),
        "exercise_ordinary": exercise,
        "maximum_new_tasks": 2,
        "maximum_model_calls": 0,
        "maximum_financial_writes": 0,
    }
    return plan, state, stack


def _worker(plan):
    from datetime import datetime
    from secrets import compare_digest

    from aftercare import backup
    from aftercare.auth import UserContext, token_hash
    from aftercare.business.errors import BusinessError
    from aftercare.business.rules import digest
    from aftercare.business.service import BusinessService
    from aftercare.business.storage import verify_object
    from aftercare.config import load_settings
    from aftercare.db import User
    from aftercare.decisions.contracts import DecisionScope
    from aftercare.evaluation.chat.contracts import InitialJudgment, PostLabels, Versioned
    from aftercare.evaluation.chat.manifest import Manifest
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime
    from aftercare.evaluation.chat.registration import register_pg_blind
    from aftercare.evaluation.chat.runner import FrozenCase
    from aftercare.evaluation.chat.service import BlindReviewService
    from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindLabels, BlindTask
    from aftercare.evaluation.tables import EvaluationMutation
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session

    _check(_hash(SCRIPT) == plan["verifier_script_sha256"], "P5_REGISTRATION_SCRIPT_CHANGED")
    settings = load_settings("test")
    attest = PgAttestation(
        stack_id=plan["stack_id"],
        system_identifier=plan["system_identifier"],
        bucket=settings.object_bucket,
        sentinel_sha256=plan["sentinel_sha256"],
        sentinel_size=64,
    )
    manifest = Manifest(Path("/paired-manifest"))
    frozen = manifest.read("plan.json")
    _check(
        digest(frozen) == plan["paired_plan_sha256"]
        and frozen["campaign"] == plan["campaign"]
        and frozen["controls"]["evidence_grade"] == "CONTROLLED_WORKER_CORE",
        "P5_REGISTRATION_PLAN_MISMATCH",
    )
    sources = {row["case_id"]: row for row in frozen["cases"]}
    _check(
        len(frozen["cases"]) == 2
        and set(sources) == set(plan["cases"]) == set(CASES)
        and _journal_hashes(manifest.directory) == plan["journal_sha256"],
        "P5_REGISTRATION_CASES_MISMATCH",
    )
    exercise = plan["exercise_ordinary"]
    allowed = DELTAS if exercise else {"public.eval_chat_blind_tasks": 2}

    def state(target):
        with target.engine.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            tables = backup._fingerprints(db)
            objects = backup._materials(db)
        for item in objects:
            verify_object(target.storage.get(item.key, item.size), item.size, item.sha256)
        return tables, objects

    # The credential is read only within the private, offline verifier.
    credentials_path = Path("/workspace/.runtime/users-test.json")
    permissions = credentials_path.lstat()
    _check(
        permissions.st_uid == os.getuid()
        and permissions.st_mode & 0o077 == 0
        and credentials_path.is_file()
        and not credentials_path.is_symlink(),
        "P5_REGISTRATION_CREDENTIALS_NOT_PRIVATE",
    )
    testers = [
        item for item in json.loads(credentials_path.read_bytes()) if item["role"] == "tester"
    ]
    _check(len(testers) == 1, "P5_REGISTRATION_TESTER_REQUIRED")
    tester = testers[0]

    with open_pg_runtime(settings, attest, plan["campaign"]) as runtime:
        target = runtime.registration_business()
        with Session(target.engine) as db:
            user = db.get_one(User, tester["user_id"])
            _check(
                user.enabled
                and user.role == "tester"
                and compare_digest(user.token_hash, token_hash(tester["token"])),
                "P5_REGISTRATION_TESTER_BINDING_INVALID",
            )
            actor = UserContext(user_id=user.id, role="tester")
            _check(
                not db.scalar(
                    select(BlindTask.id)
                    .where(
                        BlindTask.reviewer_id == actor.user_id,
                        BlindTask.case_label.in_(CASES),
                    )
                    .limit(1)
                ),
                "P5_REGISTRATION_TASK_ALREADY_EXISTS",
            )
        before, original_objects = state(target)
        # Pin all prior rows in the tables the exercise is allowed to extend.
        allowed_tables = dict(BLIND_TABLES)
        if exercise:
            allowed_tables["eval_mutations"] = "object_id"

        def prior_rows(exclude):
            result = {}
            with target.engine.connect() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                for table, key in allowed_tables.items():
                    name = backup._qualified("public", table)
                    records = db.execute(text(f"SELECT {key}, to_jsonb(t) FROM {name} t"))
                    result[table] = sorted(
                        digest(value) for ident, value in records if ident not in exclude
                    )
            return result

        historical = prior_rows(set())
        tasks = {}
        for case_id in CASES:
            record = sources[case_id]
            scope = DecisionScope.model_validate(record["scope"])
            repetition = record["repetition"]
            decision_at = datetime.fromisoformat(record["decision_at"])
            with runtime.source(
                case_id, repetition, record["source_sha256"], require_existing=True
            ) as (engine, storage, fresh):
                _check(not fresh, "P5_REGISTRATION_SOURCE_WAS_NOT_REUSED")
                case = FrozenCase(
                    case_id,
                    repetition,
                    BusinessService(engine, settings, storage=storage),
                    UserContext(user_id="support", role="support"),
                    scope.conversation_id,
                    scope.issue_id,
                    decision_at,
                    record["provenance"],
                )
            assignment = register_pg_blind(
                runtime,
                manifest,
                case,
                reviewer_id=actor.user_id,
                case_label=case_id,
            )
            _check(assignment["status"] == "BLIND", "P5_REGISTRATION_TASK_NOT_BLIND")
            tasks[case_id] = assignment

        service = BlindReviewService(target)
        for case_id, task in tasks.items():
            detail = service.get(actor, task["id"])
            binding = manifest.read(f"blind-{task['id']}.json")
            expected = [row for row in frozen["rows"] if row["case_id"] == case_id]
            _check(
                detail["status"] == "BLIND"
                and detail["exposure"] is detail["initial"] is detail["labels"] is None
                and digest(detail["input"]) == task["input_sha256"]
                and binding["plan_sha256"] == plan["paired_plan_sha256"]
                and len(binding["arms"]) == 3
                and [row["row_id"] for row in binding["arms"]] == [row["id"] for row in expected]
                and all(
                    row["snapshot_sha256"] and row["blind_candidate_id"] for row in binding["arms"]
                )
                and len({row["blind_candidate_id"] for row in binding["arms"]}) == 3
                and all(
                    row[key] not in json.dumps(detail)
                    for row in binding["arms"]
                    for key in ("run_id", "snapshot_id", "blind_candidate_id")
                ),
                "P5_REGISTRATION_BINDING_MISMATCH",
            )
            for image in detail["input"]["images"]:
                content, media = service.image(actor, task["id"], image["id"])
                verify_object(content, image["size"], image["sha256"])
                _check(media == image["content_type"], "P5_REGISTRATION_IMAGE_MEDIA_CHANGED")
            task["private_mapping_sha256"] = digest(binding)
            task["candidate_count"] = 3

        ordinary = tasks[CASES[0]]
        if exercise:
            ident = ordinary["id"]
            key = "p5-pg-registration-"
            try:
                service.write(actor, ident, "reveal", Versioned(expected_version=1), key + "early")
            except BusinessError as error:
                _check(
                    error.code == "BLIND_REVIEW_STAGE_CHANGED",
                    "P5_REGISTRATION_EARLY_REVEAL_UNVERIFIED",
                )
            else:
                raise ValueError("P5_REGISTRATION_EARLY_REVEAL_ALLOWED")
            initial = InitialJudgment(
                expected_version=1,
                judgment="UNDETERMINED",
                notes="控制验收，未作独立人工判断。",
            )
            sealed = service.write(actor, ident, "initial", initial, key + "initial")
            _check(
                sealed["status"] == "SEALED" and sealed["exposure"] is None,
                "P5_REGISTRATION_SEAL_FAILED",
            )
            reveal = service.write(
                actor, ident, "reveal", Versioned(expected_version=2), key + "reveal"
            )
            _check(
                reveal["status"] == "REVEALED" and len(reveal["exposure"]["candidates"]) == 3,
                "P5_REGISTRATION_REVEAL_FAILED",
            )
            label = PostLabels(
                expected_version=3,
                items=[
                    {"candidate_id": item["id"], "decision": "UNDETERMINED"}
                    for item in reveal["exposure"]["candidates"]
                ],
            )
            labeled = service.write(actor, ident, "labels", label, key + "labels")
            _check(
                labeled["status"] == "LABELED"
                and service.write(actor, ident, "labels", label, key + "labels") == labeled,
                "P5_REGISTRATION_LABELS_FAILED",
            )
            ordinary.update(status=labeled["status"], version=labeled["version"])
            with Session(target.engine) as db:
                ordinary_row = db.get_one(BlindTask, ident)
                initial_row = db.get_one(BlindInitial, ident)
                exposure_row = db.get_one(BlindExposure, ident)
                labels_row = db.get_one(BlindLabels, ident)
                _check(
                    ordinary_row.created_at
                    <= initial_row.created_at
                    <= exposure_row.created_at
                    <= labels_row.created_at
                    and initial_row.sha256
                    == digest([ordinary_row.input_sha256, initial_row.payload])
                    and exposure_row.initial_sha256 == initial_row.sha256
                    and exposure_row.candidates_sha256 == digest(ordinary_row.candidates)
                    and exposure_row.sha256
                    == digest([ident, initial_row.sha256, exposure_row.candidates_sha256])
                    and labels_row.exposure_sha256 == exposure_row.sha256
                    and labels_row.sha256 == digest([exposure_row.sha256, labels_row.payload])
                    and {item["decision"] for item in labels_row.payload["items"]}
                    == {"UNDETERMINED"},
                    "P5_REGISTRATION_STAGE_CHAIN_INVALID",
                )
                mutations = list(
                    db.scalars(
                        select(EvaluationMutation).where(EvaluationMutation.object_id == ident)
                    )
                )
                _check(
                    {item.scope for item in mutations}
                    == {f"chat-blind:{stage}:{ident}" for stage in ("initial", "reveal", "labels")}
                    and len(mutations) == 3
                    and all(item.actor_id == actor.user_id for item in mutations),
                    "P5_REGISTRATION_MUTATIONS_NOT_SCOPED",
                )

        after, current_objects = state(target)
        changed = {
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        }
        _check(
            changed == allowed.keys()
            and all(
                after[name].rows == before[name].rows + added for name, added in allowed.items()
            )
            and original_objects == current_objects
            and prior_rows({task["id"] for task in tasks.values()}) == historical
            and service.get(actor, tasks[CASES[1]]["id"])["status"] == "BLIND",
            "P5_REGISTRATION_UNEXPECTED_STATE_CHANGE",
        )
    return {
        "tasks": tasks,
        "allowed_table_deltas": allowed,
        "source_and_arm_tables_unchanged": True,
        "source_and_arm_objects_read_back_unchanged": len(current_objects),
        "no_model_or_financial_writes": True,
        "evidence_grade": "CONTROLLED_REGISTRATION_NO_INDEPENDENT_LABELS",
    }


def _execute(plan, state, stack):
    from unittest.mock import patch

    import offline_demo
    from verify_chat_p5_pg import STATE

    verifier = f"aftercare-fresh-{stack.run_id}-p5-registration-verifier"
    _check(stack.owned("container", verifier) is None, "P5_REGISTRATION_VERIFIER_ALREADY_PRESENT")
    state["resources"].append(["container", verifier])
    with patch.object(offline_demo, "STATE", STATE):
        offline_demo.save(state)
    try:
        result = stack.docker(
            "P5_REGISTRATION_CONTROLLED_WORKER",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            verifier,
            "--label",
            f"com.aftercare.fresh-bootstrap={stack.run_id}",
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
            f"type=bind,source={SCRIPT},target=/registration.py,readonly",
            "--mount",
            f"type=bind,source={plan['manifest_directory']},target=/paired-manifest",
            "--entrypoint",
            "python",
            plan["verifier_image"],
            "/registration.py",
            "_worker",
            json.dumps(plan),
            timeout=300,
            required=False,
            log_output=False,
        )
        _check(result is not None, "P5_REGISTRATION_WORKER_FAILED")
        if result.returncode != 0:
            lines = result.stderr.splitlines()
            code = lines[-1] if lines else ""
            raise ValueError(
                code
                if re.fullmatch(r"P5_REGISTRATION_[A-Z0-9_]+", code)
                else "P5_REGISTRATION_WORKER_FAILED"
            )
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        owned = stack.owned("container", verifier)
        _check(owned is not False, "P5_REGISTRATION_VERIFIER_OWNER_MISMATCH")
        if owned:
            stack.docker("P5_REGISTRATION_REMOVE_VERIFIER", "rm", "-f", verifier)
        _check(stack.owned("container", verifier) is None, "P5_REGISTRATION_VERIFIER_NOT_REMOVED")
        state["resources"].remove(["container", verifier])
        with patch.object(offline_demo, "STATE", STATE):
            offline_demo.save(state)


def main():
    from aftercare.business.rules import digest
    from offline_demo import lifecycle_lock, status
    from verify_chat_p5_pg import STATE
    from verify_fresh_bootstrap import VerificationError
    from verify_streaming_compatibility import _save

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovered-report", type=Path, required=True)
    parser.add_argument("--verifier-image-id", required=True)
    parser.add_argument("--exercise-ordinary", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.verifier_image_id != VERIFIER_IMAGE:
        parser.error("supply the pinned registration verifier image ID")
    if args.execute and (args.report is None or not args.expected_plan_sha256):
        parser.error("--execute requires --expected-plan-sha256 and a new --report")
    with lifecycle_lock(STATE, shared=not args.execute):
        plan, state, stack = _prepare(
            args.recovered_report, args.verifier_image_id, args.exercise_ordinary
        )
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
                )
            )
            return 0
        _check(checksum == args.expected_plan_sha256, "P5_REGISTRATION_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "REGISTRATION",
            "plan_sha256": checksum,
            "plan": plan,
        }
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            report["acceptance"] = _execute(plan, state, stack)
            _check(
                _journal_hashes(Path(plan["manifest_directory"])) == plan["journal_sha256"]
                and _hash(args.recovered_report) == plan["recovered_report_sha256"]
                and status(state)["healthy_now"],
                "P5_REGISTRATION_PRIOR_EVIDENCE_CHANGED",
            )
            report.update(status="PASSED", stage="VERIFIED")
        except (OSError, ValueError, VerificationError, TypeError, KeyError) as error:
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
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        try:
            print(json.dumps(_worker(json.loads(sys.argv[2]))))
        except Exception as error:  # noqa: BLE001 - the host must not receive credentials or DB errors
            code = getattr(error, "code", str(error))
            print(
                code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__, file=sys.stderr
            )
            raise SystemExit(1) from None
    else:
        try:
            raise SystemExit(main())
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            print("P5_REGISTRATION_PREFLIGHT_BLOCKED", file=sys.stderr)
            raise SystemExit(1) from None
