import hashlib
import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from botocore.exceptions import ClientError

from aftercare import backup
from aftercare.config import Settings

RESTORE_ID = "1" * 32
EVAL_SCHEMA = "eval_" + "2" * 32
CONTENT = b"synthetic material body"


def settings(**overrides):
    fields = {
        "profile": "test",
        "database_url": "postgresql+psycopg://ac_test:private@127.0.0.1:55451/ac_test",
        "object_endpoint": "http://127.0.0.1:58351",
        "object_access_key": "private-s3-access",
        "object_secret_key": "private-s3-secret",
        "redis_url": "redis://127.0.0.1:56380/0",
    }
    return Settings(**{**fields, **overrides})


class FakeStore:
    contents: ClassVar[dict[tuple[str, str], bytes]] = {}
    buckets: ClassVar[set[str]] = {"aftercare-private"}

    def __init__(self, config):
        self.bucket = config.object_bucket
        self.client = self

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket")

    def create_bucket(self, Bucket):
        assert Bucket not in self.buckets
        self.buckets.add(Bucket)

    def get(self, key, expected_size):
        result = self.contents[self.bucket, key]
        assert len(result) == expected_size
        return result

    def put(self, key, content, content_type):
        assert self.bucket != "aftercare-private"
        assert self.bucket in self.buckets
        assert (self.bucket, key) not in self.contents
        assert content_type == "image/png"
        self.contents[self.bucket, key] = content


@pytest.fixture
def package(tmp_path, monkeypatch):
    sha = hashlib.sha256(CONTENT).hexdigest()
    material = backup.MaterialRecord(
        key=f"cases/case-1/ITEM/{sha}",
        sha256=sha,
        size=len(CONTENT),
        content_type="image/png",
    )
    scoped = backup.MaterialRecord(
        key=f"evaluation/{EVAL_SCHEMA}/{material.key}",
        sha256=sha,
        size=material.size,
        content_type=material.content_type,
    )
    fingerprints = {
        f"public.{table}": backup.TableFingerprint(rows=1, sha256=sha)
        for table in backup.PUBLIC_TABLES
    }
    fingerprints["public.chat_attachments"] = backup.TableFingerprint(rows=0, sha256=sha)
    for table in ("business_materials", "agent_model_attempts"):
        fingerprints[f"{EVAL_SCHEMA}.{table}"] = backup.TableFingerprint(rows=1, sha256=sha)
    FakeStore.contents = {("aftercare-private", row.key): CONTENT for row in (material, scoped)}
    FakeStore.buckets = {"aftercare-private"}
    monkeypatch.setattr(backup, "S3ObjectStore", FakeStore)

    def export(_settings, archive):
        backup._write(archive, b"PGDMP example binary dump")
        return fingerprints, [material, scoped], datetime(2026, 11, 10, tzinfo=UTC)

    monkeypatch.setattr(backup, "_export_db", export)
    directory = tmp_path / "backup"
    manifest = backup.create_backup(settings(), directory)
    return directory, manifest, material


def _legacy_package(directory, manifest):
    value = manifest.model_dump(mode="json")
    value["format"] = backup.V1_FORMAT
    value["database"] = {
        name: value["database"][f"public.{name}"] for name in backup.FINGERPRINT_TABLES
    }
    value["materials"] = [row for row in value["materials"] if row["key"].startswith("cases/")]
    return _replace_manifest(directory, value)


def _replace_manifest(directory, value):
    legacy = backup.BackupManifest.model_validate_json(json.dumps(value))
    data = (legacy.model_dump_json(indent=2) + "\n").encode()
    (directory / backup.MANIFEST).write_bytes(data)
    (directory / backup.MANIFEST_HASH).write_text(hashlib.sha256(data).hexdigest() + "\n")
    assert backup.verify_backup(directory) == legacy
    return legacy


def test_package_is_private_and_readback_verified(package):
    directory, manifest, material = package
    assert backup.verify_backup(directory) == manifest
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / backup.OBJECTS).stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600 for path in directory.rglob("*") if path.is_file()
    )
    assert (directory / backup.OBJECTS / material.sha256).read_bytes() == CONTENT
    with pytest.raises(FileExistsError):
        backup.create_backup(settings(), directory)
    assert backup.verify_backup(directory) == manifest


@pytest.mark.parametrize("artifact", ["manifest", "archive", "object"])
def test_manifest_archive_and_objects_reject_corruption(package, artifact):
    directory, _, material = package
    relative, code = {
        "manifest": (Path(backup.MANIFEST), "MANIFEST_HASH_MISMATCH"),
        "archive": (Path(backup.ARCHIVE), "DATABASE_ARCHIVE_HASH_MISMATCH"),
        "object": (Path(backup.OBJECTS) / material.sha256, "OBJECT_HASH_MISMATCH"),
    }[artifact]
    path = directory / relative
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(backup.BackupError, match=code):
        backup.verify_backup(directory)


def test_broad_permissions_and_symlink_are_rejected(package, tmp_path):
    directory, _, material = package
    object_path = directory / backup.OBJECTS / material.sha256
    object_path.chmod(0o644)
    with pytest.raises(backup.BackupError, match="PRIVATE_FILE_REQUIRED"):
        backup.verify_backup(directory)
    outside = tmp_path / "outside"
    outside.write_bytes(CONTENT)
    outside.chmod(0o600)
    object_path.unlink()
    object_path.symlink_to(outside)
    with pytest.raises(backup.BackupError, match="PRIVATE_FILE_REQUIRED"):
        backup.verify_backup(directory)
    assert outside.read_bytes() == CONTENT


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile": "app"},
        {"container": True},
        {"database_url": "postgresql+psycopg://ac_app:private@127.0.0.1:55450/ac_app"},
        {"database_url": "postgresql+psycopg://ac_test:private@localhost:55451/ac_test"},
        {
            "database_url": "postgresql+psycopg://ac_test:private@127.0.0.1:55451/ac_test?sslmode=disable"
        },
        {"database_url": "postgresql+psycopg://other:private@127.0.0.1:55451/ac_test"},
    ],
)
def test_guard_rejects_non_project_anchor(overrides):
    with pytest.raises(backup.BackupError, match="RESTORE_DATABASE_TARGET_REJECTED"):
        backup.restore_database_url(settings(**overrides), f"ac_restore_{RESTORE_ID}")


@pytest.mark.parametrize("name", ["ac_test", "ac_app", "ac_restore_test", "ac_restore_'bad'"])
def test_restore_db_guard_rejects_existing_base_or_unscoped_name(name):
    with pytest.raises(backup.BackupError, match="RESTORE_DATABASE_TARGET_REJECTED"):
        backup.restore_database_url(settings(), name)


def test_s3_guard_excludes_app_and_source_bucket():
    good = settings()
    name = f"ac-restore-{RESTORE_ID}"
    assert backup.restored_bucket_settings(good, name).object_bucket == name
    for bad in (settings(object_endpoint="http://127.0.0.1:58350"), settings(object_bucket=name)):
        with pytest.raises(backup.BackupError, match="RESTORE_OBJECT_TARGET_REJECTED"):
            backup.restored_bucket_settings(bad, name)
    assert backup.restore_database_url(good, f"ac_restore_{RESTORE_ID}").database == (
        f"ac_restore_{RESTORE_ID}"
    )


def test_restore_verifies_before_resource_creation(package, monkeypatch):
    directory, _, material = package
    (directory / backup.OBJECTS / material.sha256).unlink()
    monkeypatch.setattr(backup, "_create_db", lambda *_: pytest.fail("destination created"))
    with pytest.raises(FileNotFoundError):
        backup.restore_backup(settings(), directory)


@pytest.mark.parametrize(
    "scenario",
    [
        "v1",
        "old_v2",
        "v2_before_progress",
        "current_v2",
        "changed_intake",
        "missing_intake",
        "changed_progress",
        "missing_progress",
        "unrecorded_progress",
    ],
)
def test_restore_writes_new_bucket_and_validates_db_fingerprints(package, monkeypatch, scenario):
    directory, manifest, _ = package
    restored = dict(manifest.database)
    if scenario == "v1":
        manifest = _legacy_package(directory, manifest)
        restored = dict(manifest.database)
    elif scenario in {"old_v2", "v2_before_progress", "unrecorded_progress"}:
        value = manifest.model_dump(mode="json")
        del value["database"]["public.eval_progress_events"]
        if scenario != "unrecorded_progress":
            del restored["public.eval_progress_events"]
            # Old archives restore their own schema, so their experiment row hash excludes progress_seq.
            value["database"]["public.eval_experiments"]["sha256"] = "2" * 64
            restored["public.eval_experiments"] = backup.TableFingerprint(rows=1, sha256="2" * 64)
        if scenario == "old_v2":
            for key in ("intake_model_audits", "alembic_version", "checkpoint_migrations"):
                del value["database"][f"public.{key}"]
        manifest = _replace_manifest(directory, value)
    elif scenario == "changed_intake":
        restored["public.intake_model_audits"] = backup.TableFingerprint(rows=1, sha256="0" * 64)
    elif scenario == "missing_intake":
        del restored["public.intake_model_audits"]
    elif scenario == "changed_progress":
        restored["public.eval_progress_events"] = backup.TableFingerprint(rows=1, sha256="0" * 64)
    elif scenario == "missing_progress":
        del restored["public.eval_progress_events"]
    calls = []
    connection = SimpleNamespace(execute=lambda *_: None)
    engine = SimpleNamespace(
        connect=lambda: nullcontext(connection),
        dispose=lambda: calls.append("disposed"),
    )
    identity = ("aftercare-v2-test", "500", "ac_test", "ac_test")
    database = f"ac_restore_{RESTORE_ID}"
    bucket = f"ac-restore-{RESTORE_ID}"
    monkeypatch.setattr(backup.secrets, "token_hex", lambda _: RESTORE_ID)
    monkeypatch.setattr(
        backup, "_create_db", lambda _, name: identity if name == database else None
    )
    monkeypatch.setattr(backup, "_verify_container", lambda profile, _: calls.append(profile))
    monkeypatch.setattr(backup, "restored_engine", lambda *_: engine)
    monkeypatch.setattr(
        backup, "_identity", lambda _: (identity[0], identity[1], database, identity[3])
    )

    def fingerprints(_, *, format):
        assert format == manifest.format
        return restored

    def materials(_, *, format):
        assert format == manifest.format
        return manifest.materials

    monkeypatch.setattr(backup, "_fingerprints", fingerprints)
    monkeypatch.setattr(backup, "_materials", materials)

    def postgres(profile, command, database_name, *flags, stdin):
        assert profile == "test" and command == "pg_restore" and database_name == database
        assert "--exit-on-error" in flags
        assert "--single-transaction" not in flags
        assert "--clean" not in flags and "--create" not in flags
        assert stdin.read().startswith(b"PGDMP")
        calls.append(command)

    monkeypatch.setattr(backup, "_container_pg", postgres)
    if scenario in {
        "changed_intake",
        "missing_intake",
        "changed_progress",
        "missing_progress",
        "unrecorded_progress",
    }:
        with pytest.raises(backup.BackupError, match="RESTORED_DATABASE_FINGERPRINT_MISMATCH"):
            backup.restore_backup(settings(), directory)
        assert bucket not in FakeStore.buckets
        return
    result = backup.restore_backup(settings(), directory)
    assert result["status"] == "RESTORE_VERIFIED"
    assert result["database"] == database and result["bucket"] == bucket
    assert calls == ["test", "pg_restore", "disposed"]
    assert result["format"] == manifest.format
    assert result["material_coverage"] == (
        "public_only" if scenario == "v1" else "public_and_evaluation"
    )
    assert {key for saved_bucket, key in FakeStore.contents if saved_bucket == bucket} == {
        item.key for item in manifest.materials
    }
    assert all(data == CONTENT for data in FakeStore.contents.values())


def test_subprocess_does_not_pass_password_in_arguments(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(stdout=b"response")

    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(backup, "_compose_command", lambda: ("/example/docker-compose",))
    assert backup._container_pg("test", "psql", "ac_test", "-XAt") == b"response"
    args, kwargs = calls[0]
    assert "pg-test" in args and "--username=ac_test" in args
    assert args[:3] == ["/example/docker-compose", "--env-file", "/dev/null"]
    assert "private" not in " ".join(args)
    assert kwargs["stderr"] == backup.subprocess.DEVNULL
    assert "env" not in kwargs


def test_cli_errors_never_print_credentials(tmp_path, monkeypatch, capsys):
    def failure(*_):
        raise RuntimeError("password=credential-must-not-leak")

    monkeypatch.setattr(backup, "load_settings", failure)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup",
            "create",
            "--profile",
            "test",
            "--directory",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit, match="1"):
        backup.main()
    assert capsys.readouterr().err == "BACKUP_OPERATION_FAILED\n"


@pytest.mark.parametrize("plugin", [True, False])
def test_compose_detects_plugin_or_falls_back(monkeypatch, plugin):
    monkeypatch.setattr(backup.shutil, "which", lambda program: f"/example/{program}")

    def version(args, **kwargs):
        assert args == ["/example/docker", "compose", "version"]
        assert kwargs["stdout"] == kwargs["stderr"] == backup.subprocess.DEVNULL
        if not plugin:
            raise backup.subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(backup.subprocess, "run", version)
    expected = ("/example/docker", "compose") if plugin else ("/example/docker-compose",)
    assert backup._compose_command.__wrapped__() == expected


def test_schema_catalog_filters_identifiers_and_prefixes_evaluation_materials():
    sha = hashlib.sha256(CONTENT).hexdigest()
    base = f"cases/case-1/ITEM/{sha}"
    fingerprints = SimpleNamespace(one=lambda: (1, sha))
    new_tables = {"intake_model_audits", "alembic_version", "checkpoint_migrations"}
    rows = [("public", table) for table in sorted(backup.PUBLIC_TABLES | new_tables)]
    rows += [
        *((EVAL_SCHEMA, table) for table in sorted(new_tables)),
        ("public", "arbitrary_extension_table"),
        (EVAL_SCHEMA, "business_materials"),
        (EVAL_SCHEMA, "agent_model_attempts"),
        (EVAL_SCHEMA, "eval_experiments"),
        (EVAL_SCHEMA, "business_orders;drop"),
        ("eval_wrong", "business_materials"),
        ('evil"."eval_' + "1" * 32, "business_materials"),
    ]
    seen = []

    def execute(query, params=None):
        sql = str(query)
        seen.append(sql)
        if "pg_catalog.pg_class" in sql:
            assert params == {"evaluation_schema": r"^eval_[0-9a-f]{32}$"}
            return rows
        if "pg_catalog.pg_attribute" in sql:
            return SimpleNamespace(scalar_one=lambda: True)
        if 'FROM "public"."chat_messages" WHERE sequence IS NULL' in sql:
            return SimpleNamespace(scalar_one=lambda: False)
        if "SELECT a.object_key" in sql and 'FROM "public"."chat_attachments"' in sql:
            return SimpleNamespace(mappings=list)
        if "SELECT object_key" in sql:
            return SimpleNamespace(
                mappings=lambda: [
                    {
                        "key": base,
                        "sha256": sha,
                        "size": len(CONTENT),
                        "content_type": "image/png",
                    }
                ]
            )
        return fingerprints

    connection = SimpleNamespace(execute=execute)
    found = backup._fingerprints(connection)
    assert {f"public.{table}" for table in new_tables} <= found.keys()
    assert set(found) == {f"public.{table}" for table in backup.PUBLIC_TABLES} | {
        f"{EVAL_SCHEMA}.business_materials",
        f"{EVAL_SCHEMA}.agent_model_attempts",
    }
    assert [row.key for row in backup._materials(connection)] == [
        base,
        f"evaluation/{EVAL_SCHEMA}/{base}",
    ]
    assert not any("eval_wrong" in sql or "business_orders;drop" in sql for sql in seen)
    with pytest.raises(backup.BackupError, match="DATABASE_IDENTIFIER_REJECTED"):
        backup._qualified("eval_wrong", "business_materials")


def test_v2_manifest_rejects_unbound_evaluation_material(package):
    _, manifest, _ = package
    value = manifest.model_dump(mode="json")
    del value["database"][f"{EVAL_SCHEMA}.business_materials"]
    with pytest.raises(ValueError, match="Material has no database fingerprint"):
        backup.BackupManifest.model_validate_json(json.dumps(value))


def test_v2_deduplicates_bytes_but_keeps_both_schema_keys(package):
    directory, manifest, _ = package
    assert manifest.format == backup.FORMAT
    assert [row.schema_name for row in manifest.materials] == ["public", EVAL_SCHEMA]
    assert len(list((directory / backup.OBJECTS).iterdir())) == 1


def test_failed_database_import_leaves_no_published_restore(package, monkeypatch):
    directory, _, _ = package
    identity = ("aftercare-v2-test", "500", "ac_test", "ac_test")
    database = f"ac_restore_{RESTORE_ID}"
    bucket = f"ac-restore-{RESTORE_ID}"
    monkeypatch.setattr(backup.secrets, "token_hex", lambda _: RESTORE_ID)
    monkeypatch.setattr(
        backup, "_create_db", lambda _, name: identity if name == database else None
    )
    monkeypatch.setattr(backup, "_verify_container", lambda *_: None)
    monkeypatch.setattr(
        backup, "restored_engine", lambda *_: pytest.fail("partial import verified")
    )

    def failed_import(profile, command, database_name, *flags, stdin):
        assert profile == "test" and command == "pg_restore" and database_name == database
        assert "--exit-on-error" in flags and "--single-transaction" not in flags
        assert stdin.read().startswith(b"PGDMP")
        raise backup.BackupError("PG_RESTORE_FAILED")

    monkeypatch.setattr(backup, "_container_pg", failed_import)
    with pytest.raises(backup.BackupError, match="RESTORE_FAILED PG_RESTORE_FAILED"):
        backup.restore_backup(settings(), directory)
    assert bucket not in FakeStore.buckets


def test_v2_manifest_without_later_optional_tables_remains_valid(package):
    _, manifest, _ = package
    value = manifest.model_dump(mode="json")
    for name in backup.PUBLIC_TABLES - backup.REQUIRED_PUBLIC_TABLES:
        del value["database"][f"public.{name}"]
    rebuilt = backup.BackupManifest.model_validate_json(json.dumps(value))
    assert rebuilt.format == backup.FORMAT
    assert rebuilt.materials == manifest.materials


@pytest.mark.parametrize(
    ("has_progress_table", "has_progress_column"),
    [(False, False), (True, True), (False, True), (True, False)],
)
def test_v2_fingerprints_accept_both_complete_schema_shapes(
    has_progress_table, has_progress_column
):
    catalog = [("public", table) for table in sorted(backup.REQUIRED_PUBLIC_TABLES)]
    if has_progress_table:
        catalog.append(("public", "eval_progress_events"))
    queries = []

    def execute(query, _params=None):
        sql = str(query)
        queries.append(sql)
        if "pg_catalog.pg_class" in sql:
            return catalog
        if "pg_catalog.pg_attribute" in sql:
            assert "'public.eval_experiments'::regclass" in sql and "'progress_seq'" in sql
            return SimpleNamespace(scalar_one=lambda: has_progress_column)
        return SimpleNamespace(one=lambda: (0, "0" * 64))

    connection = SimpleNamespace(execute=execute)
    if has_progress_table != has_progress_column:
        with pytest.raises(backup.BackupError, match="EVALUATION_PROGRESS_SCHEMA_MISMATCH"):
            backup._fingerprints(connection)
        assert not any("to_jsonb" in sql for sql in queries)
    else:
        found = backup._fingerprints(connection)
        assert ("public.eval_progress_events" in found) is has_progress_table
        experiment = next(sql for sql in queries if 'FROM "public"."eval_experiments"' in sql)
        assert "to_jsonb(t)" in experiment


def test_chat_attachments_have_separate_fingerprint_counts(package):
    _, manifest, material = package
    value = manifest.model_dump()
    chat = {**material.model_dump(), "key": f"conversations/conv-1/message-1/0-{material.sha256}"}
    value["materials"].append(chat)
    value["database"]["public.chat_attachments"]["rows"] = 1
    rebuilt = backup.BackupManifest.model_validate(value)
    assert rebuilt.materials[-1].table_name == "chat_attachments"
    value["database"]["public.chat_attachments"]["rows"] = 0
    with pytest.raises(ValueError, match="Material count"):
        backup.BackupManifest.model_validate(value)
    del value["database"]["public.chat_attachments"]
    with pytest.raises(ValueError, match="Incomplete conversation schema"):
        backup.BackupManifest.model_validate(value)


@pytest.mark.parametrize("key", [
    "conversations/../msg/0-", "conversations/c/msg/6-", "conversations/c/msg/-1-",
    "evaluation/eval_" + "2" * 32 + "/conversations/c/msg/0-",
])
def test_chat_attachment_key_rejects_traversal_and_unowned_scopes(key):
    with pytest.raises(ValueError, match="Unexpected material object key"):
        backup.MaterialRecord(key=key + "a" * 64, sha256="a" * 64, size=1, content_type="image/png")


def test_backup_refuses_incomplete_chat_upload_snapshot(monkeypatch):
    monkeypatch.setattr(backup, "_managed_tables", lambda conn: [("public", "chat_messages")])
    connection = SimpleNamespace(execute=lambda query: SimpleNamespace(scalar_one=lambda: True))
    with pytest.raises(backup.BackupError, match="CHAT_UPLOADS_PENDING"):
        backup._materials(connection)


def test_backup_enumerates_verified_chat_object(monkeypatch):
    sha = hashlib.sha256(CONTENT).hexdigest()
    row = {"key": f"conversations/c/m/0-{sha}", "sha256": sha,
           "size": len(CONTENT), "content_type": "image/png",
           "message_id": "m", "conversation_id": "c", "position": 0}
    monkeypatch.setattr(backup, "_managed_tables", lambda conn: [
        ("public", "chat_messages"), ("public", "chat_attachments")])
    def execute(query):
        if "SELECT EXISTS" in str(query):
            return SimpleNamespace(scalar_one=lambda: False)
        return SimpleNamespace(mappings=lambda: [row])
    assert [r.key for r in backup._materials(SimpleNamespace(execute=execute))] == [row["key"]]


def test_backup_rejects_partial_conversation_schema(package):
    _, manifest, _ = package
    value = manifest.model_dump()
    del value["database"]["public.chat_messages"]
    with pytest.raises(ValueError, match="Incomplete conversation schema"):
        backup.BackupManifest.model_validate(value)
    catalog = [("public", t) for t in backup.REQUIRED_PUBLIC_TABLES | {"chat_attachments"}]
    with pytest.raises(backup.BackupError, match="CHAT_SCHEMA_INCOMPLETE"):
        backup._managed_tables(SimpleNamespace(execute=lambda *args: catalog))


def test_backup_rejects_misbound_conversation_object(monkeypatch):
    sha = "a" * 64
    row = {"key": f"conversations/other/m/0-{sha}", "sha256": sha,
           "size": 1, "content_type": "image/png", "message_id": "m",
           "conversation_id": "c", "position": 0}
    monkeypatch.setattr(backup, "_managed_tables", lambda conn: [("public", "chat_attachments")])
    conn = SimpleNamespace(execute=lambda query: SimpleNamespace(mappings=lambda: [row]))
    with pytest.raises(backup.BackupError, match="CHAT_ATTACHMENT_SCOPE_MISMATCH"):
        backup._materials(conn)


def test_legacy_fingerprint_rejects_archive_containing_chat():
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(scalar_one=lambda: True))
    with pytest.raises(backup.BackupError, match="LEGACY_BACKUP_CONTAINS_CHAT"):
        backup._fingerprints(conn, format=backup.V1_FORMAT)


def test_chat_analysis_tables_are_an_optional_complete_backup_bundle(package):
    _, manifest, _ = package
    previous = manifest.model_dump()
    for table in backup.CHAT_BLIND_REVIEW_TABLES:
        previous["database"].pop(f"public.{table}")
    for table in backup.CHAT_ANALYSIS_TABLES | backup.CHAT_DECISION_TABLES | {backup.CHAT_INVESTIGATION_TABLE, backup.CHAT_MATERIAL_SOURCE_TABLE}:
        previous["database"].pop(f"public.{table}", None)
    assert backup.BackupManifest.model_validate(previous)

    current = backup.BackupManifest.model_validate(previous).model_dump()
    for table in backup.CHAT_ANALYSIS_TABLES:
        current["database"][f"public.{table}"] = {"rows": 0, "sha256": "a" * 64}
    assert backup.BackupManifest.model_validate(current)
    current["database"].pop("public.chat_observations")
    with pytest.raises(ValueError, match="Incomplete conversation analysis schema"):
        backup.BackupManifest.model_validate(current)

    catalog = [
        ("public", table)
        for table in backup.REQUIRED_PUBLIC_TABLES | backup.CHAT_TABLES | backup.CHAT_ANALYSIS_TABLES
    ]
    managed = backup._managed_tables(SimpleNamespace(execute=lambda *args: catalog))
    assert {("public", table) for table in backup.CHAT_ANALYSIS_TABLES} <= set(managed)
    catalog.remove(("public", "chat_observations"))
    with pytest.raises(backup.BackupError, match="CHAT_ANALYSIS_SCHEMA_INCOMPLETE"):
        backup._managed_tables(SimpleNamespace(execute=lambda *args: catalog))


def test_chat_decision_tables_are_optional_only_as_a_complete_bundle(package):
    _, manifest, _ = package
    legacy = manifest.model_dump()
    for table in backup.CHAT_BLIND_REVIEW_TABLES:
        legacy["database"].pop(f"public.{table}")
    for table in backup.CHAT_DECISION_TABLES | {backup.CHAT_INVESTIGATION_TABLE, backup.CHAT_MATERIAL_SOURCE_TABLE}:
        legacy["database"].pop(f"public.{table}", None)
    assert backup.BackupManifest.model_validate(legacy)
    current = manifest.model_dump()
    for table in backup.CHAT_ANALYSIS_TABLES | backup.CHAT_DECISION_TABLES:
        current["database"][f"public.{table}"] = {"rows": 0, "sha256": "a" * 64}
    assert backup.BackupManifest.model_validate(current)
    without_analysis = backup.BackupManifest.model_validate(current).model_dump()
    for table in backup.CHAT_ANALYSIS_TABLES:
        without_analysis["database"].pop(f"public.{table}")
    with pytest.raises(ValueError, match="Incomplete conversation decision schema"):
        backup.BackupManifest.model_validate(without_analysis)
    del current["database"]["public.chat_customer_decisions"]
    with pytest.raises(ValueError, match="Incomplete conversation decision schema"):
        backup.BackupManifest.model_validate(current)


def test_chat_investigation_fingerprint_requires_predecessor_schema(package):
    _, manifest, _ = package
    current = manifest.model_dump()
    for table in backup.CHAT_BLIND_REVIEW_TABLES:
        current["database"].pop(f"public.{table}")
    assert f"public.{backup.CHAT_INVESTIGATION_TABLE}" in current["database"]
    current["database"].pop(f"public.{backup.CHAT_MATERIAL_SOURCE_TABLE}")
    previous = backup.BackupManifest.model_validate(current).model_dump()
    previous["database"].pop(f"public.{backup.CHAT_INVESTIGATION_TABLE}")
    assert backup.BackupManifest.model_validate(previous)
    for table in backup.CHAT_DECISION_TABLES:
        current["database"].pop(f"public.{table}")
    with pytest.raises(ValueError, match="Incomplete conversation investigation schema"):
        backup.BackupManifest.model_validate(current)


def test_chat_material_source_fingerprint_requires_investigation_schema(package):
    _, manifest, _ = package
    current = manifest.model_dump()
    for table in backup.CHAT_BLIND_REVIEW_TABLES:
        current["database"].pop(f"public.{table}")
    assert f"public.{backup.CHAT_MATERIAL_SOURCE_TABLE}" in current["database"]
    previous = backup.BackupManifest.model_validate(current).model_dump()
    previous["database"].pop(f"public.{backup.CHAT_MATERIAL_SOURCE_TABLE}")
    assert backup.BackupManifest.model_validate(previous)
    current["database"].pop(f"public.{backup.CHAT_INVESTIGATION_TABLE}")
    with pytest.raises(ValueError, match="Incomplete conversation material source schema"):
        backup.BackupManifest.model_validate(current)


def test_chat_blind_review_fingerprints_are_optional_complete_group(package):
    _, manifest, _ = package
    current = manifest.model_dump()
    previous = manifest.model_dump()
    for table in backup.CHAT_BLIND_REVIEW_TABLES:
        previous["database"].pop(f"public.{table}")
    assert backup.BackupManifest.model_validate(previous)
    current["database"].pop("public.eval_chat_blind_exposures")
    with pytest.raises(ValueError, match="Incomplete chat blind review schema"):
        backup.BackupManifest.model_validate(current)
