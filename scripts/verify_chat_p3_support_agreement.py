"""Read two old P3 agreement records through an isolated, temporary offline verifier."""

import argparse
import inspect
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import offline_demo
from aftercare.business.rules import digest
from verify_chat_backup_restore import private_json
from verify_fresh_bootstrap import ROOT, VerificationError, require
from verify_material_review_migration import _docker, _hash
from verify_streaming_compatibility import _save

STATE = ROOT / ".runtime/offline-browser-demo-p3-progress.json"
HISTORY = ROOT / "reports/chat-p3-progress-20261111.json"
HISTORY_SHA = "9507ef05db1ffb3f7007ecc4714010f7d030f40993a1ea9a187473174b4c22ec"
STACK = "199fdc29bfdc4fdaa92c751fcbc0afae"
URL = "http://127.0.0.1:5194"
ROLES = ("pg", "objects", "redis", "api", "worker", "beat", "frontend")


def _inspect(stack, kind, name, field):
    require(
        (kind, name) in stack.resources and stack.owned(kind, name) is True,
        "P3_AGREEMENT_RESOURCE_UNOWNED",
    )
    return json.loads(
        stack.docker(
            "P3_AGREEMENT_INSPECT",
            kind,
            "inspect",
            "--format",
            "{{json ." + field + "}}",
            name,
            log_output=False,
        ).stdout
    )


def _stopped(stack):
    return all(
        _inspect(stack, "container", f"{stack.prefix}-{role}", "State.Status") == "exited"
        for role in ROLES
    )


def _plan(image, cutoff):
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "P3_AGREEMENT_LOCAL_DOCKER_REQUIRED",
    )
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
        "P3_AGREEMENT_VERIFIER_IMAGE_PIN_REQUIRED",
    )
    state = private_json(STATE)
    historical = private_json(HISTORY)
    require(
        state["run_id"] == STACK
        and state["url"] == URL
        and state.get("profile", "app") == "app"
        and historical["status"] == "PASSED"
        and historical["isolated_url"] == URL
        and historical["plan_sha256"] == digest(historical["plan"])
        and _hash(HISTORY) == HISTORY_SHA,
        "P3_AGREEMENT_HISTORY_MISMATCH",
    )
    stack = offline_demo.restore(state)
    network = f"{stack.prefix}-net"
    pg = f"{stack.prefix}-pg"
    volume = f"{stack.prefix}-pg-data"
    require(
        _inspect(stack, "network", network, "Internal") is True
        and set(_inspect(stack, "container", pg, "NetworkSettings.Networks")) == {network}
        and _inspect(stack, "volume", volume, "Name") == volume
        and any(
            row.get("Name") == volume and row["Destination"] == "/var/lib/postgresql/data"
            for row in _inspect(stack, "container", pg, "Mounts")
        )
        and _stopped(stack),
        "P3_AGREEMENT_OLD_STACK_NOT_STOPPED",
    )
    targets = {}
    for name in ("complete", "withdrawal"):
        row = historical["scenarios"][name]
        targets[name] = {
            key: str(UUID(row[key]))
            for key in (
                "original_snapshot_id",
                "support_snapshot_id",
                "conversation_id",
                "case_id",
                "run_id",
            )
        }
    return {
        "boundary": "AUTOMATED_CONTROL",
        "source": "OWNED_P3_HISTORICAL_NATIVE_RECORDS",
        "stack_id": STACK,
        "url": URL,
        "source_profile": "app",
        "state_sha256": _hash(STATE),
        "history_sha256": HISTORY_SHA,
        "script_sha256": _hash(Path(__file__)),
        "verifier_image": image,
        "network": network,
        "pg_container": pg,
        "pg_container_id": _inspect(stack, "container", pg, "Id"),
        "pg_volume": volume,
        "system_identifier_binding": "FIRST_OBSERVED_FROM_OWNED_PG_AND_RECHECKED_BY_VERIFIER",
        "cutoff": cutoff.isoformat(),
        "option_policy": "FIRST_NATIVE_CANDIDATE_OPTION_IN_STORED_ORDER",
        "targets": targets,
        "provider_calls_allowed": 0,
        "business_writes_allowed": 0,
    }, stack


def _extract(plan, system_id):
    from aftercare.business.rules import digest
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.decisions.contracts import DecisionRef
    from aftercare.evaluation.chat.scoring import PlannedRow
    from aftercare.evaluation.chat.support_agreement_export import (
        ExportTarget,
        export_support_agreement,
    )
    from sqlalchemy import text

    settings = load_settings("app")
    if not settings.container or any(
        (settings.model_enabled, settings.chat_vision_enabled, settings.embedding_enabled)
    ):
        raise ValueError("P3_AGREEMENT_OFFLINE_REQUIRED")
    engine = guarded_engine(settings).execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    )

    def audit():
        with engine.begin() as db:
            identity = db.execute(
                text(
                    "SELECT current_database(),current_user,session_user,current_setting('cluster_name'),"
                    "system_identifier::text,current_setting('transaction_read_only') FROM pg_control_system()"
                )
            ).one()
            if tuple(identity) != (
                "ac_app",
                "ac_app",
                "ac_app",
                "aftercare-v2-app",
                system_id,
                "on",
            ):
                raise ValueError("P3_AGREEMENT_DATABASE_IDENTITY_MISMATCH")
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            values = {}
            for table in (
                "chat_decision_snapshots",
                "chat_support_decisions",
                "business_approvals",
                "business_executions",
            ):
                hashes = sorted(
                    digest(row)
                    for row in db.scalars(text(f"SELECT to_jsonb(t) FROM public.{table} t"))
                )
                values[table] = {"rows": len(hashes), "sha256": digest(hashes)}
            return values

    try:
        before = audit()
        cutoff = datetime.fromisoformat(plan["cutoff"])
        targets = []
        selected = {}
        with engine.begin() as db:
            for name, target in plan["targets"].items():
                native = (
                    db.execute(
                        text(
                            "SELECT version,sha256,conversation_id,run_id,origin,input->>'case_id' AS case_id,"
                            "candidate->'options'->0->>'id' AS option_id "
                            "FROM public.chat_decision_snapshots WHERE id=:id"
                        ),
                        {"id": target["original_snapshot_id"]},
                    )
                    .mappings()
                    .one()
                )
                if (
                    native["conversation_id"],
                    native["run_id"],
                    native["origin"],
                    native["case_id"],
                ) != (
                    target["conversation_id"],
                    target["run_id"],
                    "AGENT",
                    target["case_id"],
                ) or not native["option_id"]:
                    raise ValueError("P3_AGREEMENT_NATIVE_CANDIDATE_MISMATCH")
                ref = DecisionRef(
                    id=target["original_snapshot_id"],
                    version=native["version"],
                    sha256=native["sha256"],
                )
                selected[name] = {
                    "candidate": ref.model_dump(mode="json"),
                    "option_id": native["option_id"],
                }
                targets.append(
                    ExportTarget(PlannedRow(name, 1, "legacy_p3"), ref, native["option_id"], cutoff)
                )
        exported = export_support_agreement(engine, tuple(targets))
        after = audit()
        if after != before:
            raise ValueError("P3_AGREEMENT_NATIVE_ROWS_CHANGED")
        exclusions = []
        for entry in exported["extraction"]:
            target = plan["targets"][entry["row"].case_id]
            if (
                entry["exclusion"]
                or entry["result"] is None
                or entry["result"].id != target["support_snapshot_id"]
            ):
                exclusions.append(entry["row"].case_id)
        return {
            "scope_system_identifier": system_id,
            "before": before,
            "after": after,
            "selected_options": selected,
            "extraction_exclusions": exclusions,
            "agreement": exported,
        }
    finally:
        engine.dispose()


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def _execute(stack, plan, report):
    pg = plan["pg_container"]
    worker = f"{stack.prefix}-agreement-verifier"
    require(
        stack.owned("container", worker) is None
        and _stopped(stack)
        and _inspect(stack, "container", pg, "Id") == plan["pg_container_id"],
        "P3_AGREEMENT_OLD_STACK_CHANGED",
    )
    try:
        stack.docker("P3_AGREEMENT_START_OWN_PG", "start", pg, log_output=False)
        stack.wait(
            "P3_AGREEMENT_PG_READY",
            "exec",
            pg,
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "ac_app",
            "-d",
            "ac_app",
        )
        attached = _inspect(stack, "network", plan["network"], "Containers")
        require(
            {item["Name"] for item in attached.values()} == {pg},
            "P3_AGREEMENT_INTERNAL_NETWORK_NOT_EXCLUSIVE",
        )
        system_id = stack.docker(
            "P3_AGREEMENT_SYSTEM_IDENTIFIER",
            "exec",
            pg,
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" PGOPTIONS="-c default_transaction_read_only=on" '
            "psql -h 127.0.0.1 -U ac_app -d ac_app -XAt -v ON_ERROR_STOP=1 "
            '-c "SELECT system_identifier::text FROM pg_control_system()"',
            log_output=False,
        ).stdout.strip()
        require(re.fullmatch(r"[0-9]{10,25}", system_id), "P3_AGREEMENT_SYSTEM_IDENTIFIER_INVALID")
        code = (
            "import json, re, sys\nfrom dataclasses import asdict, is_dataclass\n"
            "from datetime import datetime\n"
            + inspect.getsource(_extract)
            + inspect.getsource(_json_default)
            + "\ntry:\n print(json.dumps(_extract(json.loads(sys.argv[1]),sys.argv[2]),default=_json_default))\n"
            "except Exception as error:\n"
            " code = str(error)\n"
            " print('P3_WORKER_DIAGNOSTIC:' + (code if re.fullmatch(r'P3_AGREEMENT_[A-Z0-9_]+', code) else type(error).__name__),file=sys.stderr)\n"
            " raise SystemExit(1) from None\n"
        )
        result = stack.docker(
            "P3_AGREEMENT_READ_ONLY_VERIFIER",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            worker,
            "--label",
            stack.label,
            "--network",
            plan["network"],
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=app",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--mount",
            f"type=bind,source={stack.runtime / 'local.json'},target=/workspace/.runtime/local.json,readonly",
            "--entrypoint",
            "python",
            plan["verifier_image"],
            "-c",
            code,
            json.dumps(plan),
            system_id,
            timeout=60,
            required=False,
            log_output=False,
        )
        if result is None or result.returncode:
            lines = result.stderr.strip().splitlines() if result else []
            last = lines[-1] if lines else ""
            match = re.fullmatch(
                r"P3_WORKER_DIAGNOSTIC:(P3_AGREEMENT_[A-Z0-9_]+|[A-Za-z][A-Za-z0-9]{0,63})",
                last,
            )
            report["worker_code"] = match[1] if match else "UNAVAILABLE"
            raise ValueError("P3_AGREEMENT_READ_ONLY_VERIFIER_FAILED")
        report.update(json.loads(result.stdout.strip().splitlines()[-1]))
        require(not report["extraction_exclusions"], "P3_AGREEMENT_EXPORT_EXCLUSIONS")
    finally:
        try:
            owner = stack.owned("container", worker)
            require(owner is not False, "P3_AGREEMENT_TEMP_VERIFIER_UNOWNED")
            if owner:
                stack.docker("P3_AGREEMENT_REMOVE_VERIFIER", "rm", "-f", worker, log_output=False)
            report["temporary_verifier_removed"] = stack.owned("container", worker) is None
        finally:
            if _inspect(stack, "container", pg, "State.Running"):
                stack.docker("P3_AGREEMENT_STOP_OWN_PG", "stop", pg, timeout=35, log_output=False)
            report["old_containers_stopped"] = _stopped(stack)
            report["historical_files_unchanged"] = (
                _hash(STATE) == plan["state_sha256"] and _hash(HISTORY) == HISTORY_SHA
            )
        require(
            all(
                report[key]
                for key in (
                    "temporary_verifier_removed",
                    "old_containers_stopped",
                    "historical_files_unchanged",
                )
            ),
            "P3_AGREEMENT_CLEANUP_UNVERIFIED",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verifier-image-id", required=True)
    parser.add_argument(
        "--cutoff", required=True, help="Predeclared cutoff with an explicit UTC offset"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.cutoff)
    require(cutoff.utcoffset() == UTC.utcoffset(None), "P3_AGREEMENT_EXPLICIT_UTC_CUTOFF_REQUIRED")
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    with (
        offline_demo.lifecycle_lock(STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", STATE),
    ):
        plan, stack = _plan(args.verifier_image_id, cutoff)
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}, indent=2
                )
            )
            return 0
        require(args.expected_plan_sha256 == checksum, "P3_AGREEMENT_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "boundary": "AUTOMATED_CONTROL",
            "plan": plan,
            "plan_sha256": checksum,
            "independent_human_accuracy_measured": False,
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _execute(stack, plan, report)
            report["status"] = "PASSED"
        except Exception as error:  # noqa: BLE001 - bound the report to safe failure codes
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
        raise SystemExit(main())
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "P3_AGREEMENT_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
