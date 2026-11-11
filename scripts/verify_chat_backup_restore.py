"""Verify production backup/restore against an owned, disposable chat demo."""

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5

from aftercare import backup
from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.business.service import BusinessService
from aftercare.business.storage import S3ObjectStore
from aftercare.config import TARGETS, Settings
from aftercare.conversations.service import ConversationService
from aftercare.db import User
from aftercare.decisions.tables import DecisionSnapshot
from aftercare.evaluation.chat.service import BlindReviewService
from aftercare.evaluation.chat.tables import BlindExposure, BlindLabels, BlindTask
from offline_demo import STATE, lifecycle_lock, restore, status
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from verify_fresh_bootstrap import IMAGES, FreshStack, VerificationError, private_write, require

BRIDGE = """
import asyncio

async def copy(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass

async def handle(reader, writer):
    try:
        upstream, peer = await asyncio.open_connection('SOURCE_PG_ALIAS', 5432)
    except OSError:
        writer.close()
        return
    try:
        await asyncio.gather(copy(reader, peer), copy(upstream, writer))
    finally:
        writer.close()
        peer.close()

async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', 15432)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""


def published_port(stack, container, internal_port):
    line = stack.docker(
        "PORT", "port", container, f"{internal_port}/tcp", log_output=False
    ).stdout.strip()
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", line)
    require(match is not None, "LOOPBACK_BINDING_REQUIRED")
    port = int(match[1])
    require(0 < port <= 65535 and port not in {55450, 55451, 58350, 58351}, "SHARED_PORT_REJECTED")
    return port


def private_json(path):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
        "PRIVATE_STATE_REQUIRED",
    )
    return json.loads(path.read_text())


def _validate_state_path(state_path):
    require(
        state_path.parent.resolve() == STATE.parent.resolve()
        and re.fullmatch(
            r"offline-browser-demo(?:-[a-z0-9][a-z0-9-]{0,30})?\.json", state_path.name
        ),
        "DEMO_STATE_PATH_REJECTED",
    )


def source_resources(state_path=STATE):
    _validate_state_path(state_path)
    state = private_json(state_path)
    address = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", state["url"])
    require(
        address is not None
        and 1024 <= int(address[1]) <= 65535
        and int(address[1]) not in {5186, 5187, 55450, 55451, 58350, 58351}
        and (state_path.name != STATE.name or int(address[1]) == 5190),
        "DEMO_URL_REJECTED",
    )
    source = restore(state)
    prefix = f"aftercare-fresh-{state['run_id']}"
    names = {
        suffix: f"{prefix}-{suffix}"
        for suffix in ("pg", "objects", "ingress", "net", "frontend", "api")
    }
    for suffix, name in names.items():
        kind = "network" if suffix in {"ingress", "net"} else "container"
        require((kind, name) in source.resources and source.owned(kind, name), "SOURCE_NOT_OWNED")
    require(
        published_port(source, names["frontend"], 8080) == int(address[1]),
        "SOURCE_INGRESS_BINDING_MISMATCH",
    )
    names["backend_image_id"] = source.docker(
        "SOURCE_BACKEND_IMAGE",
        "container",
        "inspect",
        "--format",
        "{{.Image}}",
        names["api"],
        log_output=False,
    ).stdout.strip()
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", names["backend_image_id"]), "SOURCE_IMAGE_REJECTED"
    )
    require(status(state)["healthy_now"], "DEMO_UNAVAILABLE")
    return state, source, names


def settings_for(values, profile, pg_port, object_port):
    original = make_url(values["database_url"])
    _, database, username, _ = TARGETS[profile]
    require(
        original.drivername == "postgresql+psycopg"
        and original.username == username
        and original.database == database
        and bool(original.password)
        and not original.query,
        "SOURCE_CREDENTIAL_ROUTE_REJECTED",
    )
    return Settings(
        profile=profile,
        database_url=original.set(host="127.0.0.1", port=pg_port).render_as_string(
            hide_password=False
        ),
        object_endpoint=f"http://127.0.0.1:{object_port}",
        object_bucket=values.get("object_bucket", "aftercare-private"),
        object_access_key=values["object_access_key"],
        object_secret_key=values["object_secret_key"],
        redis_url="redis://127.0.0.1:1/0",
    )


class IsolatedTopology:
    """Adapt only the fixed local routes, retaining independent identities on each DB path."""

    def __init__(self, source, target, containers):
        self.settings = {"source": source, "target": target}
        self.cli_slot = None
        self.containers = containers
        self.identities = {}

    @contextmanager
    def route_cli(self, slot):
        previous = self.cli_slot
        self.cli_slot = slot
        try:
            yield
        finally:
            self.cli_slot = previous

    def engine(self, settings, *, test_only=False):
        routes = [slot for slot, expected in self.settings.items() if settings == expected]
        require(len(routes) == 1, "ENGINE_ROUTE_REJECTED")
        slot = routes[0]
        profile = settings.profile
        require(not test_only or (slot == "target" and profile == "test"), "ENGINE_ROUTE_REJECTED")
        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                **({"options": "-c default_transaction_read_only=on"} if slot == "source" else {}),
            },
        )
        try:
            with engine.connect() as connection, self.route_cli(slot):
                identity = backup._identity(connection)
                _, database, user, cluster = TARGETS[profile]
                require(
                    (identity[0], identity[2], identity[3]) == (cluster, database, user),
                    "DB_IDENTITY_REJECTED",
                )
                existing = self.identities.setdefault(slot, identity)
                require(existing == identity, "DB_SYSTEM_ID_CHANGED")
                backup._verify_container(profile, identity)
        except BaseException:
            engine.dispose()
            raise
        return engine

    def pg_cli(self, profile, command, database, *options, stdin=None, stdout=None):
        slot = self.cli_slot
        require(
            slot in self.settings and self.settings[slot].profile == profile,
            "PG_CLI_ROUTE_REJECTED",
        )
        stack, container = self.containers[slot]
        _, origin, username, _ = TARGETS[profile]
        require(stack.owned("container", container), "PG_CLI_CONTAINER_NOT_OWNED")
        restored = slot == "target" and self._restore_name(database)
        require(
            (command == "psql" and (database == origin or restored))
            or (command == "pg_dump" and slot == "source" and database == origin)
            or (command == "pg_restore" and restored),
            "PG_CLI_COMMAND_REJECTED",
        )
        if command == "psql":
            require(
                len(options) == 5
                and tuple(options[:4]) == ("-XAt", "-v", "ON_ERROR_STOP=1", "-c")
                and "pg_control_system()" in options[-1],
                "PG_CLI_IDENTITY_ONLY",
            )
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "sh",
                    "-c",
                    'export PGPASSWORD="${POSTGRES_PASSWORD:?}"; exec "$@"',
                    "aftercare-verify",
                    command,
                    "--host=127.0.0.1",
                    "--port=5432",
                    f"--username={username}",
                    f"--dbname={database}",
                    *options,
                ],
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            raise backup.BackupError(f"{command.upper()}_FAILED") from None
        return result.stdout

    @staticmethod
    def _restore_name(name):
        return isinstance(name, str) and re.fullmatch(r"ac_restore_[0-9a-f]{32}", name)

    def source_guard(self, settings):
        require(settings == self.settings["source"], "BACKUP_SOURCE_ROUTE_REJECTED")
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", settings.object_bucket),
            "SOURCE_BUCKET_REJECTED",
        )

    def restore_db(self, settings, name):
        require(
            settings == self.settings["target"] and self._restore_name(name),
            "RESTORE_DB_ROUTE_REJECTED",
        )
        return make_url(settings.database_url).set(database=name)

    def restore_bucket(self, settings, name):
        require(
            settings == self.settings["target"]
            and name != settings.object_bucket
            and re.fullmatch(r"ac-restore-[0-9a-f]{32}", name),
            "RESTORE_BUCKET_ROUTE_REJECTED",
        )
        return settings.model_copy(update={"object_bucket": name})

    def patch(self, contexts):
        replacements = {
            "guarded_engine": self.engine,
            "_container_pg": self.pg_cli,
            "_validate_source": self.source_guard,
            "restore_database_url": self.restore_db,
            "restored_bucket_settings": self.restore_bucket,
        }
        for field, replacement in replacements.items():
            contexts.enter_context(patch.object(backup, field, replacement))


def source_fingerprints(topology, settings):
    engine = topology.engine(settings)
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            return backup._fingerprints(connection)
    finally:
        engine.dispose()


def _material_sources(connection):
    return (
        connection.execute(
            text("""
        SELECT s.attachment_id, s.material_id, s.case_id, s.conversation_id, s.message_id,
               s.issue_id, s.kind, s.sha256, conv.customer_id, a.object_key AS original_key,
               m.object_key AS material_key, a.size, a.content_type
        FROM chat_material_sources s
        JOIN chat_attachments a ON a.id = s.attachment_id AND a.sha256 = s.sha256
        JOIN chat_messages msg ON msg.id = a.message_id AND msg.id = s.message_id
        JOIN chat_conversations conv ON conv.id = msg.conversation_id AND conv.id = s.conversation_id
        JOIN business_cases c ON c.id = s.case_id AND c.customer_id = conv.customer_id
        JOIN business_materials m ON m.id = s.material_id AND m.case_id = c.id
             AND m.kind = s.kind AND m.sha256 = s.sha256
             AND m.size = a.size AND m.content_type = a.content_type
        WHERE msg.sequence IS NOT NULL AND msg.discarded_at IS NULL
              AND msg.analysis_issue_id = s.issue_id AND msg.role = 'customer'
              AND c.decision_conversation_id = conv.id AND c.decision_issue_id = s.issue_id
        ORDER BY s.case_id, s.attachment_id
    """)
        )
        .mappings()
        .all()
    )


def _verify_material_sources(rows, business, chat, source_store, archive_dir, manifest):
    archived = {item.key: item for item in manifest.materials}
    evidence = []
    for row in rows:
        owner = UserContext(
            user_id="restore-verification", role="customer", customer_id=row["customer_id"]
        )
        foreign = owner.model_copy(update={"customer_id": "restore-verification-foreign"})
        original_args = row["conversation_id"], row["attachment_id"]
        material_args = row["case_id"], row["material_id"]
        original_bytes, original_kind = chat.content(*original_args, owner)
        typed_bytes, typed_kind = business.material_content(*material_args, owner)
        keys = row["original_key"], row["material_key"]
        require(
            keys[0] != keys[1]
            and archived[keys[0]].table_name == "chat_attachments"
            and archived[keys[1]].table_name == "business_materials"
            and archived[keys[0]].sha256 == archived[keys[1]].sha256 == row["sha256"],
            "MATERIAL_SOURCE_ARCHIVE_LINK_MISMATCH",
        )
        archived_bytes = (archive_dir / backup.OBJECTS / row["sha256"]).read_bytes()
        require(
            original_bytes == typed_bytes == archived_bytes
            and sha256(typed_bytes).hexdigest() == row["sha256"]
            and len(typed_bytes) == row["size"]
            and original_kind == typed_kind == row["content_type"]
            and all(source_store.get(key, row["size"]) == archived_bytes for key in keys),
            "MATERIAL_SOURCE_BYTES_MISMATCH",
        )
        for read, identifiers in (
            (chat.content, original_args),
            (business.material_content, material_args),
        ):
            try:
                read(*identifiers, foreign)
            except BusinessError as error:
                require(error.status == 404, "MATERIAL_SOURCE_AUTH_MISMATCH")
            else:
                raise VerificationError("MATERIAL_SOURCE_AUTH_MISMATCH")
        evidence.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "conversation_id",
                        "issue_id",
                        "case_id",
                        "message_id",
                        "attachment_id",
                        "material_id",
                        "kind",
                        "sha256",
                    )
                },
                "bytes": len(typed_bytes),
                "foreign_original_status": 404,
                "foreign_material_status": 404,
            }
        )
    return evidence


def _p5_evidence(path, browser_path, state_path, state):
    require(
        state_path.name == "offline-browser-demo-p5.json"
        and state["url"] == "http://127.0.0.1:5197"
        and state.get("profile") == "test",
        "P5_OWNED_TEST_SOURCE_REQUIRED",
    )
    report = private_json(path)
    require(
        report["status"] == "PASSED"
        and report["isolated_url"] == state["url"]
        and report["plan"]["profile"] == "test"
        and report["plan"]["human_judgments"] == 0
        and report["plan"]["browser_task_final_stage"] == "BLIND",
        "P5_CONTROLLED_HTTP_EVIDENCE_REQUIRED",
    )
    task_ids = {role: str(UUID(report[f"{role}_task_id"])) for role in ("fixture", "browser")}
    require(len(set(task_ids.values())) == 2, "P5_DISTINCT_FIXTURE_TASKS_REQUIRED")
    info = browser_path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o022 == 0,
        "P5_BROWSER_REPORT_FILE_REJECTED",
    )
    browser = json.loads(browser_path.read_text())
    require(
        browser["outcome"] == "PASS"
        and browser["evidence_kind"] == "native_browser_offline_control"
        and browser["independent_human_judgment"] is False
        and browser["gold_label"] is False
        and browser["app_profile"] == "test"
        and browser["origin"] == state["url"]
        and browser["task"]["id"] == task_ids["browser"]
        and browser["final"]["full_navigation_and_reopen_verified"] is True
        and browser["final"]["initial_immutable"] is True,
        "P5_CONTROLLED_BROWSER_EVIDENCE_REQUIRED",
    )
    hashes = {
        "fixture": report["database_audit"]["hashes"],
        "browser": [
            browser["final"][f"{stage}_sha256"] for stage in ("initial", "exposure", "labels")
        ],
    }
    require(
        all(
            len(values) == 3 and all(re.fullmatch(r"[0-9a-f]{64}", x) for x in values)
            for values in hashes.values()
        ),
        "P5_REPORT_STAGE_HASHES_INVALID",
    )
    return {
        role: {
            "task_id": task_id,
            "snapshot_id": str(UUID(report["sources"][role]["snapshot_id"])),
            "images": report["sources"][role]["original_images"],
            "stage_hashes": hashes[role],
            "input_sha256": browser["blind_state"]["input_sha256"] if role == "browser" else None,
        }
        for role, task_id in task_ids.items()
    }


def _require_not_found(operation, *args):
    try:
        operation(*args)
    except BusinessError as error:
        require(error.status == 404, "P5_RESTORED_AUTH_MISMATCH")
    else:
        raise VerificationError("P5_RESTORED_AUTH_MISMATCH")


def _verify_blind_reviews(engine, business, source_store, archive_dir, manifest, evidence, tester):
    service = BlindReviewService(business)
    reviewer = UserContext(user_id=tester["user_id"], role="tester")
    with Session(engine) as db, db.begin():
        db.execute(text("SET TRANSACTION READ ONLY"))
        user = db.get_one(User, reviewer.user_id)
        require(
            user.enabled
            and user.role == "tester"
            and user.token_hash == token_hash(tester["token"]),
            "P5_RESTORED_TESTER_CREDENTIAL_MISMATCH",
        )
        other = db.scalar(
            select(User).where(
                User.role == "tester",
                User.enabled.is_(True),
                User.id != reviewer.user_id,
            )
        )
        require(other is not None, "P5_RESTORED_UNASSIGNED_TESTER_REQUIRED")
        foreign = UserContext(user_id=other.id, role="tester")
    expected_ids = {item["task_id"] for item in evidence.values()}
    require(
        {item["id"] for item in service.list(reviewer)["items"]} == expected_ids
        and service.list(foreign)["items"] == [],
        "P5_RESTORED_TASK_LIST_MISMATCH",
    )
    archived = {item.key: item for item in manifest.materials}
    verified = {}
    for role, expected in evidence.items():
        task_id = expected["task_id"]
        detail = service.get(reviewer, task_id)
        _require_not_found(service.get, foreign, task_id)
        require(
            detail["status"] == "LABELED"
            and detail["version"] == 4
            and detail["input"]["policy_coverage"] == "COMPLETE_CATALOGUE"
            and detail["input_sha256"] == digest(detail["input"]),
            "P5_RESTORED_TASK_STATE_MISMATCH",
        )
        initial, exposure, labels = (detail[key] for key in ("initial", "exposure", "labels"))
        require(
            initial["sha256"] == digest([detail["input_sha256"], initial["payload"]])
            and exposure["sha256"]
            == digest([task_id, initial["sha256"], digest(exposure["candidates"])])
            and labels["sha256"] == digest([exposure["sha256"], labels["payload"]])
            and initial["created_at"] <= exposure["created_at"] <= labels["created_at"]
            and {row["candidate_id"] for row in labels["payload"]["items"]}
            == {row["id"] for row in exposure["candidates"]},
            "P5_RESTORED_STAGE_CHAIN_MISMATCH",
        )
        hashes = [item["sha256"] for item in (initial, exposure, labels)]
        require(hashes == expected["stage_hashes"], "P5_RESTORED_REPORT_STAGE_HASH_MISMATCH")
        if expected["input_sha256"] is not None:
            require(
                detail["input_sha256"] == expected["input_sha256"],
                "P5_RESTORED_BROWSER_INPUT_HASH_MISMATCH",
            )
        with Session(engine) as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            task = db.get_one(BlindTask, task_id)
            snapshot = db.get_one(DecisionSnapshot, expected["snapshot_id"])
            row = db.get_one(BlindExposure, task_id)
            label_row = db.get_one(BlindLabels, task_id)
            require(
                task.reviewer_id == reviewer.user_id
                and task.source_sha256 == digest([snapshot.input["input_sha256"], snapshot.id])
                and row.initial_sha256 == initial["sha256"]
                and row.candidates_sha256 == digest(exposure["candidates"])
                and label_row.exposure_sha256 == exposure["sha256"],
                "P5_RESTORED_SNAPSHOT_BINDING_MISMATCH",
            )
            keys = task.image_objects
        expected_images = {item["id"]: item for item in expected["images"]}
        require(
            len(expected_images) == 2 and set(keys) == expected_images.keys(),
            "P5_RESTORED_PHOTO_SCOPE_MISMATCH",
        )
        photos = []
        for image in detail["input"]["images"]:
            image_id = image["id"]
            record = archived[keys[image_id]]
            data, kind = service.image(reviewer, task_id, image_id)
            _require_not_found(service.image, foreign, task_id, image_id)
            original = (archive_dir / backup.OBJECTS / record.sha256).read_bytes()
            require(
                record.table_name == "chat_attachments"
                and data == original == source_store.get(record.key, record.size)
                and sha256(data).hexdigest() == record.sha256 == expected_images[image_id]["sha256"]
                and len(data) == record.size == expected_images[image_id]["size"]
                and kind == record.content_type,
                "P5_RESTORED_ORIGINAL_BYTES_MISMATCH",
            )
            photos.append({"id": image_id, "sha256": record.sha256, "bytes": len(data)})
        require(
            {item["id"] for item in photos} == expected_images.keys(),
            "P5_RESTORED_IMAGES_INCOMPLETE",
        )
        verified[role] = {
            "task_id": task_id,
            "stage": detail["status"],
            "input_sha256": detail["input_sha256"],
            "stage_hashes": hashes,
            "original_images": photos,
            "foreign_task_and_image_read": 404,
        }
    return verified


def _p5_pg_evidence(path, state_path, state, source, names):
    from verify_chat_p5_pg import _failed_manifest_hash

    require(
        state_path.name == "offline-browser-demo-p5-pg.json"
        and state.get("profile") == "test"
        and state["url"] == "http://127.0.0.1:5200",
        "P5_PG_OWNED_TEST_SOURCE_REQUIRED",
    )
    report = private_json(path)
    plan, acceptance = report["plan"], report["acceptance"]
    require(
        report["status"] == "PASSED"
        and report["stage"] == "VERIFIED"
        and report["owned_stack"] == plan["recovery"]["stack_id"] == state["run_id"]
        and report["owned_url"] == state["url"]
        and report["prior_manifest_unchanged"]
        and report["prior_failed_report_unchanged"]
        and report["plan_sha256"] == digest(plan)
        and plan["scope"] == "P5_CONTROLLED_PG_S3_RECOVERY_FROM_PREPLAN_FAILURE"
        and plan["profile"] == "test"
        and plan["images"]["aftercare-backend:chat-p5-pg-20261111"] == names["backend_image_id"]
        and acceptance["system_identifier"] == plan["recovery"]["system_identifier"]
        and acceptance["recovery_reused_existing_sentinel"]
        and set(acceptance["boundary"])
        == {"CONTROLLED_WORKER_CORE", "NO_PROVIDER_CALLS", "NO_INDEPENDENT_LABELS"},
        "P5_PG_ACCEPTANCE_REPORT_MISMATCH",
    )
    recovery = plan["recovery"]
    prior_path = Path(recovery["failed_report"])
    prior_manifest = source.directory / "p5-pg-output" / "manifest"
    prior = private_json(prior_path)
    require(
        prior_path.name == "chat-p5-pg-controlled-20261111.json"
        and prior["status"] == "FAILED"
        and prior["stage"] == "CONTROLLED_RUN"
        and prior["owned_stack"] == state["run_id"]
        and prior["owned_url"] == state["url"]
        and prior["private_manifest_directory"] == str(prior_manifest)
        and Path(recovery["prior_manifest_directory"]) == prior_manifest
        and sha256(prior_path.read_bytes()).hexdigest() == recovery["failed_report_sha256"]
        and _failed_manifest_hash(prior_manifest) == recovery["prior_manifest_sha256"],
        "P5_PG_PRIOR_FAILURE_EVIDENCE_MISMATCH",
    )
    output = source.directory / "p5-pg-recovery-output"
    primary = output / "manifest"
    require(
        Path(report["private_manifest_directory"]) == primary,
        "P5_PG_ACCEPTANCE_MANIFEST_PATH_MISMATCH",
    )
    receipt = acceptance["receipt"]
    interruption = acceptance["controlled_interruption"]
    require(
        interruption["injection_boundary"] == "AFTER_READY_BEFORE_AGENT_START"
        and interruption["status"] == "UNKNOWN"
        and interruption["additional_attempts_on_two_reopens"] == 0
        and len(receipt["rows"]) == len({item["id"] for item in receipt["rows"]}) == 6,
        "P5_PG_INTERRUPTION_NOT_VERIFIED",
    )
    schemas = {}
    for journal, campaign, plan_hash, is_primary in (
        (primary, plan["campaign"], receipt["plan_sha256"], True),
        (
            output / "interruption-manifest",
            plan["independent_recovery_campaign"],
            interruption["plan_sha256"],
            False,
        ),
    ):
        frozen = private_json(journal / "plan.json")
        require(
            digest(frozen) == plan_hash
            and frozen["campaign"] == campaign
            and frozen["controls"]["storage_topology"] == "OWNED_POSTGRESQL_S3"
            and frozen["controls"]["evidence_grade"] == "CONTROLLED_WORKER_CORE"
            and len(frozen["cases"]) == (2 if is_primary else 1)
            and len(frozen["rows"]) == (6 if is_primary else 3)
            and (is_primary or interruption["row_id"] in {row["id"] for row in frozen["rows"]}),
            "P5_PG_MANIFEST_PLAN_MISMATCH",
        )
        cases = {(row["case_id"], row["repetition"]): row for row in frozen["cases"]}

        attestation_sha = frozen["controls"]["attestation_sha256"]
        require(
            re.fullmatch(r"[0-9a-f]{64}", attestation_sha) is not None,
            "P5_PG_ATTESTATION_HASH_INVALID",
        )

        def owner_claim(
            namespace, *, campaign=campaign, attestation_sha=attestation_sha, **binding
        ):
            return {
                "version": "p5-pg-owner-v1",
                "attestation_sha256": attestation_sha,
                "campaign": campaign,
                "namespace": namespace,
                "object_prefix": f"evaluation/{namespace}/",
                **binding,
            }

        for case in cases.values():
            namespace = (
                "eval_"
                + uuid5(
                    NAMESPACE_URL,
                    digest(["p5-source-v1", campaign, case["case_id"], case["repetition"]]),
                ).hex
            )
            require(
                digest(case["scope"]) == case["scope_sha256"]
                and case["source_namespace"] == namespace
                and re.fullmatch(r"[0-9a-f]{64}", case["source_sha256"]) is not None
                and namespace not in schemas,
                "P5_PG_FROZEN_SOURCE_MISMATCH",
            )
            schemas[namespace] = {
                "case": case,
                "row": None,
                "owner_claim": owner_claim(
                    namespace,
                    source_case=case["case_id"],
                    repetition=case["repetition"],
                    source_sha256=case["source_sha256"],
                ),
            }
        primary_rows = {item["id"]: item for item in receipt["rows"]}
        for row in frozen["rows"]:
            row_id = str(UUID(row["id"]))
            saved = private_json(journal / f"{row_id}.result.json")
            namespace = "eval_" + UUID(row_id).hex
            ready = private_json(journal / f"{row_id}.ready.json")
            expected = {
                **row,
                "plan_sha256": plan_hash,
                "controls_sha256": digest(frozen["controls"]),
            }
            require(
                namespace not in schemas
                and ready == {**expected, "namespace": namespace}
                and all(saved[key] == value for key, value in expected.items()),
                "P5_PG_ARM_RECEIPT_MISMATCH",
            )
            if is_primary:
                require(
                    saved == primary_rows[row_id] and saved["checkpoint_persisted"],
                    "P5_PG_ARM_ACCEPTANCE_MISMATCH",
                )
            elif row_id == interruption["row_id"]:
                require(
                    saved["status"] == "UNKNOWN" and not saved["attempts"],
                    "P5_PG_INTERRUPTION_RECEIPT_MISMATCH",
                )
            schemas[namespace] = {
                "case": cases[(row["case_id"], row["repetition"])],
                "row": saved,
                "owner_claim": owner_claim(namespace, **expected),
            }
    require(
        len(schemas) == 12
        and {
            key
            for key, item in schemas.items()
            if item["row"] is None and item["owner_claim"]["campaign"] == plan["campaign"]
        }
        == set(acceptance["recovery_existing_sources"]),
        "P5_PG_EXPECTED_SCHEMA_SCOPE_MISMATCH",
    )
    return {
        "schemas": schemas,
        "system_identifier": acceptance["system_identifier"],
        "stack_id": state["run_id"],
        "sentinel_sha256": acceptance["object_sentinel_sha256"],
        "plan_sha256": receipt["plan_sha256"],
        "report_sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _verify_p5_pg_rows(connection, manifest, evidence):
    schemas = evidence["schemas"]
    require(
        {key.split(".")[0] for key in manifest.database if key.startswith("eval_")}
        == schemas.keys(),
        "P5_PG_RESTORED_SCHEMA_SCOPE_MISMATCH",
    )
    verified = []
    for schema, item in sorted(schemas.items()):
        row, case = item["row"], item["case"]

        def table(name, *, namespace=schema):
            return backup._qualified(namespace, name)

        def count(name):
            return connection.scalar(text(f"SELECT count(*) FROM {table(name)}"))

        owner = connection.execute(text(f"SELECT claim, ready FROM {table('p5_owner')}")).one()
        require(
            owner.ready is True
            and owner.claim == item["owner_claim"]
            and count("business_executions") == 0,
            "P5_PG_RESTORED_SCHEMA_OWNER_MISMATCH",
        )
        if row is None or row["status"] == "UNKNOWN":
            require(
                all(
                    count(name) == 0
                    for name in ("agent_runs", "agent_model_attempts", "chat_decision_snapshots")
                )
                and (
                    f"{schema}.checkpoints" not in manifest.database
                    if row is None
                    else count("checkpoints") == 0
                ),
                "P5_PG_RESTORED_EMPTY_RUN_MISMATCH",
            )
        else:
            run = connection.execute(
                text(f"SELECT id, thread_id, status FROM {table('agent_runs')}")
            ).one()
            snapshot = connection.execute(
                text(f"SELECT id, run_id, origin, input FROM {table('chat_decision_snapshots')}")
            ).one()
            checkpoints = connection.execute(
                text(f"SELECT thread_id, checkpoint_id FROM {table('checkpoints')}")
            ).all()
            require(
                tuple(run) == (row["run_id"], row["thread_id"], row["run_status"])
                and (snapshot.id, snapshot.run_id, snapshot.origin, snapshot.input)
                == (row["snapshot_id"], row["run_id"], "AGENT", case["scope"])
                and {checkpoint.thread_id for checkpoint in checkpoints} == {row["thread_id"]}
                and (row["thread_id"], row["checkpoint_id"]) in checkpoints
                and count("agent_model_attempts") == len(row["attempts"]),
                "P5_PG_RESTORED_RUN_BINDING_MISMATCH",
            )
        verified.append(
            {
                "schema": schema,
                "kind": "source" if row is None else "arm",
                **(
                    {"run_id": row.get("run_id"), "status": row["status"]}
                    if row is not None
                    else {}
                ),
            }
        )
    require(
        connection.scalar(text("SELECT count(*) FROM public.business_executions")) == 0,
        "P5_PG_PUBLIC_EXECUTIONS_PRESENT",
    )
    return verified


def _missing_pg_authorization(store, key):
    try:
        store.get(key, 4096)
    except BusinessError as error:
        require(error.code == "MATERIAL_OBJECT_MISSING", "P5_PG_AUTHORIZATION_READ_UNVERIFIED")
    else:
        raise VerificationError("P5_PG_AUTHORIZATION_OBJECT_TRANSFERRED")


def _verify_p5_pg_objects(archive_dir, manifest, result, source_store, target_store, evidence):
    require(
        result.get("p5_runtime_authorization") == "REQUIRES_NEW_ATTESTATION",
        "P5_PG_RESTORED_AUTHORIZATION_MISMATCH",
    )
    sentinel = f"evaluation/p5-stack/{evidence['stack_id']}/sentinel"
    source_sentinel = source_store.get(sentinel, 64)
    require(
        len(source_sentinel) == 64
        and sha256(source_sentinel).hexdigest() == evidence["sentinel_sha256"],
        "P5_PG_SOURCE_SENTINEL_CHANGED",
    )
    _missing_pg_authorization(target_store, sentinel)
    for schema in evidence["schemas"]:
        _missing_pg_authorization(target_store, f"evaluation/{schema}/.p5-owner.json")
    expected = {
        (schema, table): sorted(
            (row["sha256"], row["size"], row["content_type"]) for row in item["case"][key]
        )
        for schema, item in evidence["schemas"].items()
        for table, key in (
            ("chat_attachments", "images"),
            ("business_materials", "material_objects"),
        )
    }
    seen = {key: [] for key in expected}
    for record in manifest.materials:
        key = (record.schema_name, record.table_name)
        require(key in seen, "P5_PG_UNEXPECTED_ARCHIVED_OBJECT")
        original = (archive_dir / backup.OBJECTS / record.sha256).read_bytes()
        require(
            original
            == source_store.get(record.key, record.size)
            == target_store.get(record.key, record.size)
            and len(original) == record.size
            and sha256(original).hexdigest() == record.sha256,
            "P5_PG_RESTORED_OBJECT_BYTES_MISMATCH",
        )
        seen[key].append((record.sha256, record.size, record.content_type))
    require(
        {key: sorted(rows) for key, rows in seen.items()} == expected, "P5_PG_OBJECT_SCOPE_MISMATCH"
    )
    return {
        "chat_originals": sum(
            len(rows) for key, rows in seen.items() if key[1] == "chat_attachments"
        ),
        "typed_materials": sum(
            len(rows) for key, rows in seen.items() if key[1] == "business_materials"
        ),
        "source_archive_restore_bytes_match": True,
        "runtime_authorization": result["p5_runtime_authorization"],
        "sentinel_and_owner_objects_not_transferred": True,
    }


def restore_readback(
    topology,
    target,
    archive_dir,
    manifest,
    result,
    *,
    require_material_sources,
    p5=None,
    p5_pg=None,
    tester=None,
):
    engine = backup.restored_engine(target, result["database"])
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            require(
                backup._identity(connection)
                == (
                    topology.identities["target"][0],
                    topology.identities["target"][1],
                    result["database"],
                    topology.identities["target"][3],
                ),
                "RESTORE_READBACK_WRONG_SYSTEM",
            )
            if p5_pg:
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                require(
                    backup._fingerprints(connection) == manifest.database,
                    "RESTORE_READBACK_FINGERPRINT_MISMATCH",
                )
                verified = _verify_p5_pg_rows(connection, manifest, p5_pg)
            else:
                attachments = (
                    connection.execute(
                        text("""
                    SELECT a.id, a.object_key, a.sha256, a.size, a.content_type,
                           m.id AS message_id, c.id AS conversation_id, c.customer_id
                    FROM chat_attachments a JOIN chat_messages m ON m.id = a.message_id
                    JOIN chat_conversations c ON c.id = m.conversation_id
                    WHERE m.sequence IS NOT NULL AND (
                        SELECT count(*) FROM chat_attachments sibling WHERE sibling.message_id = m.id
                    ) = 2
                    ORDER BY m.id, a.position
                """)
                    )
                    .mappings()
                    .all()
                )
                require(len(attachments) >= 2, "TWO_PHOTO_MESSAGE_REQUIRED")
                link_count = connection.scalar(
                    text("""
                    SELECT count(*) FROM chat_case_links link
                    JOIN business_cases cases ON cases.id = link.case_id
                    JOIN chat_conversations conv ON conv.id = link.conversation_id
                    WHERE conv.customer_id = cases.customer_id
                """)
                )
                require(link_count > 0, "CHAT_CASE_LINK_REQUIRED")
                rows = _material_sources(connection) if require_material_sources else []
                if require_material_sources:
                    require(
                        len(rows)
                        == manifest.database[f"public.{backup.CHAT_MATERIAL_SOURCE_TABLE}"].rows
                        and len({(row["conversation_id"], row["issue_id"]) for row in rows}) > 0
                        and any(
                            sum(item["message_id"] == row["message_id"] for item in rows) >= 2
                            for row in rows
                        ),
                        "SCOPED_MATERIAL_SOURCE_PAIR_REQUIRED",
                    )
                if require_material_sources or p5:
                    connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                    require(
                        backup._fingerprints(connection) == manifest.database,
                        "RESTORE_READBACK_FINGERPRINT_MISMATCH",
                    )
        store = S3ObjectStore(topology.restore_bucket(target, result["bucket"]))
        if p5_pg:
            return {
                "restored_database_fingerprints_match_archive": True,
                "evaluation_schemas": verified,
                "objects": _verify_p5_pg_objects(
                    archive_dir,
                    manifest,
                    result,
                    S3ObjectStore(topology.settings["source"]),
                    store,
                    p5_pg,
                ),
            }
        business = BusinessService(engine, target, storage=store)
        service = ConversationService(business)
        archived = {item.key: item for item in manifest.materials}
        evidence = []
        for item in attachments[:2]:
            owner = UserContext(
                user_id="restore-verification", role="customer", customer_id=item["customer_id"]
            )
            data, kind = service.content(item["conversation_id"], item["id"], owner)
            record = archived[item["object_key"]]
            original = (archive_dir / backup.OBJECTS / record.sha256).read_bytes()
            require(
                sha256(data).hexdigest() == item["sha256"] == record.sha256
                and data == original
                and len(data) == item["size"]
                and kind == item["content_type"],
                "CHAT_ORIGINAL_MISMATCH",
            )
            foreign = owner.model_copy(update={"customer_id": "restore-verification-foreign"})
            try:
                service.content(item["conversation_id"], item["id"], foreign)
            except BusinessError as error:
                require(error.status == 404, "CHAT_ATTACHMENT_AUTH_MISMATCH")
            else:
                raise VerificationError("CHAT_ATTACHMENT_AUTH_MISMATCH")
            evidence.append({"sha256": record.sha256, "bytes": len(data), "content_type": kind})
        require(
            attachments[0]["message_id"] == attachments[1]["message_id"], "PHOTOS_NOT_SAME_MESSAGE"
        )
        readback = {
            "photo_originals": evidence,
            "case_links": link_count,
            "foreign_attachment_read": 404,
        }
        if require_material_sources:
            readback["material_sources"] = _verify_material_sources(
                rows,
                business,
                service,
                S3ObjectStore(topology.settings["source"]),
                archive_dir,
                manifest,
            )
            readback["restored_database_fingerprints_match_archive"] = True
        if p5:
            readback["blind_reviews"] = _verify_blind_reviews(
                engine,
                business,
                S3ObjectStore(topology.settings["source"]),
                archive_dir,
                manifest,
                p5,
                tester,
            )
            readback["restored_database_fingerprints_match_archive"] = True
        return readback
    finally:
        engine.dispose()


def execute(
    report,
    state_path=STATE,
    *,
    require_material_sources=False,
    p5_http_report=None,
    p5_browser_report=None,
    p5_pg_report=None,
):
    state, source, names = source_resources(state_path)
    source_profile = state.get("profile", "app")
    require(source_profile in TARGETS, "SOURCE_PROFILE_REJECTED")
    p5 = (
        _p5_evidence(p5_http_report, p5_browser_report, state_path, state)
        if p5_http_report
        else None
    )
    p5_pg = (
        _p5_pg_evidence(p5_pg_report, state_path, state, source, names) if p5_pg_report else None
    )
    tester = (
        next(row for row in private_json(source.credentials_path) if row["role"] == "tester")
        if p5
        else None
    )
    directory = Path(tempfile.mkdtemp(prefix="chat-backup-restore-", dir=state_path.parent))
    stack = FreshStack(directory)
    report.update(
        {
            "status": "RUNNING",
            "private_run_directory": str(directory),
            "source_ready": True,
            "source_url": state["url"],
            "source_profile": source_profile,
            "source_backend_image_id": names["backend_image_id"],
        }
    )
    failed = True
    try:
        endpoint = stack.docker(
            "DOCKER_CONTEXT",
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
            log_output=False,
        ).stdout.strip()
        require(
            endpoint.startswith("unix://")
            and (
                not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")
            ),
            "LOCAL_DOCKER_REQUIRED",
        )
        bridge = stack.run(
            names["backend_image_id"],
            "source-read-bridge",
            names["ingress"],
            (
                "-p",
                "127.0.0.1::15432",
                "--entrypoint",
                "python",
            ),
            ("-c", BRIDGE.replace("SOURCE_PG_ALIAS", f"pg-{source_profile}")),
        )
        stack.docker("BRIDGE_NETWORK", "network", "connect", names["net"], bridge)
        source_pg_port = published_port(stack, bridge, 15432)
        source_s3_port = published_port(source, names["objects"], 8333)
        source_values = private_json(source.runtime / "local.json")[source_profile]
        require(
            source_values["object_presign_endpoint"] == f"http://127.0.0.1:{source_s3_port}",
            "SOURCE_OBJECT_BINDING_MISMATCH",
        )
        src = settings_for(source_values, source_profile, source_pg_port, source_s3_port)
        net = stack.create("network", "restore-net")
        pg_volume = stack.create("volume", "restore-pg-data")
        s3_volume = stack.create("volume", "restore-s3-data")
        password = stack.secrets[0]
        private_write(
            directory / "restore-pg.env",
            f"POSTGRES_USER=ac_test\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=ac_test\n",
        )
        pg = stack.run(
            IMAGES["pg"],
            "restore-pg",
            net,
            (
                "-p",
                "127.0.0.1::5432",
                "--env-file",
                str(directory / "restore-pg.env"),
                "--mount",
                f"type=volume,source={pg_volume},target=/var/lib/postgresql/data",
            ),
            ("postgres", "-c", "cluster_name=aftercare-v2-test"),
        )
        objects = stack.run(
            IMAGES["objects"],
            "restore-objects",
            net,
            (
                "-p",
                "127.0.0.1::8333",
                "--mount",
                f"type=volume,source={s3_volume},target=/data",
                "--mount",
                f"type=bind,source={directory / 's3.json'},target=/config/s3.json,readonly",
            ),
            (
                "server",
                "-dir=/data",
                "-ip=localhost",
                "-ip.bind=0.0.0.0",
                "-s3",
                "-s3.port=8333",
                "-s3.config=/config/s3.json",
            ),
        )
        stack.wait(
            "DESTINATION_PG_READY",
            "exec",
            pg,
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "ac_test",
            "-d",
            "ac_test",
        )
        target_values = private_json(stack.runtime / "local.json")["app"]
        target_values["database_url"] = f"postgresql+psycopg://ac_test:{password}@127.0.0.1/ac_test"
        target = settings_for(
            target_values,
            "test",
            published_port(stack, pg, 5432),
            published_port(stack, objects, 8333),
        )
        topology = IsolatedTopology(
            src, target, {"source": (source, names["pg"]), "target": (stack, pg)}
        )
        archive = directory / "archive"
        with ExitStack() as contexts:
            topology.patch(contexts)
            # Verify both cluster routes and physical identities before archiving or restoring.
            for settings in (src, target):
                topology.engine(settings).dispose()
            require(
                topology.identities["source"][1] != topology.identities["target"][1],
                "SOURCE_AND_DESTINATION_SAME_SYSTEM",
            )
            if p5_pg:
                require(
                    str(topology.identities["source"][1]) == p5_pg["system_identifier"],
                    "P5_PG_SOURCE_SYSTEM_ID_MISMATCH",
                )
                report["p5_pg_evidence"] = {
                    key: p5_pg[key] for key in ("plan_sha256", "report_sha256")
                }
            if require_material_sources or p5 or p5_pg:
                before = source_fingerprints(topology, src)
            if require_material_sources:
                required = (
                    backup.CHAT_TABLES
                    | backup.CHAT_ANALYSIS_TABLES
                    | backup.CHAT_DECISION_TABLES
                    | {backup.CHAT_INVESTIGATION_TABLE, backup.CHAT_MATERIAL_SOURCE_TABLE}
                )
                require(
                    all(f"public.{table}" in before for table in required)
                    and before[f"public.{backup.CHAT_MATERIAL_SOURCE_TABLE}"].rows >= 2
                    and before[f"public.{backup.CHAT_INVESTIGATION_TABLE}"].rows >= 1,
                    "P4_SOURCE_EVIDENCE_REQUIRED",
                )
            if p5:
                require(
                    len(before) == 57
                    and all(
                        before.get(f"public.{table}") and before[f"public.{table}"].rows == 2
                        for table in backup.CHAT_BLIND_REVIEW_TABLES
                    ),
                    "P5_SOURCE_FINGERPRINT_OR_STAGE_COUNT_MISMATCH",
                )
            report["stage"] = "ARCHIVING"
            with topology.route_cli("source"):
                manifest = backup.create_backup(src, archive)
            require(backup.verify_backup(archive) == manifest, "ARCHIVE_VERIFICATION_MISMATCH")
            if require_material_sources or p5 or p5_pg:
                require(manifest.database == before, "SOURCE_CHANGED_BEFORE_ARCHIVE")
            tables = required if require_material_sources else backup.CHAT_TABLES
            if p5:
                tables = tables | backup.CHAT_BLIND_REVIEW_TABLES
            report["archive"] = {
                "format": manifest.format,
                "sha256": manifest.archive_sha256,
                "bytes": manifest.archive_size,
                "snapshot_at": manifest.snapshot_at.isoformat(),
                "objects": len(manifest.materials),
                "chat_objects": sum(
                    item.table_name == "chat_attachments" for item in manifest.materials
                ),
                "chat_table_rows": {
                    table: manifest.database[f"public.{table}"].rows for table in sorted(tables)
                },
            }
            report["stage"] = "RESTORING"
            with topology.route_cli("target"):
                result = backup.restore_backup(target, archive)
            require(result["status"] == "RESTORE_VERIFIED", "RESTORE_STATUS_MISMATCH")
            report["restore"] = {
                "status": result["status"],
                "database": result["database"],
                "bucket": result["bucket"],
                "material_count": result["material_count"],
                "fingerprinted_tables": len(result["table_rows"]),
                **(
                    {"p5_runtime_authorization": result["p5_runtime_authorization"]}
                    if p5_pg
                    else {}
                ),
            }
            report["stage"] = "READBACK"
            report["readback"] = restore_readback(
                topology,
                target,
                archive,
                manifest,
                result,
                require_material_sources=require_material_sources,
                p5=p5,
                p5_pg=p5_pg,
                tester=tester,
            )
            if require_material_sources or p5 or p5_pg:
                require(
                    source_fingerprints(topology, src) == before, "SOURCE_CHANGED_AFTER_RESTORE"
                )
                report["source_unchanged"] = {
                    "before_archive_after_database_fingerprints_match": True,
                    "fingerprinted_tables": len(before),
                    **(
                        {"original_and_typed_source_bytes_match_archived_snapshot": True}
                        if require_material_sources
                        else {}
                    ),
                    **({"four_blind_review_source_originals_match_archive": True} if p5 else {}),
                }
        require(status(state)["healthy_now"], "SOURCE_READINESS_CHANGED")
        report.update(status="PASSED", stage="COMPLETE", source_ready_after=True)
        failed = False
    finally:
        report["cleanup_failures"] = stack.cleanup(failed)
        report["owned_resources"] = stack.resources
        if not report["cleanup_failures"]:
            for path in (
                directory / "pg.env",
                directory / "restore-pg.env",
                directory / "s3.json",
                stack.runtime / "local.json",
            ):
                path.unlink(missing_ok=True)
        else:
            report.update(status="FAILED", failure_code="ISOLATED_RESOURCE_CLEANUP_FAILED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-state", type=Path, default=STATE)
    parser.add_argument("--require-material-sources", action="store_true")
    parser.add_argument("--p5-http-report", type=Path)
    parser.add_argument("--p5-browser-report", type=Path)
    parser.add_argument("--p5-pg-report", type=Path)
    args = parser.parse_args()
    if bool(args.p5_http_report) != bool(args.p5_browser_report):
        parser.error("P5 verification requires both --p5-http-report and --p5-browser-report")
    if args.p5_pg_report and (args.p5_http_report or args.require_material_sources):
        parser.error("--p5-pg-report is mutually exclusive with public-chat verification")
    if not args.execute:
        state, source, names = source_resources(args.source_state)
        if args.p5_pg_report:
            _p5_pg_evidence(args.p5_pg_report, args.source_state, state, source, names)
        if args.p5_http_report:
            _p5_evidence(args.p5_http_report, args.p5_browser_report, args.source_state, state)
        print(json.dumps({"status": "READY", "source": state["url"], "writes": 0}))
        return
    if args.output is None:
        parser.error("--execute requires a new --output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    report = {
        "status": "FAILED",
        "stage": "PREFLIGHT",
        "provider_calls": 0,
        "topology_adaptations": [
            "Source is the selected owned offline demo via an ephemeral loopback-only PG bridge and source S3 binding",
            "Target is a fresh owned PG cluster and S3 service with loopback-only random ports",
            "Script-local settings and Docker PG CLI routing replace the production shared-compose route",
            "Production identity SQL is compared across SQLAlchemy and container psql for both systems",
        ],
        "production_core": [
            "create_backup, verify_backup, restore_backup",
            "exported read-only repeatable-read snapshot and pg_dump custom archive",
            "manifest hashing, table fingerprint comparison, object digest and restored object verification",
            "random restore database and bucket, production fresh destination creation",
            (
                "evaluation schema run, candidate, checkpoint, and original/typed object readback"
                if args.p5_pg_report
                else "authenticated conversation attachment service with owner/foreign readback"
            ),
        ],
    }
    with os.fdopen(fd, "w") as output:
        try:
            _validate_state_path(args.source_state)
            private_json(args.source_state)
            with lifecycle_lock(args.source_state, shared=True):
                execute(
                    report,
                    args.source_state,
                    require_material_sources=args.require_material_sources,
                    p5_http_report=args.p5_http_report,
                    p5_browser_report=args.p5_browser_report,
                    p5_pg_report=args.p5_pg_report,
                )
        except BlockingIOError:
            report.update(status="FAILED", failure_code="DEMO_LIFECYCLE_BUSY")
        except (VerificationError, backup.BackupError) as error:
            report.update(status="FAILED", failure_code=str(error))
        except Exception as error:  # noqa: BLE001 - never persist arbitrary exception text or credentials
            report.update(status="FAILED", failure_code=type(error).__name__)
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps({"status": report["status"], "stage": report["stage"]}))
    raise SystemExit(report["status"] != "PASSED")


if __name__ == "__main__":
    main()
