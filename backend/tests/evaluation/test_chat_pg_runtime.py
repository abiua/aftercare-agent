"""Fail-closed PostgreSQL/S3 configuration checks without connecting to a database."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

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
