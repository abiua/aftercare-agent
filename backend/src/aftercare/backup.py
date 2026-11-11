"""Consistent PostgreSQL snapshot and verified private material backup."""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aftercare.business.storage import MAX_BYTES, S3ObjectStore, verify_object
from aftercare.config import ROOT, TARGETS, Settings, load_settings
from aftercare.db import guarded_engine

V1_FORMAT: Literal["aftercare-backup-v1"] = "aftercare-backup-v1"
FORMAT: Literal["aftercare-backup-v2"] = "aftercare-backup-v2"
ARCHIVE = "database.dump"
MANIFEST = "manifest.json"
MANIFEST_HASH = "manifest.sha256"
OBJECTS = "objects"
FINGERPRINT_TABLES = (
    "business_orders",
    "business_order_lines",
    "business_cases",
    "business_materials",
    "business_proposals",
    "business_approvals",
    "business_executions",
    "business_simulated_payments",
    "agent_runs",
    "agent_run_events",
    "agent_model_attempts",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)
EVALUATION_SCHEMA = r"eval_[0-9a-f]{32}"
CHAT_TABLES = frozenset({"chat_conversations", "chat_messages", "chat_attachments", "chat_case_links"})
CHAT_ANALYSIS_TABLES = frozenset({"chat_analysis_turns", "chat_model_attempts", "chat_observations"})
CHAT_DECISION_TABLES = frozenset({
    "chat_decision_heads", "chat_decision_snapshots", "chat_support_decisions",
    "chat_decision_offers", "chat_customer_decisions", "chat_decision_projections",
})
CHAT_INVESTIGATION_TABLE = "chat_investigation_requests"
CHAT_MATERIAL_SOURCE_TABLE = "chat_material_sources"
CHAT_BLIND_REVIEW_TABLES = frozenset({
    "eval_chat_blind_tasks", "eval_chat_blind_initials",
    "eval_chat_blind_exposures", "eval_chat_blind_labels",
})
CHAT_KEY = r"conversations/[A-Za-z0-9_-]{1,36}/[A-Za-z0-9_-]{1,36}/[0-5]-"
MATERIAL_KEY = r"cases/[A-Za-z0-9_-]{1,36}/(?:ITEM|PARCEL|WRONG_ITEM|LABEL)/"
ISOLATED_TABLES = frozenset(
    {
        "users",
        "business_customers",
        "business_products",
        "business_orders",
        "business_order_lines",
        "business_inventory",
        "business_policies",
        "business_cases",
        "business_object_uploads",
        "business_materials",
        "business_material_reviews",
        "business_proposals",
        "business_approvals",
        "business_executions",
        "business_simulated_payments",
        "business_idempotency",
        "business_events",
        "agent_runs",
        "agent_run_events",
        "agent_run_mutations",
        "agent_model_attempts",
        "policy_vectors",
    }
)
EVALUATION_PROGRESS_TABLE = "eval_progress_events"
EVALUATION_TABLES = frozenset(
    {"eval_experiments", "eval_results", "eval_reviews", "eval_mutations"}
)
REQUIRED_PUBLIC_TABLES = frozenset(FINGERPRINT_TABLES) | ISOLATED_TABLES | EVALUATION_TABLES
# Older v2 manifests did not fingerprint these even if pg_dump contained them.
V2_ADDED_PUBLIC_FINGERPRINTS = frozenset(
    {"intake_model_audits", "alembic_version", "checkpoint_migrations"}
)
PUBLIC_TABLES = (
    REQUIRED_PUBLIC_TABLES
    | V2_ADDED_PUBLIC_FINGERPRINTS
    | CHAT_TABLES
    | CHAT_ANALYSIS_TABLES
    | CHAT_DECISION_TABLES
    | {CHAT_INVESTIGATION_TABLE, CHAT_MATERIAL_SOURCE_TABLE}
    | CHAT_BLIND_REVIEW_TABLES
    | {
        "eval_imports",
        EVALUATION_PROGRESS_TABLE,
        "dispatch_jobs",
        "dispatch_events",
        "provider_rate_scopes",
        "provider_rate_leases",
    }
)


def _qualified(schema: str, table: str) -> str:
    public_table = schema == "public" and table in PUBLIC_TABLES
    eval_table = re.fullmatch(EVALUATION_SCHEMA, schema) and table in ISOLATED_TABLES
    if not (public_table or eval_table):
        raise BackupError("DATABASE_IDENTIFIER_REJECTED")
    return f'"{schema}"."{table}"'


HexHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BackupError(Exception):
    """Safe operator-facing error code without command, response or credential data."""


class PrivateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MaterialRecord(PrivateRecord):
    key: str
    sha256: HexHash
    size: int = Field(gt=0, le=MAX_BYTES)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]

    @model_validator(mode="after")
    def check_key(self):
        if not re.fullmatch(
            rf"(?:(?:evaluation/{EVALUATION_SCHEMA}/)?{MATERIAL_KEY}|{CHAT_KEY}){self.sha256}",
            self.key,
        ):
            raise ValueError("Unexpected material object key")
        return self

    @property
    def table_name(self) -> str:
        return "chat_attachments" if self.key.startswith("conversations/") else "business_materials"

    @property
    def schema_name(self) -> str:
        return self.key.split("/")[1] if self.key.startswith("evaluation/") else "public"


class TableFingerprint(PrivateRecord):
    rows: int = Field(ge=0)
    sha256: HexHash


class BackupManifest(PrivateRecord):
    format: Literal["aftercare-backup-v1", "aftercare-backup-v2"]
    source_profile: Literal["app", "test"]
    created_at: datetime
    snapshot_at: datetime
    archive_size: int = Field(gt=0)
    archive_sha256: HexHash
    database: dict[str, TableFingerprint]
    materials: list[MaterialRecord]

    @model_validator(mode="after")
    def check_manifest(self):
        if len({item.key for item in self.materials}) != len(self.materials):
            raise ValueError("Duplicate material key")
        counts = Counter((item.schema_name, item.table_name) for item in self.materials)
        if self.format == V1_FORMAT:
            if set(self.database) != set(FINGERPRINT_TABLES):
                raise ValueError("Missing database fingerprint")
            if set(counts) - {("public", "business_materials")} or self.database["business_materials"].rows != len(
                self.materials
            ):
                raise ValueError("Material count does not match database")
            return self

        present_chat = {f"public.{table}" for table in CHAT_TABLES} & self.database.keys()
        if present_chat and present_chat != {f"public.{table}" for table in CHAT_TABLES}:
            raise ValueError("Incomplete conversation schema")
        analysis_keys = {f"public.{table}" for table in CHAT_ANALYSIS_TABLES}
        present_analysis = analysis_keys & self.database.keys()
        if present_analysis and (present_analysis != analysis_keys or not present_chat):
            raise ValueError("Incomplete conversation analysis schema")
        decision_keys = {f"public.{table}" for table in CHAT_DECISION_TABLES}
        present_decisions = decision_keys & self.database.keys()
        if present_decisions and (present_decisions != decision_keys or not present_analysis):
            raise ValueError("Incomplete conversation decision schema")
        if f"public.{CHAT_INVESTIGATION_TABLE}" in self.database and not present_decisions:
            raise ValueError("Incomplete conversation investigation schema")
        if (
            f"public.{CHAT_MATERIAL_SOURCE_TABLE}" in self.database
            and f"public.{CHAT_INVESTIGATION_TABLE}" not in self.database
        ):
            raise ValueError("Incomplete conversation material source schema")
        blind_keys = {f"public.{table}" for table in CHAT_BLIND_REVIEW_TABLES}
        present_blind = blind_keys & self.database.keys()
        if present_blind and (
            present_blind != blind_keys or f"public.{CHAT_MATERIAL_SOURCE_TABLE}" not in self.database
        ):
            raise ValueError("Incomplete chat blind review schema")
        expected_public = {f"public.{table}" for table in REQUIRED_PUBLIC_TABLES}
        if not expected_public <= self.database.keys():
            raise ValueError("Missing database fingerprint")
        material_tables = set()
        for name, fingerprint in self.database.items():
            schema, separator, table = name.partition(".")
            if not separator:
                raise ValueError("Unqualified database fingerprint")
            try:
                _qualified(schema, table)
            except BackupError:
                raise ValueError("Unexpected database fingerprint") from None
            if table in {"business_materials", "chat_attachments"}:
                material_tables.add((schema, table))
                if fingerprint.rows != counts[(schema, table)]:
                    raise ValueError("Material count does not match database")
        if set(counts) - material_tables:
            raise ValueError("Material has no database fingerprint")
        return self


def _private_dir(path: Path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise BackupError("PRIVATE_DIRECTORY_REQUIRED")
    if info.st_uid != os.getuid():
        raise BackupError("BACKUP_OWNER_MISMATCH")


def _private_file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise BackupError("PRIVATE_FILE_REQUIRED")
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise BackupError("BACKUP_OWNER_MISMATCH")
    return info


def _create_private(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


def _write(path: Path, data: bytes):
    with _create_private(path) as output:
        output.write(data)


def _sha_file(path: Path) -> str:
    _private_file(path)
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(connection):
    return tuple(
        connection.execute(
            text(
                "SELECT current_setting('cluster_name'), system_identifier::text, "
                "current_database(), current_user FROM pg_control_system()"
            )
        ).one()
    )


@lru_cache(maxsize=1)
def _compose_command() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker:
        try:
            subprocess.run(
                [docker, "compose", "version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            return docker, "compose"
    standalone = shutil.which("docker-compose")
    if standalone:
        return (standalone,)
    raise BackupError("DOCKER_COMPOSE_UNAVAILABLE")


def _container_pg(profile, command, database, *options, stdin=None, stdout=None):
    username = TARGETS[profile][2]
    args = [
        *_compose_command(),
        "--env-file",
        "/dev/null",
        "-f",
        str(ROOT / "infra" / "compose.yml"),
        "--profile",
        profile,
        "exec",
        "-T",
        f"pg-{profile}",
        "sh",
        "-c",
        'export PGPASSWORD="${POSTGRES_PASSWORD:?}"; exec "$@"',
        "aftercare-pg",
        command,
        "--host=127.0.0.1",
        "--port=5432",
        f"--username={username}",
        f"--dbname={database}",
        *options,
    ]
    try:
        return subprocess.run(
            args,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=600,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise BackupError(f"{command.upper()}_FAILED") from None


def _verify_container(profile, identity):
    command = (
        "SELECT current_setting('cluster_name'), system_identifier::text, "
        "current_database(), current_user FROM pg_control_system()"
    )
    response = _container_pg(
        profile,
        "psql",
        identity[2],
        "-XAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        command,
    )
    if not response or tuple(response.decode().strip().split("|")) != identity:
        raise BackupError("PG_CONTAINER_IDENTITY_REJECTED")


def _managed_tables(connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT n.nspname, c.relname "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid "
            "WHERE c.relkind IN ('r', 'p') "
            "AND (n.nspname = 'public' OR n.nspname ~ :evaluation_schema) "
            "ORDER BY n.nspname, c.relname"
        ),
        {"evaluation_schema": f"^{EVALUATION_SCHEMA}$"},
    )
    managed = []
    for schema, table in rows:
        if (
            schema == "public"
            and table in PUBLIC_TABLES
            or re.fullmatch(EVALUATION_SCHEMA, schema)
            and table in ISOLATED_TABLES
        ):
            managed.append((schema, table))
    present_chat = {table for schema, table in managed if schema == "public"} & CHAT_TABLES
    if present_chat and present_chat != CHAT_TABLES:
        raise BackupError("CHAT_SCHEMA_INCOMPLETE")
    present_analysis = {table for schema, table in managed if schema == "public"} & CHAT_ANALYSIS_TABLES
    if present_analysis and (present_analysis != CHAT_ANALYSIS_TABLES or not present_chat):
        raise BackupError("CHAT_ANALYSIS_SCHEMA_INCOMPLETE")
    present_decisions = {table for schema, table in managed if schema == "public"} & CHAT_DECISION_TABLES
    if present_decisions and (present_decisions != CHAT_DECISION_TABLES or not present_analysis):
        raise BackupError("CHAT_DECISION_SCHEMA_INCOMPLETE")
    if ("public", CHAT_INVESTIGATION_TABLE) in managed and not present_decisions:
        raise BackupError("CHAT_INVESTIGATION_SCHEMA_INCOMPLETE")
    if (
        ("public", CHAT_MATERIAL_SOURCE_TABLE) in managed
        and ("public", CHAT_INVESTIGATION_TABLE) not in managed
    ):
        raise BackupError("CHAT_MATERIAL_SOURCE_SCHEMA_INCOMPLETE")
    present_blind = {table for schema, table in managed if schema == "public"} & CHAT_BLIND_REVIEW_TABLES
    if present_blind and (
        present_blind != CHAT_BLIND_REVIEW_TABLES or ("public", CHAT_MATERIAL_SOURCE_TABLE) not in managed
    ):
        raise BackupError("CHAT_BLIND_REVIEW_SCHEMA_INCOMPLETE")
    if not REQUIRED_PUBLIC_TABLES <= {table for schema, table in managed if schema == "public"}:
        raise BackupError("BACKUP_REQUIRED_TABLES_MISSING")
    return managed


def _fingerprints(connection, *, format=FORMAT) -> dict[str, TableFingerprint]:
    legacy = format == V1_FORMAT
    if legacy:
        has_chat = connection.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
            "WHERE schemaname='public' AND tablename = ANY(:tables))"
        ), {"tables": sorted(CHAT_TABLES)}).scalar_one()
        if has_chat:
            raise BackupError("LEGACY_BACKUP_CONTAINS_CHAT")
    managed = (
        [("public", table) for table in FINGERPRINT_TABLES]
        if legacy
        else _managed_tables(connection)
    )
    if not legacy:
        has_progress_table = ("public", EVALUATION_PROGRESS_TABLE) in managed
        has_progress_column = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute "
                "WHERE attrelid = 'public.eval_experiments'::regclass "
                "AND attname = 'progress_seq' AND attnum > 0 AND NOT attisdropped)"
            )
        ).scalar_one()
        if has_progress_table != has_progress_column:
            raise BackupError("EVALUATION_PROGRESS_SCHEMA_MISMATCH")
    values = {}
    for schema, table in managed:
        identifier = _qualified(schema, table)
        rows, sha = connection.execute(
            text(
                "SELECT count(*), encode(sha256(convert_to(coalesce("
                "string_agg(row_hash, '' ORDER BY row_hash), ''), 'UTF8')), 'hex') "
                "FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), "
                f"'hex') AS row_hash FROM {identifier} t) AS hashed_rows"
            )
        ).one()
        key = table if legacy else f"{schema}.{table}"
        values[key] = TableFingerprint(rows=rows, sha256=sha)
    return values


def _materials(connection, *, format=FORMAT) -> list[MaterialRecord]:
    managed = (
        [("public", "business_materials")] if format == V1_FORMAT else _managed_tables(connection)
    )
    if ("public", "chat_messages") in managed:
        pending = connection.execute(text(
            'SELECT EXISTS (SELECT 1 FROM "public"."chat_messages" WHERE sequence IS NULL AND discarded_at IS NULL)'
        )).scalar_one()
        if pending:
            raise BackupError("CHAT_UPLOADS_PENDING")
    materials = []
    for schema, table in managed:
        if table not in {"business_materials", "chat_attachments"}:
            continue
        identifier = _qualified(schema, table)
        sql = (
            "SELECT a.object_key AS key, a.sha256, a.size, a.content_type, "
            "a.message_id, a.position, m.conversation_id "
            f"FROM {identifier} a JOIN public.chat_messages m ON m.id = a.message_id "
            "ORDER BY a.object_key"
            if table == "chat_attachments" else
            "SELECT object_key AS key, sha256, size, content_type "
            f"FROM {identifier} ORDER BY object_key"
        )
        rows = connection.execute(text(sql)).mappings()
        for raw in rows:
            row = dict(raw)
            if table == "chat_attachments":
                conversation = row.pop("conversation_id")
                message = row.pop("message_id")
                position = row.pop("position")
                expected = f"conversations/{conversation}/{message}/{position}-{row['sha256']}"
                if row["key"] != expected:
                    raise BackupError("CHAT_ATTACHMENT_SCOPE_MISMATCH")
            material = MaterialRecord(**row)
            if material.schema_name != "public" or material.table_name != table:
                raise BackupError("UNEXPECTED_DATABASE_MATERIAL_KEY")
            key = material.key if schema == "public" else f"evaluation/{schema}/{material.key}"
            materials.append(MaterialRecord(**{**row, "key": key}))
    return materials if format == V1_FORMAT else sorted(materials, key=lambda item: item.key)


def _validate_source(settings: Settings):
    object_port = {"app": 58350, "test": 58351}[settings.profile]
    if (
        settings.container
        or settings.object_endpoint != f"http://127.0.0.1:{object_port}"
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", settings.object_bucket)
    ):
        raise BackupError("SOURCE_NAMESPACE_REJECTED")


def _export_db(settings: Settings, archive: Path):
    engine = guarded_engine(settings)
    try:
        with (
            engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            identity = _identity(conn)
            _, database_name, username, cluster_name = TARGETS[settings.profile]
            if (identity[0], identity[2], identity[3]) != (cluster_name, database_name, username):
                raise BackupError("SOURCE_DATABASE_IDENTITY_REJECTED")
            _verify_container(settings.profile, identity)
            snapshot, snapshot_at = conn.execute(
                text("SELECT pg_export_snapshot(), transaction_timestamp()")
            ).one()
            database, materials = _fingerprints(conn), _materials(conn)
            with _create_private(archive) as output:
                _container_pg(
                    settings.profile,
                    "pg_dump",
                    identity[2],
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--snapshot={snapshot}",
                    stdout=output,
                )
    finally:
        engine.dispose()
    return database, materials, snapshot_at


def create_backup(settings: Settings, directory: Path) -> BackupManifest:
    _validate_source(settings)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    objects = directory / OBJECTS
    objects.mkdir(mode=0o700)
    objects.chmod(0o700)

    archive = directory / ARCHIVE
    database, materials, snapshot_at = _export_db(settings, archive)
    store = S3ObjectStore(settings)
    saved = set()
    for item in materials:
        content = store.get(item.key, item.size)
        verify_object(content, item.size, item.sha256)
        if item.sha256 not in saved:
            _write(objects / item.sha256, content)
            saved.add(item.sha256)

    manifest = BackupManifest(
        format=FORMAT,
        source_profile=settings.profile,
        created_at=datetime.now(UTC),
        snapshot_at=snapshot_at,
        archive_size=_private_file(archive).st_size,
        archive_sha256=_sha_file(archive),
        database=database,
        materials=materials,
    )
    data = (manifest.model_dump_json(indent=2) + "\n").encode()
    _write(directory / MANIFEST, data)
    _write(directory / MANIFEST_HASH, (hashlib.sha256(data).hexdigest() + "\n").encode())
    return manifest


def verify_backup(directory: Path) -> BackupManifest:
    _private_dir(directory)
    _private_dir(directory / OBJECTS)
    manifest_file, manifest_hash = directory / MANIFEST, directory / MANIFEST_HASH
    _private_file(manifest_file)
    _private_file(manifest_hash)
    data = manifest_file.read_bytes()
    expected = manifest_hash.read_text("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(),
        expected,
    ):
        raise BackupError("MANIFEST_HASH_MISMATCH")
    manifest = BackupManifest.model_validate_json(data)
    archive = directory / ARCHIVE
    if _private_file(archive).st_size != manifest.archive_size or not hmac.compare_digest(
        _sha_file(archive), manifest.archive_sha256
    ):
        raise BackupError("DATABASE_ARCHIVE_HASH_MISMATCH")
    with archive.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise BackupError("DATABASE_ARCHIVE_FORMAT_REJECTED")
    seen = set()
    for item in manifest.materials:
        path = directory / OBJECTS / item.sha256
        if _private_file(path).st_size != item.size:
            raise BackupError("OBJECT_SIZE_MISMATCH")
        if item.sha256 not in seen:
            if not hmac.compare_digest(_sha_file(path), item.sha256):
                raise BackupError("OBJECT_HASH_MISMATCH")
            seen.add(item.sha256)
    return manifest


def restore_database_url(settings: Settings, name: str):
    """Return only a fresh restore namespace on the project's host test PG route."""
    try:
        url = make_url(settings.database_url)
        valid = (
            settings.profile == "test"
            and not settings.container
            and url.drivername == "postgresql+psycopg"
            and url.host == "127.0.0.1"
            and url.port == 55451
            and url.username == "ac_test"
            and url.database == "ac_test"
            and bool(url.password)
            and not url.query
            and re.fullmatch(r"ac_restore_[0-9a-f]{32}", name)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise BackupError("RESTORE_DATABASE_TARGET_REJECTED")
    return url.set(database=name)


def restored_engine(settings: Settings, name: str):
    """Read or inspect a restored database without changing the app's normal guard."""
    url = restore_database_url(settings, name)
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            cluster, _, database, user = _identity(conn)
            if (cluster, database, user) != (TARGETS["test"][3], name, "ac_test"):
                raise BackupError("RESTORED_DATABASE_IDENTITY_REJECTED")
    except Exception:
        engine.dispose()
        raise
    return engine


def restored_bucket_settings(settings: Settings, bucket: str) -> Settings:
    if (
        settings.profile != "test"
        or settings.container
        or settings.object_endpoint != "http://127.0.0.1:58351"
        or bucket == settings.object_bucket
        or not re.fullmatch(r"ac-restore-[0-9a-f]{32}", bucket)
    ):
        raise BackupError("RESTORE_OBJECT_TARGET_REJECTED")
    return settings.model_copy(update={"object_bucket": bucket})


def _new_bucket(store: S3ObjectStore):
    try:
        store.client.head_bucket(Bucket=store.bucket)
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise BackupError("RESTORE_BUCKET_ACCESS_FAILED") from None
    else:
        raise BackupError("RESTORE_BUCKET_EXISTS")
    store.client.create_bucket(Bucket=store.bucket)


def _create_db(settings: Settings, name: str):
    restore_database_url(settings, name)
    engine = guarded_engine(settings, test_only=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            identity = _identity(conn)
            if (identity[0], identity[2], identity[3]) != (
                TARGETS["test"][3],
                "ac_test",
                "ac_test",
            ):
                raise BackupError("RESTORE_DATABASE_IDENTITY_REJECTED")
            _verify_container("test", identity)
            # PostgreSQL CREATE DATABASE itself rejects an existing destination.
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    finally:
        engine.dispose()
    return identity


def restore_backup(settings: Settings, directory: Path) -> dict:
    manifest = verify_backup(directory)
    namespace = secrets.token_hex(16)
    database, bucket = f"ac_restore_{namespace}", f"ac-restore-{namespace}"
    restore_database_url(settings, database)
    store = S3ObjectStore(restored_bucket_settings(settings, bucket))
    try:
        identity = _create_db(settings, database)
        target_identity = (identity[0], identity[1], database, identity[3])
        _verify_container("test", target_identity)
        with (directory / ARCHIVE).open("rb") as source:
            _container_pg(
                "test",
                "pg_restore",
                database,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                stdin=source,
            )
        engine = restored_engine(settings, database)
        try:
            with engine.connect() as connection:
                if _identity(connection) != target_identity:
                    raise BackupError("RESTORED_DATABASE_IDENTITY_REJECTED")
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                fingerprints = _fingerprints(connection, format=manifest.format)
                if manifest.format == FORMAT:
                    for table in V2_ADDED_PUBLIC_FINGERPRINTS:
                        key = f"public.{table}"
                        if key not in manifest.database:
                            fingerprints.pop(key, None)
                if (
                    fingerprints != manifest.database
                    or _materials(connection, format=manifest.format) != manifest.materials
                ):
                    raise BackupError("RESTORED_DATABASE_FINGERPRINT_MISMATCH")
        finally:
            engine.dispose()

        _new_bucket(store)
        for item in manifest.materials:
            content = (directory / OBJECTS / item.sha256).read_bytes()
            verify_object(content, item.size, item.sha256)
            store.put(item.key, content, item.content_type)
            verify_object(store.get(item.key, item.size), item.size, item.sha256)
    except BackupError as error:
        raise BackupError(f"RESTORE_FAILED {error} ({database}, {bucket})") from None
    except Exception:  # noqa: BLE001 - database and object errors may contain credentials
        raise BackupError(f"RESTORE_FAILED ({database}, {bucket})") from None
    return {
        "status": "RESTORE_VERIFIED",
        "format": manifest.format,
        "material_coverage": "public_only"
        if manifest.format == V1_FORMAT
        else "public_and_evaluation",
        "database": database,
        "bucket": bucket,
        "material_count": len(manifest.materials),
        "table_rows": {name: table.rows for name, table in manifest.database.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("create", "verify", "restore"):
        command = operations.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        if name == "create":
            command.add_argument("--profile", choices=("app", "test"), required=True)
    args = parser.parse_args()

    try:
        if args.operation == "restore":
            result = restore_backup(load_settings("test"), args.directory)
        else:
            manifest = (
                create_backup(load_settings(args.profile), args.directory)
                if args.operation == "create"
                else verify_backup(args.directory)
            )
            result = {
                "status": "BACKUP_CREATED" if args.operation == "create" else "BACKUP_VERIFIED",
                "source_profile": manifest.source_profile,
                "format": manifest.format,
                "material_coverage": "public_only"
                if manifest.format == V1_FORMAT
                else "public_and_evaluation",
                "material_count": len(manifest.materials),
                "table_rows": {name: table.rows for name, table in manifest.database.items()},
            }
    except BackupError as error:
        parser.exit(1, f"{error}\n")
    except Exception:  # noqa: BLE001 - keep unfiltered errors out of CLI output
        parser.exit(1, "BACKUP_OPERATION_FAILED\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
