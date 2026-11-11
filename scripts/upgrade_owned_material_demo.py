"""Back up and verify the owned 5201 demo before replacing only its apps."""

import argparse
import json
import os
import re
from contextlib import contextmanager
from ipaddress import ip_network
from pathlib import Path
from unittest.mock import patch

import offline_demo
import verify_chat_backup_restore as br
from aftercare import backup
from aftercare.business.rules import digest
from aftercare.business.storage import S3ObjectStore, verify_object
from sqlalchemy import text
from verify_chat_p2_offline import owned_container
from verify_fresh_bootstrap import ROOT, VerificationError, private_write, require
from verify_material_review_migration import (
    NEW_BACKEND as OLD_BACKEND,
)
from verify_material_review_migration import (
    NEW_FRONTEND as OLD_FRONTEND,
)
from verify_material_review_migration import (
    NEW_REV,
    STATE,
    URL,
    _docker,
    _hash,
    _replace_apps,
    _topology,
)
from verify_streaming_compatibility import _save

STACK_ID = "315cfced887148e3b4221b777887e929"
SYSTEM_ID = "7706834943065174055"
IMAGE_FILE = ROOT / ".runtime/chat-blind-v2-images.json"
COMMIT = "b49f914dc6c0c2359f56ae8aaa21e1235f92e149"
OLD = {"new_backend": OLD_BACKEND, "new_frontend": OLD_FRONTEND}
ROLES = ("frontend", "beat", "worker", "api")
RESTORE_SUBNET = "10.253.251.0/28"


def _local():
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "OWNED_5201_LOCAL_DOCKER_REQUIRED",
    )


def _restore_subnet():
    candidate = ip_network(RESTORE_SUBNET)
    networks = _docker("network", "ls", "--no-trunc", "--format", "{{.ID}}").splitlines()
    require(bool(networks), "OWNED_5201_DOCKER_NETWORK_INVENTORY_EMPTY")
    configs = _docker("network", "inspect", "--format", "{{json .IPAM.Config}}", *networks)
    rows = configs.splitlines()
    require(len(rows) == len(networks), "OWNED_5201_DOCKER_NETWORK_INVENTORY_CHANGED")
    occupied = [
        ip_network(config["Subnet"], strict=False)
        for row in rows
        for config in (json.loads(row) or ())
        if config.get("Subnet")
    ]
    require(
        not any(
            candidate.version == subnet.version and candidate.overlaps(subnet)
            for subnet in occupied
        ),
        "OWNED_5201_RESTORE_SUBNET_OVERLAP",
    )
    return str(candidate)


def _images():
    images = br.private_json(IMAGE_FILE)
    require(
        images["commit"] == COMMIT and images["runtime_updated"] is False,
        "OWNED_5201_TARGET_IMAGE_RECEIPT_INVALID",
    )
    new = {"new_backend": images["backend"], "new_frontend": images["frontend"]}
    for image in {*OLD.values(), *new.values()}:
        require(
            re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "OWNED_5201_IMAGE_PIN_MISMATCH",
        )
    require(new != OLD, "OWNED_5201_NEW_IMAGES_REQUIRED")
    return new


def _identity(stack):
    pg = owned_container(stack, "pg")
    output = (
        stack.docker(
            "OWNED_5201_DB_IDENTITY",
            "exec",
            pg,
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" PGOPTIONS="-c default_transaction_read_only=on" '
            "psql -h 127.0.0.1 -U ac_test -d ac_test -XAt -v ON_ERROR_STOP=1 "
            '-c "SELECT current_database(),current_user,session_user,current_setting(\x27cluster_name\x27),'
            'system_identifier::text,(SELECT version_num FROM public.alembic_version) FROM pg_control_system()"',
            log_output=False,
        )
        .stdout.strip()
        .split("|")
    )
    require(
        output == ["ac_test", "ac_test", "ac_test", "aftercare-v2-test", SYSTEM_ID, NEW_REV],
        "OWNED_5201_DB_IDENTITY_OR_REVISION_CHANGED",
    )


def _infrastructure(stack):
    result = {}
    for kind, suffix in (
        ("container", "pg"),
        ("container", "objects"),
        ("container", "redis"),
        ("network", "net"),
        ("network", "ingress"),
        ("volume", "pg-data"),
        ("volume", "objects-data"),
    ):
        name = f"{stack.prefix}-{suffix}"
        require(
            (kind, name) in stack.resources and stack.owned(kind, name) is True,
            "OWNED_5201_INFRA_UNOWNED",
        )
        fields = "{{json .Id}}" if kind != "volume" else "{{json .Name}}"
        ident = json.loads(
            stack.docker(
                "OWNED_5201_INFRA_PIN", kind, "inspect", "--format", fields, name, log_output=False
            ).stdout
        )
        result[suffix] = ident
    return result


def _scope(state):
    require(
        state["run_id"] == STACK_ID and state["url"] == URL and state["profile"] == "test",
        "OWNED_5201_STATE_REJECTED",
    )
    return offline_demo.restore(state)


def _plan():
    _local()
    state = br.private_json(STATE)
    stack = _scope(state)
    require(
        state["status"] == "READY" and offline_demo.status(state)["healthy_now"],
        "OWNED_5201_SOURCE_NOT_READY",
    )
    network, system_id = _topology(stack, OLD_BACKEND, OLD_FRONTEND)
    require(
        system_id == SYSTEM_ID
        and (state["backend_image"], state["frontend_image"]) == (OLD_BACKEND, OLD_FRONTEND),
        "OWNED_5201_OLD_IMAGES_MISMATCH",
    )
    _identity(stack)
    return (
        {
            "boundary": "OWNED_5201_APP_ONLY_BLIND_V2",
            "owned_stack": STACK_ID,
            "owned_url": URL,
            "system_identifier": SYSTEM_ID,
            "network": network,
            "infra": _infrastructure(stack),
            "old_images": OLD,
            "new_images": _images(),
            "target_commit": COMMIT,
            "image_receipt_sha256": _hash(IMAGE_FILE),
            "old_state_sha256": _hash(STATE),
            "alembic_revision": NEW_REV,
            "runtime_settings_sha256": _hash(stack.runtime / "local.json"),
            "restore_subnet": _restore_subnet(),
            "script_sha256": _hash(Path(__file__)),
            "allowed_financial_writes": 0,
            "allowed_model_calls": 0,
        },
        state,
        stack,
    )


def _generic_readback(topology, target, archive, manifest, result, **_):
    source = topology.settings["source"]
    engine = backup.restored_engine(target, result["database"])
    try:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            require(
                backup._identity(db)[1] == topology.identities["target"][1]
                and backup._identity(db)[1] != topology.identities["source"][1]
                and backup._fingerprints(db) == manifest.database
                and backup._materials(db) == manifest.materials,
                "OWNED_5201_RESTORE_FINGERPRINT_MISMATCH",
            )
    finally:
        engine.dispose()
    require(
        br.source_fingerprints(topology, source) == manifest.database,
        "OWNED_5201_SOURCE_CHANGED_AFTER_BACKUP",
    )
    stores = (
        S3ObjectStore(source),
        S3ObjectStore(topology.restore_bucket(target, result["bucket"])),
    )
    for obj in manifest.materials:
        archived = (archive / backup.OBJECTS / obj.sha256).read_bytes()
        verify_object(archived, obj.size, obj.sha256)
        require(
            all(store.get(obj.key, obj.size) == archived for store in stores),
            "OWNED_5201_RESTORE_OBJECT_MISMATCH",
        )
    return {
        "all_managed_fingerprints_match": True,
        "managed_table_count": len(manifest.database),
        "all_source_archive_restore_object_bytes_match": True,
        "object_count": len(manifest.materials),
        "source_system_identifier": str(topology.identities["source"][1]),
        "restore_system_identifier": str(topology.identities["target"][1]),
    }


def _heads(stack, image):
    output = stack.docker(
        "OWNED_5201_IMAGE_ALEMBIC_HEAD",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "--entrypoint",
        "alembic",
        image,
        "heads",
        timeout=30,
        log_output=False,
    ).stdout.strip()
    require(output == f"{NEW_REV} (head)", "OWNED_5201_TARGET_ALEMBIC_INCOMPATIBLE")


def _start(stack, state, plan, images):
    _replace_apps(stack, state, plan["network"], images, start=True)
    require(
        _topology(stack, images["new_backend"], images["new_frontend"])
        == (plan["network"], SYSTEM_ID)
        and _infrastructure(stack) == plan["infra"]
        and offline_demo.status(state)["healthy_now"],
        "OWNED_5201_REPLACED_APPS_UNHEALTHY",
    )
    _identity(stack)
    require(
        _hash(stack.runtime / "local.json") == plan["runtime_settings_sha256"],
        "OWNED_5201_RUNTIME_SETTINGS_CHANGED",
    )


def _rollback(stack, state, plan):
    _identity(stack)
    require(_infrastructure(stack) == plan["infra"], "OWNED_5201_ROLLBACK_INFRA_CHANGED")
    for role in ROLES:
        name = f"{stack.prefix}-{role}"
        owner = stack.owned("container", name)
        require(owner is not False, "OWNED_5201_ROLLBACK_APP_UNOWNED")
        if owner:
            image = stack.docker(
                "OWNED_5201_ROLLBACK_APP_PIN",
                "inspect",
                "--format",
                "{{.Image}}",
                name,
                log_output=False,
            ).stdout.strip()
            key = "new_frontend" if role == "frontend" else "new_backend"
            require(
                image in {plan["old_images"][key], plan["new_images"][key]},
                "OWNED_5201_ROLLBACK_APP_IMAGE_UNKNOWN",
            )
            if _paused(stack, name):
                stack.docker("OWNED_5201_ROLLBACK_UNPAUSE_APP", "unpause", name, log_output=False)
            stack.docker("OWNED_5201_ROLLBACK_REMOVE_APP", "rm", "-f", name, log_output=False)
        if ("container", name) in stack.resources:
            stack.resources.remove(("container", name))
        state["resources"] = stack.resources
        offline_demo.save(state)
    _start(stack, state, plan, plan["old_images"])


def _paused(stack, name):
    return (
        stack.docker(
            "OWNED_5201_APP_PAUSE_STATE",
            "inspect",
            "--format",
            "{{.State.Paused}}",
            name,
            log_output=False,
        ).stdout.strip()
        == "true"
    )


def _unpause(stack, names):
    for name in reversed(names):
        require(stack.owned("container", name) is True, "OWNED_5201_APP_UNOWNED")
        if _paused(stack, name):
            stack.docker("OWNED_5201_UNPAUSE_APP", "unpause", name, log_output=False)


@contextmanager
def _quiesce(stack, plan):
    verified = br.source_resources(STATE)
    names = [owned_container(stack, role) for role in ROLES]
    require(not any(_paused(stack, name) for name in names), "OWNED_5201_APP_ALREADY_PAUSED")

    def frozen_source(path):
        require(path == STATE, "OWNED_5201_WRONG_SOURCE")
        return verified

    def verify_quiescence(state):
        require(
            state["run_id"] == STACK_ID
            and all(_paused(stack, name) for name in names)
            and _infrastructure(stack) == plan["infra"],
            "OWNED_5201_SOURCE_NOT_QUIESCED",
        )
        # The legacy backup orchestrator expects this lifecycle probe key. The
        # probe now checks paused writers and unchanged infra, not HTTP readiness.
        return {"healthy_now": True}

    class RestoreStack(br.FreshStack):
        def create(self, kind, suffix, *args):
            if kind == "network" and suffix == "restore-net":
                require(
                    plan["restore_subnet"] == _restore_subnet(), "OWNED_5201_RESTORE_SUBNET_CHANGED"
                )
                args = (*args, "--subnet", plan["restore_subnet"])
            return super().create(kind, suffix, *args)

    retiring = False

    def retire(state):
        nonlocal retiring
        verify_quiescence(state)
        retiring = True
        for name in names:
            networks = json.loads(
                stack.docker(
                    "OWNED_5201_APP_NETWORKS",
                    "inspect",
                    "--format",
                    "{{json .NetworkSettings.Networks}}",
                    name,
                    log_output=False,
                ).stdout
            )
            expected_networks = {plan["network"]}
            if name == names[0]:
                expected_networks.add(f"{stack.prefix}-ingress")
            require(
                set(networks) == expected_networks,
                "OWNED_5201_APP_NETWORK_CHANGED",
            )
            for network in networks:
                stack.docker(
                    "OWNED_5201_DISCONNECT_PAUSED_APP",
                    "network",
                    "disconnect",
                    "-f",
                    network,
                    name,
                    log_output=False,
                )
            # Remove only after detaching the app from PG, S3, Redis and ingress.
            stack.docker("OWNED_5201_UNPAUSE_DETACHED_APP", "unpause", name, log_output=False)
            stack.docker("OWNED_5201_REMOVE_DETACHED_APP", "rm", "-f", name, log_output=False)
            stack.resources.remove(("container", name))
            offline_demo.save(state)

    try:
        for name in names:
            stack.docker("OWNED_5201_PAUSE_APP", "pause", name, log_output=False)
        verify_quiescence(verified[0])
        with patch.multiple(
            br,
            FreshStack=RestoreStack,
            source_resources=frozen_source,
            status=verify_quiescence,
            restore_readback=_generic_readback,
        ):
            yield retire
    finally:
        if not retiring:
            _unpause(stack, names)


def _upgrade(plan, state, stack, report, path):
    _heads(stack, plan["new_images"]["new_backend"])
    report["stage"] = "BACKUP_RESTORE"
    _save(path, report)
    receipt = {}
    with _quiesce(stack, plan) as retire:
        try:
            br.execute(receipt, STATE)
        finally:
            report["backup_restore"] = receipt
            if "source_ready_after" in receipt:
                receipt.pop("source_ready_after")
                receipt["source_remained_quiesced_after_restore"] = True
            _save(path, report)
        require(
            receipt["status"] == "PASSED"
            and not receipt["cleanup_failures"]
            and receipt["readback"]["source_system_identifier"] == SYSTEM_ID,
            "OWNED_5201_BACKUP_RESTORE_REQUIRED",
        )
        directory = Path(receipt["private_run_directory"])
        archive = directory / "archive"
        manifest = backup.verify_backup(archive)
        require(
            manifest.archive_sha256 == receipt["archive"]["sha256"]
            and _hash(STATE) == plan["old_state_sha256"]
            and _hash(IMAGE_FILE) == plan["image_receipt_sha256"],
            "OWNED_5201_BACKUP_OR_STATE_DRIFT",
        )
        private_write(directory / "pre-upgrade-state.json", STATE.read_text())
        private_write(directory / "pre-upgrade-plan.json", json.dumps(plan))
        report.update(stage="REPLACING_APPS", verified_archive=str(archive))
        _save(path, report)
        _identity(stack)
        require(_infrastructure(stack) == plan["infra"], "OWNED_5201_INFRA_CHANGED")
        state["status"] = "UPGRADING_APPS"
        state["resources"] = stack.resources
        offline_demo.save(state)
        try:
            retire(state)
            _start(stack, state, plan, plan["new_images"])
        except Exception:  # Restore the original app image pair without touching data.
            try:
                _rollback(stack, state, plan)
                report["rollback"] = "RESTORED_OLD_APPS"
            except Exception as error:  # noqa: BLE001 - preserve bounded recovery evidence
                report["rollback"] = "RECOVERY_REQUIRED"
                report["rollback_error_type"] = type(error).__name__
            raise


def _rollback_plan(path):
    prior = br.private_json(path)
    saved = prior["plan"]
    require(
        prior["plan_sha256"] == digest(saved)
        and prior["stage"] in {"BACKUP_RESTORE", "REPLACING_APPS", "COMPLETE"}
        and saved["owned_stack"] == STACK_ID
        and saved["system_identifier"] == SYSTEM_ID
        and saved["old_images"] == OLD
        and saved["new_images"] == _images(),
        "OWNED_5201_ROLLBACK_RECEIPT_INVALID",
    )
    state = br.private_json(STATE)
    stack = _scope(state)
    _identity(stack)
    require(_infrastructure(stack) == saved["infra"], "OWNED_5201_ROLLBACK_INFRA_CHANGED")
    plan = {
        **saved,
        "rollback_report": str(path.resolve()),
        "rollback_report_sha256": _hash(path),
        "rollback_state_sha256": _hash(STATE),
        "rollback_stage": prior["stage"],
    }
    return plan, state, stack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--rollback-from", type=Path, help="Prior private upgrade receipt")
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    _local()
    with (
        offline_demo.lifecycle_lock(STATE, shared=not args.execute),
        patch.object(offline_demo, "STATE", STATE),
    ):
        plan, state, stack = _rollback_plan(args.rollback_from) if args.rollback_from else _plan()
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}, indent=2
                )
            )
            return 0
        require(checksum == args.expected_plan_sha256, "OWNED_5201_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "PREFLIGHT",
            "plan": plan,
            "plan_sha256": checksum,
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if args.rollback_from:
                report["stage"] = "REPLACING_APPS"
                _save(args.report, report)
                if plan["rollback_stage"] == "BACKUP_RESTORE":
                    _unpause(stack, [owned_container(stack, role) for role in ROLES])
                    require(
                        _topology(stack, OLD_BACKEND, OLD_FRONTEND) == (plan["network"], SYSTEM_ID)
                        and offline_demo.status(state)["healthy_now"],
                        "OWNED_5201_ROLLBACK_OLD_APPS_UNHEALTHY",
                    )
                else:
                    state["status"] = "UPGRADING_APPS"
                    state["resources"] = stack.resources
                    offline_demo.save(state)
                    _rollback(stack, state, plan)
            else:
                _upgrade(plan, state, stack, report, args.report)
            report.update(status="PASSED", stage="COMPLETE")
        except Exception as error:  # noqa: BLE001 - retain failure without credential exposure
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
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "OWNED_5201_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
