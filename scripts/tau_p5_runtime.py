"""Launch the host application against only the isolated tau-chat-p5 stack."""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/tau-p5-demo"
PROFILE = "test"
API_PORT = 18225
FRONTEND_PORT = 5225
CONTINUATION_TIMEOUT = 90
CONTINUATION_DEADLINE = 240
CONTINUATION_MAX_TOTAL_CALLS = 81
CONTINUATION_MAX_TOTAL_MICROUSD = 3_000_000
TIMEOUT_CHOICES = (25, 30, CONTINUATION_TIMEOUT)


def live_registry_path(timeout_seconds, max_attempts):
    if max_attempts == 12 and timeout_seconds in (25, 30):
        return RUNTIME / (
            "live-registry.json" if timeout_seconds == 25 else "live-registry-30s.json"
        )
    return RUNTIME / f"live-registry-{timeout_seconds}s-{max_attempts}calls.json"


def configure(*, investigation=True, live=False, timeout_seconds=25, max_attempts=12):
    from aftercare import config

    if os.environ.get("AFTERCARE_CONTAINER") == "1":
        raise RuntimeError("P5 requires the local host runner")
    if live and not investigation:
        raise RuntimeError("Live P5 calls require explicit investigation scope")

    continuation = live and timeout_seconds == CONTINUATION_TIMEOUT

    # Bind every app entry point to a distinct, checked PostgreSQL identity.
    config.TARGETS.clear()
    config.TARGETS.update({PROFILE: (15440, "tau_chat_p5", "tau_chat_p5", "tau-chat-p5")})
    os.environ["AFTERCARE_PROFILE"] = PROFILE
    os.environ["TAU_RAG_MILVUS_URI"] = "http://127.0.0.1:19538"

    def settings(profile=PROFILE):
        if profile != PROFILE:
            raise RuntimeError("P5 accepts only the isolated test configuration")
        return config.Settings(
            profile=PROFILE,
            demo_role_login_enabled=True,
            login_accounts_path=RUNTIME / "login-accounts.json",
            database_url="postgresql+psycopg://tau_chat_p5:tau_chat_p5_local@127.0.0.1:15440/tau_chat_p5",
            object_endpoint="http://127.0.0.1:18340",
            object_access_key="tau-chat-p5",
            object_secret_key="tau-chat-p5-local",
            object_bucket="tau-chat-p5-private",
            redis_url="redis://127.0.0.1:16390/0",
            chat_analysis_enabled=True,
            chat_decisions_enabled=True,
            chat_investigation_enabled=investigation,
            chat_investigation_protocol="compact-v7",
            chat_investigation_output_reserve=4096 if investigation else 1800,
            chat_investigation_direct_recheck_request=continuation,
            chat_investigation_run_max_calls=12 if continuation else 8,
            chat_investigation_issue_max_calls=CONTINUATION_MAX_TOTAL_CALLS if continuation else 24,
            chat_investigation_issue_max_microusd=(
                CONTINUATION_MAX_TOTAL_MICROUSD if continuation else 1_400_000
            ),
            model_enabled=live,
            model_config_path=live_registry_path(timeout_seconds, max_attempts)
            if live
            else ROOT / ".runtime/models.json",
            chat_tau_policy_enabled=True,
            chat_tau_policy_document=ROOT / ".runtime/tau-p2-root-20261112/policy",
            chat_tau_authorized_policy_catalog=RUNTIME / "authorized-policy-catalog.json",
            chat_tau_policy_catalog=ROOT / ".runtime/tau-rag-p3/catalog-milvus-resume.json",
            chat_tau_policy_model_cache=ROOT / ".runtime/tau-rag-p3/models",
        )

    config.load_settings = settings


def initialize():
    from aftercare import manage
    from alembic import command
    from alembic.config import Config
    seed_path = RUNTIME / "data/v0.1/agent/business.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep existing demo business records. Retired evaluation cases must not be
    # reloaded or regenerated when restarting the application.
    if not seed_path.exists():
        seed = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
        serialized = json.dumps(seed, ensure_ascii=False, sort_keys=True)
        with seed_path.open("x") as output:
            output.write(serialized)
    (RUNTIME / ".runtime").mkdir(mode=0o700, exist_ok=True)
    command.upgrade(Config(str(ROOT / "backend/alembic.ini")), "head")
    manage.ROOT = RUNTIME
    manage.bootstrap(PROFILE)
    setup_demo_roles()


def setup_demo_roles():
    from secrets import token_urlsafe

    from aftercare.password_login import password_hash

    path = RUNTIME / "login-accounts.json"
    if path.exists():
        return
    credentials = json.loads((RUNTIME / ".runtime/users-test.json").read_text())
    aliases = {
        "customer": "customer",
        "support": "support",
        "approver": "finance",
        "tester": "tester",
    }
    accounts = [
        {
            "username": aliases[row["role"]],
            "user_id": row["user_id"],
            "token": row["token"],
            "password_hash": password_hash(token_urlsafe(32)),
        }
        for row in credentials
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(accounts, output)


def index():
    from aftercare.business.service import BusinessService
    from aftercare.config import load_settings
    from aftercare.conversations.tau_policy import build_authorized_catalog
    from aftercare.db import guarded_engine

    settings = load_settings(PROFILE)
    engine = guarded_engine(settings)
    try:
        catalog = build_authorized_catalog(
            settings, BusinessService(engine, settings).all_policy_versions()
        )
        print(json.dumps(catalog, ensure_ascii=False))
    finally:
        engine.dispose()


def configure_live_registry(timeout_seconds, max_attempts):
    from aftercare.intelligence.models import Registry

    base = Registry.read(ROOT / ".runtime/models.json")
    (analysis,) = [spec for spec in base.models if spec.alias == "analysis"]
    if (
        analysis.provider != "openai"
        or analysis.api_key_env != "AFTERCARE_OPENAI_KEY"
        or analysis.compatibility != "VERIFIED"
        or not analysis.tool_calls
    ):
        raise RuntimeError("Expected a verified analysis model with tool calls")
    spec = analysis.model_dump(mode="json")
    spec.update(data_scopes=["customer_conversation"], output_tokens=4096, fallback_aliases=[])
    registry = Registry.model_validate(
        {
            "version": f"tau-p5-synthetic-chat-{timeout_seconds}s",
            "models": [spec],
            "allowed_providers": ["openai"],
            "native_streaming": False,
            "timeout_seconds": timeout_seconds,
            "deadline_seconds": CONTINUATION_DEADLINE
            if timeout_seconds == CONTINUATION_TIMEOUT
            else 120,
            "max_attempts": max_attempts,
            "max_run_attempts": max_attempts,
            "max_usd": "0.70",
            "max_run_usd": "0.70",
        }
    )
    destination = live_registry_path(timeout_seconds, max_attempts)
    staged = destination.with_suffix(".json.pending")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(registry.model_dump_json(indent=2))
    staged.replace(destination)


def verify_reference():
    from aftercare.config import load_settings
    from aftercare.conversations.tau_policy import search_reference

    audit, hits = search_reference(load_settings(PROFILE), "Can I return a delivered order?")
    if audit["status"] != "RETRIEVED" or not hits:
        raise RuntimeError(f"Retained P3 reference retrieval {audit['status']}")
    print(json.dumps(audit, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init",
            "index",
            "verify-reference",
            "configure-live-registry",
            "api",
        ],
    )
    parser.add_argument("--timeout-seconds", type=int, choices=TIMEOUT_CHOICES, default=25)
    parser.add_argument("--max-attempts", type=int, choices=(9, 12), default=12)
    args = parser.parse_args()
    configure()
    if args.action == "init":
        initialize()
    elif args.action == "index":
        index()
    elif args.action == "configure-live-registry":
        configure_live_registry(args.timeout_seconds, args.max_attempts)
    elif args.action == "verify-reference":
        verify_reference()
    elif args.action == "api":
        import uvicorn
        from aftercare.api import create_app

        uvicorn.run(create_app(PROFILE), host="127.0.0.1", port=API_PORT)


if __name__ == "__main__":
    main()
