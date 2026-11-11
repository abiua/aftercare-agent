"""Fail-closed PostgreSQL/S3 configuration checks without connecting to a database."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, table
from sqlalchemy.dialects import postgresql

from aftercare.evaluation.chat import pg_runtime
from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key

ATTESTATION = PgAttestation("1" * 32, "7351111683238127052", "owned-test", "2" * 64, 32)


def _settings(**changes):
    return SimpleNamespace(
        **{
            "profile": "test",
            "container": True,
            "model_enabled": False,
            "embedding_enabled": False,
            "object_endpoint": "http://objects-test:8333",
            "object_bucket": "owned-test",
            "database_url": "postgresql+psycopg://ac_test@pg-test:5432/ac_test",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"profile": "app"},
        {"container": False},
        {"model_enabled": True},
        {"embedding_enabled": True},
        {"chat_vision_enabled": True},
        {"object_endpoint": "http://objects-app:8333"},
        {"object_bucket": "unowned"},
    ],
)
def test_invalid_topology_is_rejected_before_engine_or_bucket_access(monkeypatch, changes):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unowned topology reached its database or bucket")

    monkeypatch.setattr(pg_runtime, "guarded_engine", unexpected)
    monkeypatch.setattr(pg_runtime, "S3ObjectStore", unexpected)
    monkeypatch.setenv("AFTERCARE_CONTAINER", "1")
    monkeypatch.setenv("AFTERCARE_PROFILE", "test")
    with (
        pytest.raises(ValueError, match="CHAT_EVAL_PG_DEDICATED_RUNTIME_REQUIRED"),
        open_pg_runtime(_settings(**changes), ATTESTATION, "owned-campaign"),
    ):
        pytest.fail("unowned runtime was opened")


def test_real_identity_requires_both_roles_and_the_pinned_cluster_system_id():
    identity = ATTESTATION.identity()
    pg_runtime._verify_identity(identity, ATTESTATION)
    for index in range(len(identity)):
        damaged = list(identity)
        damaged[index] = "wrong"
        with pytest.raises(ValueError, match="CHAT_EVAL_PG_IDENTITY_REJECTED"):
            pg_runtime._verify_identity(damaged, ATTESTATION)
    with pytest.raises(ValueError, match="CHAT_EVAL_PG_ATTESTATION_INVALID"):
        replace(ATTESTATION, system_identifier="other cluster")
    assert (
        sentinel_key(ATTESTATION.stack_id) == f"evaluation/p5-stack/{ATTESTATION.stack_id}/sentinel"
    )


def test_pg_freeze_rejects_an_injected_source_before_touching_it():
    runtime = pg_runtime._PgRuntime(
        _settings(), ATTESTATION, "owned-campaign", None, None, None, {}
    )
    foreign = SimpleNamespace(case_id="DAMAGE-ORDINARY", repetition=1, source=None)
    with pytest.raises(ValueError, match="CHAT_EVAL_PG_SOURCE_UNOWNED"):
        runtime.freeze(foreign)


def test_pg_freeze_accepts_only_the_owned_schema_and_sqlalchemy_alias(monkeypatch):
    namespace = "eval_" + "3" * 32
    settings = _settings()
    backend_storage = object()
    options = {"schema_translate_map": {None: namespace}}
    engine = SimpleNamespace(get_execution_options=lambda: options)
    storage = SimpleNamespace(prefix=f"evaluation/{namespace}/", storage=backend_storage)
    runtime = pg_runtime._PgRuntime(
        settings, ATTESTATION, "owned-campaign", None, backend_storage, None, {}
    )
    case = SimpleNamespace(
        case_id="DAMAGE-ORDINARY",
        repetition=1,
        source=SimpleNamespace(engine=engine, storage=storage, settings=settings),
    )
    claim = runtime._claim(namespace, source_sha256="4" * 64)
    runtime._sources[case.case_id, case.repetition] = engine, storage, namespace, claim
    verified = []
    monkeypatch.setattr(runtime, "_verify_owner", lambda *args: verified.append(args))
    monkeypatch.setattr(pg_runtime, "_freeze_case", lambda _: SimpleNamespace(record={}))

    assert runtime.freeze(case).record["source_namespace"] == namespace
    mapping = options["schema_translate_map"]
    select(table("chat_conversations")).compile(
        dialect=postgresql.dialect(),
        schema_translate_map=mapping,
        render_schema_translate=True,
    )
    assert mapping == {None: namespace, "_none": namespace}
    assert runtime.freeze(case).record["source_namespace"] == namespace

    for foreign in (
        {None: "public", "_none": namespace},
        {None: namespace, "_none": "public"},
        {None: namespace, "_none": namespace, "public": namespace},
        {"_none": namespace},
    ):
        options["schema_translate_map"] = foreign
        with pytest.raises(ValueError, match="CHAT_EVAL_PG_SOURCE_UNOWNED"):
            runtime.freeze(case)
    assert verified == [(namespace, claim), (namespace, claim)]


def test_recovery_requires_the_s3_claim_to_equal_the_postgresql_claim():
    class Storage:
        def get(self, key, _size):
            assert key.endswith("/.p5-owner.json")
            return b"wrong"

    runtime = pg_runtime._PgRuntime(
        _settings(), ATTESTATION, "owned-campaign", None, Storage(), None, {}
    )
    namespace = "eval_" + "3" * 32
    claim = runtime._claim(namespace, source_case="DAMAGE-ORDINARY")
    assert claim["object_prefix"] == f"evaluation/{namespace}/"
    with pytest.raises(ValueError, match="CHAT_EVAL_PG_OWNER_MISMATCH"):
        runtime._verify_object_claim(claim)
