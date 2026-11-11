"""Owned PostgreSQL/S3 runtime for controlled P5 worker-core verification."""

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    MetaData,
    String,
    Table,
    event,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.business.storage import S3ObjectStore, verify_object
from aftercare.config import TARGETS
from aftercare.db import Base, guarded_engine
from aftercare.evaluation.runner import NamespacedObjects, _namespace

from .runner import _failure, _freeze_case, _tables

_IDENTITY = """
SELECT current_database() AS database_name, current_user AS current_role,
       session_user AS session_role, current_setting('cluster_name') AS cluster_name,
       system_identifier::text AS system_identifier
FROM pg_catalog.pg_control_system()
"""
_FIELDS = ("database_name", "current_role", "session_role", "cluster_name", "system_identifier")
_OWNER_OBJECT = ".p5-owner.json"
_ENDPOINT = "http://objects-test:8333"


@dataclass(frozen=True)
class PgAttestation:
    stack_id: str
    system_identifier: str
    bucket: str
    sentinel_sha256: str
    sentinel_size: int

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-f0-9]{32}", self.stack_id)
            or not re.fullmatch(r"[0-9]{10,20}", self.system_identifier)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
            or not re.fullmatch(r"[a-f0-9]{64}", self.sentinel_sha256)
            or type(self.sentinel_size) is not int
            or not 1 <= self.sentinel_size <= 4096
        ):
            raise ValueError("CHAT_EVAL_PG_ATTESTATION_INVALID")

    def identity(self):
        _, database, role, cluster = TARGETS["test"]
        return database, role, role, cluster, self.system_identifier


def sentinel_key(stack_id):
    if not re.fullmatch(r"[a-f0-9]{32}", stack_id):
        raise ValueError("CHAT_EVAL_PG_ATTESTATION_INVALID")
    return f"evaluation/p5-stack/{stack_id}/sentinel"


def _check_settings(settings, attestation, campaign):
    if (
        settings.profile != "test"
        or not settings.container
        or os.environ.get("AFTERCARE_CONTAINER") != "1"
        or os.environ.get("AFTERCARE_PROFILE") != "test"
        or settings.model_enabled
        or settings.embedding_enabled
        or getattr(settings, "chat_vision_enabled", False)
        or str(settings.object_endpoint) != _ENDPOINT
        or settings.object_bucket != attestation.bucket
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", campaign)
    ):
        raise ValueError("CHAT_EVAL_PG_DEDICATED_RUNTIME_REQUIRED")


def _verify_identity(found, attestation):
    if tuple(found) != attestation.identity():
        raise ValueError("CHAT_EVAL_PG_IDENTITY_REJECTED")


def _owner_table(namespace):
    return Table(
        "p5_owner",
        MetaData(schema=namespace),
        Column("id", String(16), primary_key=True),
        Column("claim", JSON, nullable=False),
        Column("ready", Boolean, nullable=False),
    )


def _owner_bytes(claim):
    return json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()


def _lease_parts(attestation, campaign):
    key = digest([attestation.stack_id, campaign])
    return int(key[:8], 16) & 0x7FFFFFFF, int(key[8:16], 16) & 0x7FFFFFFF


@contextmanager
def open_pg_runtime(settings, attestation: PgAttestation, campaign):
    """Use pinned connections constructed here and an existing owned S3 bucket."""
    _check_settings(settings, attestation, campaign)
    engine = guarded_engine(settings, test_only=True)
    try:

        @event.listens_for(engine, "checkout")
        def verify_connection(connection, _record, _proxy):
            try:
                with connection.cursor() as cursor:
                    cursor.execute(_IDENTITY)
                    _verify_identity(cursor.fetchone(), attestation)
            finally:
                connection.rollback()

        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lease:
            storage = S3ObjectStore(settings)
            key = sentinel_key(attestation.stack_id)
            verify_object(
                storage.get(key, attestation.sentinel_size),
                attestation.sentinel_size,
                attestation.sentinel_sha256,
            )
            left, right = _lease_parts(attestation, campaign)
            keys = {"left": left, "right": right}
            locked = lease.scalar(
                text("SELECT pg_catalog.pg_try_advisory_lock(:left, :right)"), keys
            )
            if not locked:
                raise ValueError("CHAT_EVAL_PG_CAMPAIGN_BUSY")
            runtime = _PgRuntime(settings, attestation, campaign, engine, storage, lease, keys)
            try:
                runtime.fence()
                yield runtime
            finally:
                runtime.close()
    finally:
        engine.dispose()


class _PgRuntime:
    def __init__(self, settings, attestation, campaign, engine, storage, lease, keys):
        self.settings = settings
        self.attestation, self.campaign = attestation, campaign
        self._engine, self._storage, self._lease, self._keys = engine, storage, lease, keys
        self._lease_lock = RLock()
        self._closed = False
        self._sources = {}
        self._conninfo = make_url(settings.database_url).set(drivername="postgresql")

    def fence(self):
        with self._lease_lock:
            if (
                self._closed
                or self._lease.invalidated
                or not self._lease.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_locks
                        WHERE locktype = 'advisory' AND pid = pg_catalog.pg_backend_pid()
                          AND classid = CAST(:left AS oid) AND objid = CAST(:right AS oid)
                          AND objsubid = 2
                          AND mode = 'ExclusiveLock' AND granted
                    )
                """),
                    self._keys,
                )
            ):
                raise ValueError("CHAT_EVAL_PG_CAMPAIGN_LEASE_LOST")
        return True

    def close(self):
        with self._lease_lock:
            self._closed = True
            if not self._lease.invalidated:
                self._lease.scalar(
                    text("SELECT pg_catalog.pg_advisory_unlock(:left, :right)"), self._keys
                )

    def control_fields(self):
        return {
            "storage_topology": "OWNED_POSTGRESQL_S3",
            "attestation_sha256": digest(asdict(self.attestation)),
        }

    def _claim(self, namespace, **binding):
        return {
            "version": "p5-pg-owner-v1",
            "attestation_sha256": digest(asdict(self.attestation)),
            "campaign": self.campaign,
            "namespace": namespace,
            "object_prefix": f"evaluation/{namespace}/",
            **binding,
        }

    def _check_bucket(self):
        stamp = self.attestation
        original = self._storage.get(sentinel_key(stamp.stack_id), stamp.sentinel_size)
        verify_object(original, stamp.sentinel_size, stamp.sentinel_sha256)

    def _claim_namespace(self, namespace, claim):
        self.fence()
        self._check_bucket()
        table = _owner_table(namespace)
        data = _owner_bytes(claim)
        key = claim["object_prefix"] + _OWNER_OBJECT
        with self._engine.begin() as db:
            db.execute(CreateSchema(namespace))
            table.create(db)
            db.execute(insert(table).values(id="owner", claim=claim, ready=False))
            try:
                self._storage.get(key, len(data))
            except BusinessError as error:
                if error.code != "MATERIAL_OBJECT_MISSING":
                    raise
            else:
                raise ValueError("CHAT_EVAL_PG_OBJECT_CLAIM_EXISTS")
            try:
                self._storage.client.put_object(
                    Bucket=self.attestation.bucket,
                    Key=key,
                    Body=data,
                    ContentType="application/json",
                    CacheControl="private, no-store",
                    IfNoneMatch="*",
                )
            except (BotoCoreError, ClientError):
                raise ValueError("CHAT_EVAL_PG_OBJECT_CLAIM_FAILED") from None
            self._verify_object_claim(claim)

    def _verify_object_claim(self, claim):
        data = _owner_bytes(claim)
        actual = self._storage.get(claim["object_prefix"] + _OWNER_OBJECT, len(data))
        if actual != data:
            raise ValueError("CHAT_EVAL_PG_OWNER_MISMATCH")

    def _verify_owner(self, namespace, claim, *, require_ready=True):
        self.fence()
        self._check_bucket()
        table = _owner_table(namespace)
        with self._engine.connect() as db:
            observed = db.execute(
                select(table.c.claim, table.c.ready).where(table.c.id == "owner")
            ).one_or_none()
        if observed is None or observed.claim != claim or (require_ready and not observed.ready):
            raise ValueError("CHAT_EVAL_PG_OWNER_MISMATCH")
        self._verify_object_claim(claim)

    def _mark_ready(self, namespace, claim):
        self._verify_owner(namespace, claim, require_ready=False)
        table = _owner_table(namespace)
        with self._engine.begin() as db:
            row = db.execute(
                update(table)
                .where(table.c.id == "owner", table.c.ready.is_(False))
                .values(ready=True)
            )
            if row.rowcount != 1:
                raise ValueError("CHAT_EVAL_PG_OWNER_MISMATCH")

    def _scoped_engine(self, namespace):
        return self._engine.execution_options(schema_translate_map={None: namespace})

    @contextmanager
    def source(self, case_id, repetition, source_sha256):
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", case_id)
            or type(repetition) is not int
            or repetition < 1
            or not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
            or (case_id, repetition) in self._sources
        ):
            raise ValueError("CHAT_EVAL_PG_SOURCE_INVALID")
        self.fence()
        namespace = _namespace(
            str(uuid5(NAMESPACE_URL, digest(["p5-source-v1", self.campaign, case_id, repetition])))
        )
        claim = self._claim(
            namespace, source_case=case_id, repetition=repetition, source_sha256=source_sha256
        )
        with self._engine.connect() as db:
            existing = db.scalar(
                text("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = :namespace"),
                {"namespace": namespace},
            )
        if existing:
            self._verify_owner(namespace, claim)
        else:
            self._claim_namespace(namespace, claim)
        engine = self._scoped_engine(namespace)
        storage = NamespacedObjects(self._storage, namespace)
        if not existing:
            Base.metadata.create_all(engine, tables=_tables())
        yield engine, storage, not existing
        if not existing:
            self._mark_ready(namespace, claim)
        self._sources[case_id, repetition] = (engine, storage, namespace, claim)

    def freeze(self, case):
        registered = self._sources.get((case.case_id, case.repetition))
        if (
            registered is None
            or case.source.engine is not registered[0]
            or case.source.storage is not registered[1]
            or case.source.settings is not self.settings
            or case.source.engine.get_execution_options().get("schema_translate_map")
            != {None: registered[2]}
            or case.source.storage.prefix != registered[3]["object_prefix"]
            or case.source.storage.storage is not self._storage
        ):
            raise ValueError("CHAT_EVAL_PG_SOURCE_UNOWNED")
        self._verify_owner(registered[2], registered[3])
        frozen = _freeze_case(case)
        frozen.record.update(
            source_namespace=registered[2], source_sha256=registered[3]["source_sha256"]
        )
        return frozen

    def run(self, manifest, runner, cases):
        self.fence()
        return runner._run(manifest, self.campaign, cases, self)

    def _arm_claim(self, row):
        namespace = _namespace(row["id"])
        return namespace, self._claim(namespace, **row)

    @contextmanager
    def _saver(self, namespace, *, read_only=False):
        # Keep raw LangGraph tables in the arm schema. SQLAlchemy translation
        # cannot affect the independent psycopg connection used by its saver.
        with psycopg.connect(
            self._conninfo.render_as_string(hide_password=False),
            connect_timeout=5,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        ) as connection:
            identity = connection.execute(_IDENTITY).fetchone()
            _verify_identity((identity[key] for key in _FIELDS), self.attestation)
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(namespace)))
            if (
                connection.execute("SELECT current_schema() AS schema").fetchone()["schema"]
                != namespace
            ):
                raise ValueError("CHAT_EVAL_PG_CHECKPOINT_SCHEMA_REJECTED")
            if read_only:
                connection.execute("SET default_transaction_read_only TO on")
            yield PostgresSaver(connection)

    @contextmanager
    def arm(self, _manifest, row, _source, *, create):
        namespace, claim = self._arm_claim(row)
        if create:
            self._claim_namespace(namespace, claim)
            with self._saver(namespace) as saver:
                saver.setup()
                self._mark_ready(namespace, claim)
                yield (
                    self._scoped_engine(namespace),
                    NamespacedObjects(self._storage, namespace),
                    namespace,
                    saver,
                )
        else:
            self._verify_owner(namespace, claim)
            yield (
                self._scoped_engine(namespace).execution_options(postgresql_readonly=True),
                NamespacedObjects(self._storage, namespace),
                namespace,
                None,
            )

    def checkpoint(self, row, result):
        namespace, claim = self._arm_claim(row)
        checkpoint_id = None
        try:
            self._verify_owner(namespace, claim)
            with self._saver(namespace, read_only=True) as saver:
                checkpoint = saver.get_tuple({"configurable": {"thread_id": result["thread_id"]}})
                if checkpoint:
                    checkpoint_id = checkpoint.config["configurable"]["checkpoint_id"]
        except (psycopg.Error, BusinessError, ValueError) as error:
            if result["snapshot_id"] and result["input_comparable"]:
                integrity_failed = str(error) in {
                    "CHAT_EVAL_PG_OWNER_MISMATCH",
                    "CHAT_EVAL_PG_CHECKPOINT_SCHEMA_REJECTED",
                    "CHAT_EVAL_PG_IDENTITY_REJECTED",
                }
                result.update(
                    status="FAILED" if integrity_failed else "UNKNOWN",
                    reason="CHAT_EVAL_PG_CHECKPOINT_UNVERIFIED",
                    input_comparable=False,
                    candidate_status="UNUSABLE",
                )
        if checkpoint_id is None and result["input_comparable"] and result["snapshot_id"]:
            result.update(
                status="FAILED",
                reason="CHAT_EVAL_PG_CHECKPOINT_MISSING",
                input_comparable=False,
                candidate_status="UNUSABLE",
            )
        return {"checkpoint_persisted": checkpoint_id is not None, "checkpoint_id": checkpoint_id}

    @staticmethod
    def setup_failure(reason):
        return _failure("FAILED" if reason == "CHAT_EVAL_ARM_INPUT_MISMATCH" else "UNKNOWN", reason)
