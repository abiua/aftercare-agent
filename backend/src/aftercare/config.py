import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[3]
TARGETS = {
    'app': (55450, 'ac_app', 'ac_app', 'aftercare-v2-app'),
    'test': (55451, 'ac_test', 'ac_test', 'aftercare-v2-test'),
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)
    profile: Literal['app', 'test'] = 'app'
    database_url: str = Field(repr=False)
    object_endpoint: str
    object_presign_endpoint: str | None = Field(default=None, repr=False)
    object_access_key: str = Field(repr=False)
    object_secret_key: str = Field(repr=False)
    object_bucket: str = 'aftercare-private'
    demo_role_login_enabled: bool = False
    login_accounts_path: Path | None = None
    model_config_path: Path = ROOT / '.runtime/models.json'
    model_enabled: bool = False
    redis_url: str
    embedding_model_path: Path | None = None
    embedding_enabled: bool = False
    container: bool = False
    chat_demo_product_id: str | None = None
    chat_text_enabled: bool = False
    chat_analysis_enabled: bool = False
    chat_decisions_enabled: bool = False
    chat_investigation_enabled: bool = False
    # Ordinary chat investigations get an independent review before their outcome is published.
    chat_investigation_kernel_review: bool = False
    chat_tau_policy_enabled: bool = False
    chat_tau_policy_document: Path = ROOT / ".runtime/tau-p2-root-20261112/policy"
    chat_tau_authorized_policy_catalog: Path | None = None
    chat_tau_policy_catalog: Path = ROOT / ".runtime/tau-rag-p3/catalog-milvus-resume.json"
    chat_tau_policy_model_cache: Path = ROOT / ".runtime/tau-rag-p3/models"
    chat_investigation_output_reserve: Literal[1800, 4096] = 1800
    chat_investigation_protocol: Literal["compact-v6", "compact-v7"] = "compact-v6"
    chat_investigation_direct_recheck_request: bool = False
    chat_investigation_run_max_calls: int = Field(default=8, strict=True, ge=1, le=12)
    chat_investigation_issue_max_calls: int = Field(default=16, strict=True, ge=1, le=144)
    chat_investigation_issue_max_microusd: int = Field(default=200_000, strict=True, ge=1, le=10_000_000)
    chat_vision_enabled: bool = False
    chat_vision_alias: Literal["economy", "analysis"] = "analysis"
    chat_analysis_max_calls: int = Field(default=2, strict=True, ge=1, le=6)
    chat_analysis_max_microusd: int = Field(default=20_000, strict=True, ge=1, le=100_000)

    @field_validator("object_presign_endpoint")
    @classmethod
    def validate_object_presign_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            endpoint = urlsplit(value)
            valid = (
                endpoint.scheme in {"http", "https"}
                and bool(endpoint.hostname)
                and endpoint.username is None
                and endpoint.path in {"", "/"}
                and not re.search(r"[\s\x00-\x1f\x7f?#\\]", value)
                and (endpoint.port is None or endpoint.port > 0)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("OBJECT_PRESIGN_ENDPOINT_REJECTED")
        return value.rstrip("/")


def _container_model_settings(profile: str) -> dict:
    mode = os.environ.get("AFTERCARE_CONTAINER_MODEL_MODE", "offline")
    registry_path = ROOT / ".runtime/models.json"
    if mode == "offline":
        return {
            "model_enabled": False,
            "model_config_path": registry_path,
            "embedding_enabled": False,
            "embedding_model_path": None,
        }
    if mode != "live-hybrid" or profile != "app":
        raise ValueError("CONTAINER_MODEL_MODE_REJECTED")
    revision = os.environ.get("AFTERCARE_EMBEDDING_REVISION", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("CONTAINER_EMBEDDING_REVISION_REQUIRED")
    embedding_path = ROOT / "models" / revision
    model_files = ("config.json", "modules.json", "1_Pooling/config.json")
    if (
        not all((embedding_path / name).is_file() for name in model_files)
        or not any((embedding_path / name).is_file() for name in ("model.safetensors", "pytorch_model.bin"))
        or not any((embedding_path / name).is_file() for name in ("tokenizer.json", "vocab.txt"))
    ):
        raise ValueError("CONTAINER_EMBEDDING_SNAPSHOT_REQUIRED")

    secret_path = ROOT / ".runtime/model-secret.json"
    for path in (registry_path, secret_path):
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("CONTAINER_PRIVATE_MODEL_FILES_REQUIRED")
    try:
        secret = json.loads(secret_path.read_text())
    except (OSError, ValueError):
        raise ValueError("CONTAINER_MODEL_SECRET_INVALID") from None
    key = secret.get("api_key") if isinstance(secret, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("CONTAINER_MODEL_SECRET_INVALID")

    from aftercare.intelligence.models import Registry

    try:
        registry = Registry.read(registry_path)
    except (OSError, ValueError):
        raise ValueError("CONTAINER_MODEL_REGISTRY_INVALID") from None
    if not registry.models or any(spec.api_key_env != "AFTERCARE_OPENAI_KEY" for spec in registry.models):
        raise ValueError("CONTAINER_MODEL_CREDENTIAL_REFERENCE_REJECTED")
    return {
        "model_enabled": True,
        "model_config_path": registry_path,
        "embedding_enabled": True,
        "embedding_model_path": embedding_path,
    }


def load_settings(profile='app'):
    data = json.loads((ROOT / '.runtime/local.json').read_text())
    values = data[profile].copy()
    if os.environ.get('AFTERCARE_CONTAINER') == '1':
        from sqlalchemy.engine import make_url

        values['database_url'] = make_url(values['database_url']).set(
            host=f'pg-{profile}', port=5432).render_as_string(hide_password=False)
        if values.get('object_presign_endpoint') is None:
            values['object_presign_endpoint'] = values['object_endpoint']
        values['object_endpoint'] = f'http://objects-{profile}:8333'
        values['redis_url'] = f'redis://redis-{profile}:6379/0'
        values.update(container=True, **_container_model_settings(profile))
    return Settings(profile=profile, **values)
