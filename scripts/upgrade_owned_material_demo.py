"""Back up and verify the owned 5201 demo before replacing only its apps."""

import argparse
import json
import os
import re
from contextlib import contextmanager
from hashlib import sha256
from ipaddress import ip_network
from pathlib import Path
from tempfile import mkstemp
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
REASSESSMENT_IMAGE_FILE = ROOT / ".runtime/chat-material-reassessment-images.json"
REASSESSMENT_COMMIT = "7da08d770d72153499db7ecd5fd3231293bea263"
REASSESSMENT_BACKEND = "sha256:7319ccca9d062c57d8b2f91db388473db2cedc594f3661605bc81b6d17c43ea8"
BLIND_V2_RECEIPT_SHA = "938e04f8cdd913986aa21cda8a20f419759aff6b471f0a1e8f51b151e1416edc"
BLIND_V2_RECEIPT = ROOT / ".runtime/owned-5201-blind-v2-upgrade-retry-20261111.json"
STAGES = ("blind-v2", "material-reassessment")


def _stage_pins(stage):
    if stage == "blind-v2":
        return OLD, COMMIT, IMAGE_FILE, "OWNED_5201_APP_ONLY_BLIND_V2", {}
    require(stage == "material-reassessment", "OWNED_5201_STAGE_INVALID")
    previous = br.private_json(BLIND_V2_RECEIPT)
    prior = previous["plan"]
    require(
        _hash(BLIND_V2_RECEIPT) == BLIND_V2_RECEIPT_SHA
        and previous["status"] == "PASSED"
        and previous["stage"] == "COMPLETE"
        and previous["plan_sha256"] == digest(prior)
        and prior["boundary"] == "OWNED_5201_APP_ONLY_BLIND_V2"
        and prior["target_commit"] == COMMIT
        and prior["owned_stack"] == STACK_ID
        and prior["owned_url"] == URL
        and prior["system_identifier"] == SYSTEM_ID
        and prior["alembic_revision"] == NEW_REV,
        "OWNED_5201_PREDECESSOR_RECEIPT_INVALID",
    )
    return (
        prior["new_images"],
        REASSESSMENT_COMMIT,
        REASSESSMENT_IMAGE_FILE,
        "OWNED_5201_APP_ONLY_MATERIAL_REASSESSMENT",
        {"prior_upgrade_receipt_sha256": _hash(BLIND_V2_RECEIPT), "upgrade_stage": stage},
    )


def _analysis_settings(path):
    before = br.private_json(path)
    settings = before["test"]
    require(
        set(before) == {"test"}
        and settings["chat_analysis_enabled"] is False
        and settings["chat_vision_enabled"] is False
        and settings["chat_decisions_enabled"] is True
        and settings["chat_investigation_enabled"] is True
        and not any(settings.get(name) for name in ("model_enabled", "embedding_enabled")),
        "OWNED_5201_REASSESSMENT_SETTINGS_MISMATCH",
    )
    raw = path.read_bytes()
    updated, edits = re.subn(rb'("chat_analysis_enabled"\s*:\s*)false', rb"\g<1>true", raw)
    expected = {"test": {**settings, "chat_analysis_enabled": True}}
    require(
        edits == 1 and json.loads(updated) == expected,
        "OWNED_5201_REASSESSMENT_SETTINGS_EDIT_AMBIGUOUS",
    )
    return raw, updated


def _replace_private(path, content):
    fd, tmp = mkstemp(prefix=".owned-upgrade-settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


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


def _images(stage, old, commit, receipt):
    images = br.private_json(receipt)
    require(
        images["commit"] == commit and images["runtime_updated"] is False,
        "OWNED_5201_TARGET_IMAGE_RECEIPT_INVALID",
    )
    frontend = (
        images.get("frontend", old["new_frontend"])
        if stage == "material-reassessment"
        else images["frontend"]
    )
    new = {"new_backend": images["backend"], "new_frontend": frontend}
    if stage == "material-reassessment":
        require(
            frontend == old["new_frontend"]
            and new["new_backend"] == REASSESSMENT_BACKEND
            and new["new_backend"] != old["new_backend"],
            "OWNED_5201_REASSESSMENT_BACKEND_ONLY_REQUIRED",
        )
    for image in {*old.values(), *new.values()}:
        require(
            re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "OWNED_5201_IMAGE_PIN_MISMATCH",
        )
    require(new != old, "OWNED_5201_NEW_IMAGES_REQUIRED")
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


def _plan(stage="blind-v2"):
    _local()
    old, commit, receipt, boundary, predecessor = _stage_pins(stage)
    state = br.private_json(STATE)
    stack = _scope(state)
    require(
        state["status"] == "READY" and offline_demo.status(state)["healthy_now"],
        "OWNED_5201_SOURCE_NOT_READY",
    )
    network, system_id = _topology(stack, old["new_backend"], old["new_frontend"])
    require(
        system_id == SYSTEM_ID
        and (state["backend_image"], state["frontend_image"])
        == (old["new_backend"], old["new_frontend"]),
        "OWNED_5201_OLD_IMAGES_MISMATCH",
    )
    _identity(stack)
    infra = _infrastructure(stack)
    if stage == "material-reassessment":
        prior = br.private_json(BLIND_V2_RECEIPT)["plan"]
        require(
            (network, infra) == (prior["network"], prior["infra"]),
            "OWNED_5201_PREDECESSOR_INFRA_CHANGED",
        )
    settings_path = stack.runtime / "local.json"
    after = {}
    if stage == "material-reassessment":
        _, updated = _analysis_settings(settings_path)
        after["runtime_settings_after_sha256"] = sha256(updated).hexdigest()
    return (
        {
            "boundary": boundary,
            **predecessor,
            **after,
            "owned_stack": STACK_ID,
            "owned_url": URL,
            "system_identifier": SYSTEM_ID,
            "network": network,
            "infra": infra,
            "old_images": old,
            "new_images": _images(stage, old, commit, receipt),
            "target_commit": commit,
            "image_receipt_sha256": _hash(receipt),
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
        _hash(stack.runtime / "local.json")
        == (
            plan.get("runtime_settings_after_sha256", plan["runtime_settings_sha256"])
            if images == plan["new_images"]
            else plan["runtime_settings_sha256"]
        ),
        "OWNED_5201_RUNTIME_SETTINGS_CHANGED",
    )


def _rollback(stack, state, plan, settings_backup=None):
    _identity(stack)
    require(_infrastructure(stack) == plan["infra"], "OWNED_5201_ROLLBACK_INFRA_CHANGED")
    if "runtime_settings_after_sha256" in plan:
        current = stack.runtime / "local.json"
        require(
            settings_backup is not None
            and _hash(settings_backup) == plan["runtime_settings_sha256"]
            and _hash(current)
            in {plan["runtime_settings_sha256"], plan["runtime_settings_after_sha256"]},
            "OWNED_5201_ROLLBACK_SETTINGS_CHANGED",
        )
        original_settings = settings_backup.read_bytes()
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
    if "runtime_settings_after_sha256" in plan:
        _replace_private(current, original_settings)
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
            and _hash(_stage_pins(plan.get("upgrade_stage", "blind-v2"))[2])
            == plan["image_receipt_sha256"]
            and (
                not plan.get("prior_upgrade_receipt_sha256")
                or _hash(BLIND_V2_RECEIPT) == plan["prior_upgrade_receipt_sha256"]
            ),
            "OWNED_5201_BACKUP_OR_STATE_DRIFT",
        )
        private_write(directory / "pre-upgrade-state.json", STATE.read_text())
        private_write(directory / "pre-upgrade-plan.json", json.dumps(plan))
        settings_backup = None
        if "runtime_settings_after_sha256" in plan:
            current = stack.runtime / "local.json"
            raw, updated = _analysis_settings(current)
            require(
                sha256(raw).hexdigest() == plan["runtime_settings_sha256"]
                and sha256(updated).hexdigest() == plan["runtime_settings_after_sha256"],
                "OWNED_5201_REASSESSMENT_SETTINGS_CHANGED",
            )
            settings_backup = directory / "pre-upgrade-local.json"
            private_write(settings_backup, raw.decode("utf-8"))
            report["private_settings_backup"] = str(settings_backup)
        report.update(stage="REPLACING_APPS", verified_archive=str(archive))
        _save(path, report)
        _identity(stack)
        require(_infrastructure(stack) == plan["infra"], "OWNED_5201_INFRA_CHANGED")
        state["status"] = "UPGRADING_APPS"
        state["resources"] = stack.resources
        offline_demo.save(state)
        try:
            retire(state)
            if settings_backup is not None:
                require(
                    _hash(current) == plan["runtime_settings_sha256"],
                    "OWNED_5201_REASSESSMENT_SETTINGS_CHANGED",
                )
                _replace_private(current, updated)
            _start(stack, state, plan, plan["new_images"])
        except Exception:  # Restore the original app image pair without touching data.
            try:
                _rollback(stack, state, plan, settings_backup)
                report["rollback"] = "RESTORED_OLD_APPS"
            except Exception as error:  # noqa: BLE001 - preserve bounded recovery evidence
                report["rollback"] = "RECOVERY_REQUIRED"
                report["rollback_error_type"] = type(error).__name__
            raise


def _rollback_plan(path, stage="blind-v2"):
    prior = br.private_json(path)
    saved = prior["plan"]
    old, commit, receipt, boundary, predecessor = _stage_pins(stage)
    require(
        prior["plan_sha256"] == digest(saved)
        and prior["stage"] in {"BACKUP_RESTORE", "REPLACING_APPS", "COMPLETE"}
        and saved["owned_stack"] == STACK_ID
        and saved["system_identifier"] == SYSTEM_ID
        and saved["boundary"] == boundary
        and saved["target_commit"] == commit
        and all(saved.get(key) == value for key, value in predecessor.items())
        and saved["old_images"] == old
        and saved["new_images"] == _images(stage, old, commit, receipt),
        "OWNED_5201_ROLLBACK_RECEIPT_INVALID",
    )
    state = br.private_json(STATE)
    stack = _scope(state)
    _identity(stack)
    require(_infrastructure(stack) == saved["infra"], "OWNED_5201_ROLLBACK_INFRA_CHANGED")
    settings_rollback = {}
    if "runtime_settings_after_sha256" in saved and prior["stage"] != "BACKUP_RESTORE":
        backup_path = Path(prior["private_settings_backup"])
        require(
            backup_path
            == Path(prior["backup_restore"]["private_run_directory"]) / "pre-upgrade-local.json"
            and backup_path.parent.parent == STATE.parent
            and backup_path.parent.name.startswith("chat-backup-restore-")
            and _hash(backup_path) == saved["runtime_settings_sha256"]
            and _hash(stack.runtime / "local.json")
            in {saved["runtime_settings_sha256"], saved["runtime_settings_after_sha256"]},
            "OWNED_5201_ROLLBACK_SETTINGS_CHANGED",
        )
        br.private_json(backup_path)
        settings_rollback["rollback_settings_backup"] = str(backup_path)

    plan = {
        **saved,
        **settings_rollback,
        "rollback_report": str(path.resolve()),
        "rollback_report_sha256": _hash(path),
        "rollback_state_sha256": _hash(STATE),
        "rollback_stage": prior["stage"],
    }
    return plan, state, stack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES, default="blind-v2")
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
        plan, state, stack = (
            _rollback_plan(args.rollback_from, args.stage)
            if args.rollback_from
            else _plan(args.stage)
        )
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
                        _topology(
                            stack,
                            plan["old_images"]["new_backend"],
                            plan["old_images"]["new_frontend"],
                        )
                        == (plan["network"], SYSTEM_ID)
                        and offline_demo.status(state)["healthy_now"],
                        "OWNED_5201_ROLLBACK_OLD_APPS_UNHEALTHY",
                    )
                else:
                    state["status"] = "UPGRADING_APPS"
                    state["resources"] = stack.resources
                    offline_demo.save(state)
                    settings_backup = plan.get("rollback_settings_backup")
                    _rollback(
                        stack, state, plan, Path(settings_backup) if settings_backup else None
                    )
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
