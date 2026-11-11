"""Verify production backup/restore against the disposable 5190 chat demo."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from aftercare import backup
from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.service import BusinessService
from aftercare.business.storage import S3ObjectStore
from aftercare.config import TARGETS, Settings
from aftercare.conversations.service import ConversationService
from offline_demo import STATE, restore, status
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
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
        upstream, peer = await asyncio.open_connection('pg-app', 5432)
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


def source_resources():
    state = private_json(STATE)
    require(state["url"] == "http://127.0.0.1:5190", "DEMO_URL_REJECTED")
    source = restore(state)
    require(status(state)["healthy_now"], "DEMO_UNAVAILABLE")
    prefix = f"aftercare-fresh-{state['run_id']}"
    names = {suffix: f"{prefix}-{suffix}" for suffix in ("pg", "objects", "ingress", "net")}
    for suffix, name in names.items():
        kind = "container" if suffix in {"pg", "objects"} else "network"
        require((kind, name) in source.resources and source.owned(kind, name), "SOURCE_NOT_OWNED")
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
        self.settings = {"app": source, "test": target}
        self.containers = containers
        self.identities = {}

    def engine(self, settings, *, test_only=False):
        profile = settings.profile
        expected = self.settings[profile]
        require(
            settings == expected and (not test_only or profile == "test"), "ENGINE_ROUTE_REJECTED"
        )
        engine = create_engine(
            expected.database_url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                **({"options": "-c default_transaction_read_only=on"} if profile == "app" else {}),
            },
        )
        try:
            with engine.connect() as connection:
                identity = backup._identity(connection)
                _, database, user, cluster = TARGETS[profile]
                require(
                    (identity[0], identity[2], identity[3]) == (cluster, database, user),
                    "DB_IDENTITY_REJECTED",
                )
                existing = self.identities.setdefault(profile, identity)
                require(existing == identity, "DB_SYSTEM_ID_CHANGED")
                backup._verify_container(profile, identity)
        except BaseException:
            engine.dispose()
            raise
        return engine

    def pg_cli(self, profile, command, database, *options, stdin=None, stdout=None):
        stack, container = self.containers[profile]
        _, origin, username, _ = TARGETS[profile]
        require(stack.owned("container", container), "PG_CLI_CONTAINER_NOT_OWNED")
        allowed = database == origin or (profile == "test" and self._restore_name(database))
        require(allowed, "PG_CLI_DATABASE_REJECTED")
        require(
            (command == "psql" and database == origin)
            or (command == "psql" and profile == "test")
            or (command == "pg_dump" and profile == "app" and database == origin)
            or (command == "pg_restore" and profile == "test" and self._restore_name(database)),
            "PG_CLI_COMMAND_REJECTED",
        )
        if command == "psql":
            require(
                tuple(options[:5]) == ("-XAt", "-v", "ON_ERROR_STOP=1", "-c", options[-1])
                and len(options) == 5
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
        require(settings == self.settings["app"], "BACKUP_SOURCE_ROUTE_REJECTED")
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", settings.object_bucket),
            "SOURCE_BUCKET_REJECTED",
        )

    def restore_db(self, settings, name):
        require(
            settings == self.settings["test"] and self._restore_name(name),
            "RESTORE_DB_ROUTE_REJECTED",
        )
        return make_url(settings.database_url).set(database=name)

    def restore_bucket(self, settings, name):
        require(
            settings == self.settings["test"]
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


def restore_readback(topology, target, archive_dir, manifest, result):
    engine = backup.restored_engine(target, result["database"])
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            require(
                backup._identity(connection)
                == (
                    topology.identities["test"][0],
                    topology.identities["test"][1],
                    result["database"],
                    topology.identities["test"][3],
                ),
                "RESTORE_READBACK_WRONG_SYSTEM",
            )
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
        store = S3ObjectStore(topology.restore_bucket(target, result["bucket"]))
        service = ConversationService(BusinessService(engine, target, storage=store))
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
        return {
            "photo_originals": evidence,
            "case_links": link_count,
            "foreign_attachment_read": 404,
        }
    finally:
        engine.dispose()


def execute(report):
    state, source, names = source_resources()
    directory = Path(tempfile.mkdtemp(prefix="chat-backup-restore-", dir=STATE.parent))
    stack = FreshStack(directory)
    report.update(
        {"status": "RUNNING", "private_run_directory": str(directory), "source_ready": True}
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
            state["backend_image"],
            "source-read-bridge",
            names["ingress"],
            (
                "-p",
                "127.0.0.1::15432",
                "--entrypoint",
                "python",
            ),
            ("-c", BRIDGE),
        )
        stack.docker("BRIDGE_NETWORK", "network", "connect", names["net"], bridge)
        source_pg_port = published_port(stack, bridge, 15432)
        source_s3_port = published_port(source, names["objects"], 8333)
        source_values = private_json(source.runtime / "local.json")["app"]
        require(
            source_values["object_presign_endpoint"] == f"http://127.0.0.1:{source_s3_port}",
            "SOURCE_OBJECT_BINDING_MISMATCH",
        )
        src = settings_for(source_values, "app", source_pg_port, source_s3_port)
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
            src, target, {"app": (source, names["pg"]), "test": (stack, pg)}
        )
        archive = directory / "archive"
        with ExitStack() as contexts:
            topology.patch(contexts)
            # Verify both cluster routes and physical identities before archiving or restoring.
            for settings in (src, target):
                topology.engine(settings).dispose()
            require(
                topology.identities["app"][1] != topology.identities["test"][1],
                "SOURCE_AND_DESTINATION_SAME_SYSTEM",
            )
            report["stage"] = "ARCHIVING"
            manifest = backup.create_backup(src, archive)
            require(backup.verify_backup(archive) == manifest, "ARCHIVE_VERIFICATION_MISMATCH")
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
                    table: manifest.database[f"public.{table}"].rows
                    for table in sorted(backup.CHAT_TABLES)
                },
            }
            report["stage"] = "RESTORING"
            result = backup.restore_backup(target, archive)
            report["restore"] = {
                "status": result["status"],
                "database": result["database"],
                "bucket": result["bucket"],
                "material_count": result["material_count"],
                "fingerprinted_tables": len(result["table_rows"]),
            }
            report["stage"] = "READBACK"
            report["readback"] = restore_readback(topology, target, archive, manifest, result)
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
    args = parser.parse_args()
    if not args.execute:
        source_resources()
        print(
            json.dumps(
                {"status": "READY", "source": "offline browser demo on port 5190", "writes": 0}
            )
        )
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
            "Source is the owned offline 5190 demo via an ephemeral loopback-only PG bridge and source S3 binding",
            "Target is a fresh owned PG cluster and S3 service with loopback-only random ports",
            "Script-local settings and Docker PG CLI routing replace the production shared-compose route",
            "Production identity SQL is compared across SQLAlchemy and container psql for both systems",
        ],
        "production_core": [
            "create_backup, verify_backup, restore_backup",
            "exported read-only repeatable-read snapshot and pg_dump custom archive",
            "manifest hashing, table fingerprint comparison, object digest and restored object verification",
            "random restore database and bucket, production fresh destination creation",
            "authenticated conversation attachment service with owner/foreign readback",
        ],
    }
    with os.fdopen(fd, "w") as output:
        try:
            with STATE.with_suffix(".lock").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                execute(report)
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
