import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import botocore.auth
import pytest
from sqlalchemy.engine import make_url

from aftercare import config
from aftercare.business.storage import S3ObjectStore

REVISION = "a" * 40


def write_private(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("AFTERCARE_CONTAINER_MODEL_MODE", raising=False)
    monkeypatch.delenv("AFTERCARE_EMBEDDING_REVISION", raising=False)
    monkeypatch.setenv("AFTERCARE_CONTAINER", "1")
    private = tmp_path / ".runtime"
    private.mkdir()
    write_private(
        private / "local.json",
        {
            profile: {
                "database_url": f"postgresql+psycopg://ac_{profile}:fixture@127.0.0.1:55450/ac_{profile}",
                "object_endpoint": f"http://127.0.0.1:{58350 if profile == 'app' else 58351}",
                "object_access_key": "fixture",
                "object_secret_key": "fixture",
                "redis_url": "redis://127.0.0.1:56379/0",
                "model_enabled": True,
                "model_config_path": "/example/native-models.json",
                "embedding_enabled": True,
                "embedding_model_path": "/example/native-embedding",
            }
            for profile in ("app", "test")
        },
    )
    return private


@pytest.fixture
def opt_in(runtime, monkeypatch):
    monkeypatch.setenv("AFTERCARE_CONTAINER_MODEL_MODE", "live-hybrid")
    monkeypatch.setenv("AFTERCARE_EMBEDDING_REVISION", REVISION)
    write_private(runtime / "model-secret.json", {"api_key": "synthetic-placeholder"})
    write_private(
        runtime / "models.json",
        {
            "version": "fixture",
            "models": [
                {
                    "alias": alias,
                    "provider": "fixture",
                    "model": f"fixture-{alias}",
                    "endpoint": "https://example.invalid/v1",
                    "api_key_env": "AFTERCARE_OPENAI_KEY",
                }
                for alias in ("economy", "analysis")
            ],
        },
    )
    snapshot = runtime.parent / "models" / REVISION
    (snapshot / "1_Pooling").mkdir(parents=True)
    for name in (
        "config.json",
        "modules.json",
        "1_Pooling/config.json",
        "model.safetensors",
        "tokenizer.json",
    ):
        (snapshot / name).write_text("fixture")
    return runtime, snapshot


@pytest.mark.parametrize("profile", ["app", "test"])
def test_default_container_discards_host_model_configuration(runtime, profile):
    settings = config.load_settings(profile)
    assert settings.container
    assert not settings.model_enabled and not settings.embedding_enabled
    assert settings.embedding_model_path is None
    assert settings.model_config_path == runtime / "models.json"
    assert make_url(settings.database_url).host == f"pg-{profile}"
    assert settings.object_endpoint == f"http://objects-{profile}:8333"
    assert settings.object_presign_endpoint == (
        f"http://127.0.0.1:{58350 if profile == 'app' else 58351}"
    )
    assert settings.redis_url == f"redis://redis-{profile}:6379/0"


def test_app_opt_in_resolves_readonly_mount_locations(opt_in):
    runtime, snapshot = opt_in
    settings = config.load_settings("app")
    assert settings.container and settings.model_enabled and settings.embedding_enabled
    assert settings.embedding_model_path == snapshot
    assert settings.model_config_path == runtime / "models.json"
    assert "synthetic-placeholder" not in repr(settings)


def test_native_host_settings_are_independent_of_overlay(opt_in, monkeypatch):
    monkeypatch.delenv("AFTERCARE_CONTAINER")
    settings = config.load_settings("test")
    assert not settings.container and settings.model_enabled and settings.embedding_enabled
    assert settings.embedding_model_path.name == "native-embedding"
    assert settings.object_presign_endpoint is None


def test_explicit_signing_endpoint_preserved_in_container(runtime):
    path = runtime / "local.json"
    data = json.loads(path.read_text())
    data["app"]["object_presign_endpoint"] = "https://objects.example.invalid/"
    write_private(path, data)
    settings = config.load_settings("app")
    assert settings.object_endpoint == "http://objects-app:8333"
    assert settings.object_presign_endpoint == "https://objects.example.invalid"


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://example.invalid",
        "http://user:must-not-leak@example.invalid",
        "https://example.invalid/?must-not-leak",
        "https://example.invalid/must-not-leak",
        "http://example.invalid:65536",
        "http://example.invalid:0",
        "http://",
    ],
)
def test_invalid_presign_endpoint_is_rejected_without_credential_exposure(runtime, endpoint):
    path = runtime / "local.json"
    data = json.loads(path.read_text())
    data["app"]["object_endpoint"] = endpoint
    write_private(path, data)
    with pytest.raises(ValueError, match="OBJECT_PRESIGN_ENDPOINT_REJECTED") as caught:
        config.load_settings("app")
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize("profile,port", [("app", 58350), ("test", 58351)])
def test_presign_signature_binds_to_external_host(runtime, monkeypatch, profile, port):
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: datetime(2026, 11, 10, 10))
    settings = config.load_settings(profile)
    store = S3ObjectStore(settings)
    internal = S3ObjectStore(settings.model_copy(update={"object_presign_endpoint": None}))
    native = S3ObjectStore(
        settings.model_copy(
            update={
                "object_endpoint": f"http://127.0.0.1:{port}",
                "object_presign_endpoint": None,
                "container": False,
            }
        )
    )
    key = "cases/example/ITEM/photo"
    url = store.presign(key, "image/png")
    parsed = urlsplit(url)
    assert store.client.meta.endpoint_url == f"http://objects-{profile}:8333"
    assert parsed.netloc == f"127.0.0.1:{port}"
    assert parsed.path == f"/{settings.object_bucket}/{key}"
    assert url == native.presign(key, "image/png")
    query = parse_qs(parsed.query)
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["response-content-type"] == ["image/png"]
    assert query["response-cache-control"] == ["private, no-store"]
    internal_query = parse_qs(urlsplit(internal.presign(key, "image/png")).query)
    assert internal_query["X-Amz-Signature"] != query["X-Amz-Signature"]


def test_opt_in_refuses_test_profile(opt_in):
    with pytest.raises(ValueError, match="^CONTAINER_MODEL_MODE_REJECTED$"):
        config.load_settings("test")


def test_opt_in_requires_a_pinned_local_snapshot(opt_in, monkeypatch):
    _, snapshot = opt_in
    monkeypatch.setenv("AFTERCARE_EMBEDDING_REVISION", "../outside")
    with pytest.raises(ValueError, match="^CONTAINER_EMBEDDING_REVISION_REQUIRED$"):
        config.load_settings("app")
    monkeypatch.setenv("AFTERCARE_EMBEDDING_REVISION", REVISION)
    (snapshot / "model.safetensors").unlink()
    with pytest.raises(ValueError, match="^CONTAINER_EMBEDDING_SNAPSHOT_REQUIRED$"):
        config.load_settings("app")


@pytest.mark.parametrize("filename", ["models.json", "model-secret.json"])
def test_opt_in_rejects_nonprivate_model_files(opt_in, filename):
    runtime, _ = opt_in
    (runtime / filename).chmod(0o644)
    with pytest.raises(ValueError, match="^CONTAINER_PRIVATE_MODEL_FILES_REQUIRED$"):
        config.load_settings("app")


def test_invalid_private_registry_error_is_redacted(opt_in):
    runtime, _ = opt_in
    write_private(runtime / "models.json", {"api_key": "should-never-appear-in-error"})
    with pytest.raises(ValueError, match="^CONTAINER_MODEL_REGISTRY_INVALID$") as caught:
        config.load_settings("app")
    assert "should-never-appear-in-error" not in str(caught.value)
    assert caught.value.__suppress_context__
