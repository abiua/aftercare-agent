"""Prepare a controlled 5201 native PostgreSQL acceptance for option-level blind labels."""

import argparse
import json
import os
import re
import sys
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import offline_demo
import verify_chat_p5_pg as pg
import verify_material_review_migration as migration
from verify_streaming_compatibility import _private_json, _save

CAMPAIGN = "chat-p5-pg-option-labels-v2-control"
STACK_ID = "315cfced887148e3b4221b777887e929"
SYSTEM_ID = "7706834943065174055"
_FILES = (
    *pg.FILES,
    "scripts/verify_chat_p5_option_labels.py",
    "scripts/offline_demo.py",
    "scripts/setup_runtime.py",
    "scripts/verify_fresh_bootstrap.py",
    "scripts/verify_material_review_migration.py",
    "scripts/verify_streaming_compatibility.py",
)
_ALLOWED = {
    "public.eval_chat_blind_tasks": 2,
    "public.eval_chat_blind_initials": 2,
    "public.eval_chat_blind_exposures": 2,
    "public.eval_chat_blind_labels": 2,
    "public.eval_mutations": 6,
}


def _check(condition, suffix):
    if not condition:
        raise ValueError(f"P5_OPTION_LABELS_{suffix}")


def _plan(backend, frontend, sentinel):
    state = _private_json(migration.STATE)
    stack = offline_demo.restore(state)
    endpoint = stack.docker(
        "P5_OPTION_DOCKER_CONTEXT",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        log_output=False,
    ).stdout.strip()
    _check(
        state["run_id"] == stack.run_id == STACK_ID
        and state["profile"] == "test"
        and state["status"] == "READY"
        and state["url"] == migration.URL
        and not (stack.directory / CAMPAIGN).exists()
        and endpoint.startswith("unix://")
        and (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")),
        "OWNED_STACK_REQUIRED",
    )
    network, system_id = migration._topology(stack, backend, frontend)
    _check(system_id == SYSTEM_ID, "OWNED_SYSTEM_ID_REQUIRED")
    return {
        "campaign": CAMPAIGN,
        "stack_id": stack.run_id,
        "system_id": system_id,
        "backend": backend,
        "frontend": frontend,
        "network": network,
        "sentinel_sha256": sentinel,
        "evidence_grade": "CONTROL",
        "files": {name: sha256((pg.ROOT / name).read_bytes()).hexdigest() for name in _FILES},
    }


def _label_body(candidates, *, v2):
    body = {
        "expected_version": 3,
        "items": [{"candidate_id": item["id"], "decision": "UNDETERMINED"} for item in candidates],
    }
    if v2:
        body.update(
            label_schema_version="chat-blind-labels-v2",
            option_labels=[
                {"candidate_id": item["id"], "option_id": option["id"], "decision": "UNDETERMINED"}
                for item in candidates
                if item["status"] == "AVAILABLE"
                for option in item["options"]
            ],
        )
    return body


def _exercise(service, actor, ident, *, v2):
    from aftercare.business.errors import BusinessError
    from aftercare.evaluation.chat.contracts import InitialJudgment, PostOptionLabelsV2, Versioned

    def write(stage, body):
        return service.write(actor, ident, stage, body, f"p5-option-labels-{stage}-{ident}")

    before = service.get(actor, ident)
    _check(
        before["status"] == "BLIND"
        and before["initial"] is before["exposure"] is before["labels"] is None,
        "TASK_NOT_BLIND",
    )
    try:
        write("reveal", Versioned(expected_version=1))
    except BusinessError as error:
        _check(error.code == "BLIND_REVIEW_STAGE_CHANGED", "EARLY_REVEAL")
    else:
        _check(False, "EARLY_REVEAL")
    sealed = write(
        "initial",
        InitialJudgment(
            expected_version=1, judgment="UNDETERMINED", notes="受控验收，无独立人工标签。"
        ),
    )
    _check(sealed["status"] == "SEALED" and sealed["exposure"] is None, "SEAL")
    revealed = write("reveal", Versioned(expected_version=2))
    _check(revealed["status"] == "REVEALED", "REVEAL")
    candidates = revealed["exposure"]["candidates"]
    if v2:
        label = PostOptionLabelsV2.model_validate(_label_body(candidates, v2=True))
    else:
        from aftercare.evaluation.chat.contracts import PostLabels

        label = PostLabels.model_validate(_label_body(candidates, v2=False))
    result = write("labels", label)
    _check(
        result["status"] == "LABELED"
        and write("labels", label) == result
        and service.get(actor, ident) == result,
        "LABEL_REPLAY_READBACK",
    )
    return result


def _worker(plan, directory, expected):
    from dataclasses import replace
    from secrets import compare_digest

    from aftercare import backup
    from aftercare.auth import UserContext, token_hash
    from aftercare.business.errors import BusinessError
    from aftercare.business.rules import digest
    from aftercare.business.storage import S3ObjectStore, verify_object
    from aftercare.config import load_settings
    from aftercare.db import User
    from aftercare.decisions.tables import DecisionSnapshot
    from aftercare.evaluation.chat.gate_accuracy import (
        SealedReview,
        VerifiedLabelSource,
        _sealed,
        summarize_gate_accuracy,
    )
    from aftercare.evaluation.chat.manifest import Manifest, bound_rows
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.registration import register_pg_blind
    from aftercare.evaluation.chat.runner import ChatEvaluationRunner
    from aftercare.evaluation.chat.service import BlindReviewService
    from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindLabels, BlindTask
    from aftercare.intelligence.models import ModelRunner, Registry
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    _check(
        digest(plan) == expected
        and plan["campaign"] == CAMPAIGN
        and plan["stack_id"] == STACK_ID
        and plan["system_id"] == SYSTEM_ID,
        "PLAN_MISMATCH",
    )
    staged = Path("/acceptance")
    for file, checksum in plan["files"].items():
        _check(sha256((staged / file).read_bytes()).hexdigest() == checksum, "FILE_CHANGED")
    sys.path.insert(0, str(staged / "backend/tests/business"))
    from chat_eval_development_fixtures import load_development_cases, replay_development_case

    settings = load_settings("test").model_copy(
        update={
            "chat_investigation_enabled": True,
            "chat_investigation_protocol": "compact-v6",
            "chat_analysis_enabled": False,
        }
    )
    store = S3ObjectStore(settings)
    marker = store.get(sentinel_key(plan["stack_id"]), 64)
    verify_object(marker, 64, plan["sentinel_sha256"])
    attestation = PgAttestation(
        plan["stack_id"],
        plan["system_id"],
        store.bucket,
        plan["sentinel_sha256"],
        64,
    )
    pack = load_development_cases(staged)
    registry = Registry.model_validate(
        {
            "version": "chat-p5-option-labels-controlled",
            "allow_unverified": True,
            "models": [
                {
                    "alias": "analysis",
                    "provider": "fixture",
                    "model": "controlled",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "1",
                    "price_date": "2026-11-11",
                    "source": "synthetic",
                    "context_tokens": 32000,
                    "data_scopes": ["customer_conversation"],
                    "output_tokens": 1800,
                }
            ],
        }
    )
    calls = []
    runner = ChatEvaluationRunner(
        ModelRunner(registry, transport=pg._controlled_transport(calls)),
        None,
        calls=8,
        budget=Decimal("0.10"),
    )
    manifest = Manifest(directory / "manifest")
    details, bundles = {}, {}

    def fingerprints(target):
        with target.engine.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            return backup._fingerprints(db), backup._materials(db)

    def historical_blind_rows(target, excluded):
        result = {}
        with target.engine.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            for qualified in _ALLOWED:
                schema, table = qualified.split(".")
                relation = backup._qualified(schema, table)
                key = (
                    "object_id"
                    if table == "eval_mutations"
                    else "id"
                    if table == "eval_chat_blind_tasks"
                    else "task_id"
                )
                records = db.execute(text(f"SELECT {key}, to_jsonb(t) FROM {relation} t"))
                result[qualified] = sorted(
                    digest(row) for ident, row in records if ident not in excluded
                )
        return result

    with open_pg_runtime(settings, attestation, CAMPAIGN) as runtime:
        target = runtime.registration_business()
        tester = [
            item
            for item in _private_json(Path("/workspace/.runtime/users-test.json"))
            if item["role"] == "tester"
        ]
        _check(len(tester) == 1, "EXPECTED_SINGLE_TESTER")
        tester = tester[0]
        actor = UserContext(user_id=tester["user_id"], role="tester")
        with Session(target.engine) as db:
            user = db.get_one(User, actor.user_id)
            _check(
                user.enabled
                and user.role == "tester"
                and compare_digest(user.token_hash, token_hash(tester["token"])),
                "TESTER_BINDING",
            )
        original, original_objects = fingerprints(target)
        cases = []
        for case_id in pg.CASES:
            definition = next(item for item in pack["cases"] if item["id"] == case_id)
            with runtime.source(case_id, 1, digest([pack, definition])) as (engine, storage, fresh):
                _check(fresh, "SOURCE_EXISTS")
                cases.append(
                    replay_development_case(
                        engine,
                        storage,
                        runtime.settings,
                        staged,
                        case_id,
                        fresh=True,
                    )
                )
        receipt = runtime.run(manifest, runner, cases)
        _check(
            len(receipt["rows"]) == 6
            and all(
                row["candidate_status"] == "AVAILABLE" and row["checkpoint_persisted"]
                for row in receipt["rows"]
            ),
            "NATIVE_CANDIDATES",
        )
        before, objects_before = fingerprints(target)
        _check(
            {key: value for key, value in before.items() if key.startswith("public.")}
            == {key: value for key, value in original.items() if key.startswith("public.")}
            and all(item in objects_before for item in original_objects),
            "PUBLIC_CHANGED_BY_WORKER",
        )
        frozen = manifest.read("plan.json")
        rows = {row["id"]: row for row in bound_rows(frozen)}
        historical = historical_blind_rows(target, set())
        service = BlindReviewService(target)
        for index, case in enumerate(cases):
            v2 = index == 0
            task = register_pg_blind(
                runtime,
                manifest,
                case,
                reviewer_id=actor.user_id,
                case_label=case.case_id,
            )
            ident = task["id"]
            try:
                service.get(UserContext(user_id="support", role="support"), ident)
            except BusinessError as error:
                _check(error.status == 404, "FOREIGN_ROLE")
            else:
                _check(False, "FOREIGN_ROLE")
            detail = _exercise(service, actor, ident, v2=v2)
            for image in detail["input"]["images"]:
                raw, media = service.image(actor, ident, image["id"])
                verify_object(raw, image["size"], image["sha256"])
                _check(media == image["content_type"], "IMAGE_CONTENT_TYPE")
            binding = manifest.read(f"blind-{ident}.json")
            _check(
                binding["option_gate_schema_version"] == "chat-blind-option-gates-v1"
                and binding["option_gates_sha256"] == digest(binding["arms"]),
                "NATIVE_GATE_MAPPING",
            )
            source = runtime.freeze(case)
            candidates = {
                candidate["id"]: candidate for candidate in detail["exposure"]["candidates"]
            }
            for arm in binding["arms"]:
                row = rows[arm["row_id"]]
                gate = arm["option_gate"]
                with (
                    runtime.arm(manifest, row, source, create=False) as (engine, _, _, _),
                    Session(engine) as db,
                    db.begin(),
                ):
                    db.execute(text("SET TRANSACTION READ ONLY"))
                    snapshot = db.get_one(DecisionSnapshot, arm["snapshot_id"])
                    expected_gates = {
                        digest([snapshot.id, option["id"]])[:32]: (
                            option["id"],
                            snapshot.checks[option["id"]]["status"],
                        )
                        for option in snapshot.candidate["options"]
                    }
                    observed_gates = {
                        option["option_id"]: (option["native_option_id"], option["status"])
                        for option in gate["options"]
                    }
                    exposed = candidates[arm["blind_candidate_id"]]["options"]
                    _check(
                        gate["snapshot_sha256"] == arm["snapshot_sha256"] == snapshot.sha256
                        and gate["checks_sha256"] == digest(snapshot.checks)
                        and observed_gates == expected_gates
                        and len(observed_gates) == len(gate["options"])
                        and observed_gates.keys() == {option["id"] for option in exposed},
                        "NATIVE_GATE_READBACK",
                    )
            with Session(target.engine) as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                native_task = db.get_one(BlindTask, ident)
                native_initial = db.get_one(BlindInitial, ident)
                native_exposure = db.get_one(BlindExposure, ident)
                native_labels = db.get_one(BlindLabels, ident)
                _check(
                    native_initial.created_at
                    <= native_exposure.created_at
                    <= native_labels.created_at
                    and native_task.source_sha256 == binding["source_sha256"]
                    and native_initial.sha256
                    == digest([native_task.input_sha256, native_initial.payload])
                    and native_exposure.initial_sha256 == native_initial.sha256
                    and native_exposure.candidates_sha256 == digest(native_task.candidates)
                    and native_exposure.sha256
                    == digest([ident, native_initial.sha256, native_exposure.candidates_sha256])
                    and native_labels.exposure_sha256 == native_exposure.sha256
                    and native_labels.sha256
                    == digest([native_exposure.sha256, native_labels.payload]),
                    "NATIVE_CHAIN",
                )
                review = SealedReview(
                    task_id=ident,
                    input=native_task.input,
                    images=native_task.image_objects,
                    candidates=native_task.candidates,
                    initial=detail["initial"],
                    exposure={
                        **detail["exposure"],
                        "initial_sha256": native_exposure.initial_sha256,
                        "candidates_sha256": native_exposure.candidates_sha256,
                    },
                    labels={**detail["labels"], "exposure_sha256": native_labels.exposure_sha256},
                    registration=binding,
                    source=VerifiedLabelSource(
                        ident,
                        native_labels.sha256,
                        expected,
                        "CONTROL",
                    ),
                    registration_sha256=digest(binding),
                )
                for arm in binding["arms"]:
                    row = rows[arm["row_id"]]
                    seal = (digest(frozen), digest(frozen["controls"]), row["input_sha256"])
                    _check(
                        _sealed(review, *seal)
                        and not _sealed(
                            replace(
                                review, exposure={**review.exposure, "initial_sha256": "0" * 64}
                            ),
                            *seal,
                        ),
                        "EXPORT_SEAL_READBACK",
                    )
                    bundles[row["id"]] = review
                payload = native_labels.payload
                _check(
                    (payload.get("label_schema_version") == "chat-blind-labels-v2") == v2
                    and bool(payload.get("option_labels")) == v2,
                    "PERSISTED_LABEL_SCHEMA",
                )
                details[case.case_id] = {
                    "task_id": ident,
                    "status": detail["status"],
                    "label_version": "V2" if v2 else "V1",
                    "labels_sha256": native_labels.sha256,
                    "option_labels": len(payload.get("option_labels", [])),
                    "registered_native_gates": sum(
                        len(arm["option_gate"]["options"]) for arm in binding["arms"]
                    ),
                }
        export = summarize_gate_accuracy(frozen, digest(frozen), bundles)
        _check(
            len(export["rows"]) == 6
            and all(row["exclusion"] == "CONTROL" for row in export["rows"]),
            "CONTROL_NOT_EXCLUDED",
        )
        after, objects_after = fingerprints(target)
        _check(
            {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            == _ALLOWED.keys()
            and all(
                after[name].rows == before[name].rows + delta for name, delta in _ALLOWED.items()
            )
            and objects_before == objects_after
            and historical_blind_rows(target, {task["task_id"] for task in details.values()})
            == historical,
            "UNEXPECTED_STATE_CHANGE",
        )
    _save(directory / "gate-accuracy-control.json", export)
    return {
        "tasks": details,
        "planned_rows": len(export["rows"]),
        "controlled_transport_calls": len(calls),
        "provider_calls": 0,
        "independent_human_labels": 0,
        "gate_accuracy_export_sha256": digest(export),
        "program_gate_accuracy": "NOT_MEASURABLE",
        "boundary": "NATIVE_BLIND_REVIEW_SERVICE_AND_POSTGRES_WRITES",
        "http_submission": "NOT_TESTED",
        "public_business_unchanged": True,
        "original_bytes_read_back": True,
        "existing_testers": 1,
        "second_tester_isolation": "NOT_TESTED",
        "non_tester_access_denied": True,
    }


def _execute(plan, checksum, report):
    state = _private_json(migration.STATE)
    stack = offline_demo.restore(state)
    _check(
        migration._topology(stack, plan["backend"], plan["frontend"])
        == (plan["network"], plan["system_id"]),
        "TOPOLOGY_CHANGED",
    )
    root = stack.directory / CAMPAIGN
    root.mkdir(mode=0o700)
    staged, output = root / "input", root / "output"
    staged.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    for name, digest in plan["files"].items():
        content = (pg.ROOT / name).read_bytes()
        _check(sha256(content).hexdigest() == digest, "FILE_CHANGED")
        dest = staged / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(0o400)
    name = f"aftercare-fresh-{stack.run_id}-p5-option-labels"
    _check(stack.owned("container", name) is None, "VERIFIER_EXISTS")
    state["resources"].append(["container", name])
    with patch.object(offline_demo, "STATE", migration.STATE):
        offline_demo.save(state)
    report["private_output_directory"] = str(output)
    try:
        completed = stack.docker(
            "P5_OPTION_LABELS_WORKER",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
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
            f"type=bind,source={staged},target=/acceptance,readonly",
            "--mount",
            f"type=bind,source={output},target=/output",
            "--entrypoint",
            "python",
            plan["backend"],
            "/acceptance/scripts/verify_chat_p5_option_labels.py",
            "_worker",
            checksum,
            json.dumps(plan),
            required=False,
            log_output=False,
            timeout=300,
        )
        if completed is not None and completed.returncode != 0:
            for line in reversed(completed.stderr.splitlines()):
                if re.fullmatch(r"P5_OPTION_LABELS_[A-Z0-9_]+", line):
                    report["worker_error_code"] = line
                    break
        _check(completed is not None and completed.returncode == 0, "WORKER_FAILED")
        report["acceptance"] = json.loads(completed.stdout.strip().splitlines()[-1])
    finally:
        owned = stack.owned("container", name)
        _check(owned is not False, "VERIFIER_OWNER_MISMATCH")
        if owned:
            stack.docker("P5_OPTION_LABELS_REMOVE", "rm", "-f", name)
        _check(stack.owned("container", name) is None, "VERIFIER_NOT_REMOVED")
        state["resources"].remove(["container", name])
        with patch.object(offline_demo, "STATE", migration.STATE):
            offline_demo.save(state)


def _safe(error):
    code = str(error)
    return code if re.fullmatch(r"P5_OPTION_LABELS_[A-Z0-9_]+", code) else type(error).__name__


def main():
    from aftercare.business.rules import digest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--sentinel-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not all(
        re.fullmatch(r"sha256:[a-f0-9]{64}", value)
        for value in (args.backend_image_id, args.frontend_image_id)
    ) or not re.fullmatch(r"[a-f0-9]{64}", args.sentinel_sha256):
        parser.error("supply full immutable image IDs and the existing sentinel SHA-256")
    if args.execute and not (args.expected_plan_sha256 and args.report):
        parser.error("--execute requires a reviewed plan hash and a new report")

    def prepare():
        return _plan(args.backend_image_id, args.frontend_image_id, args.sentinel_sha256)

    plan = prepare()
    checksum = digest(plan)
    if not args.execute:
        print(json.dumps({"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}))
        return 0
    _check(checksum == args.expected_plan_sha256, "PLAN_CHANGED")
    report = {"status": "IN_PROGRESS", "plan": plan, "plan_sha256": checksum}
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
    try:
        with offline_demo.lifecycle_lock(migration.STATE):
            _check(prepare() == plan, "PLAN_CHANGED")
            _execute(plan, checksum, report)
        report["status"] = "PASSED"
    except Exception as error:  # noqa: BLE001
        report.update(status="FAILED", failure_code=_safe(error))
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        try:
            print(json.dumps(_worker(json.loads(sys.argv[3]), Path("/output"), sys.argv[2])))
        except Exception as error:  # noqa: BLE001
            print(_safe(error), file=sys.stderr)
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())
