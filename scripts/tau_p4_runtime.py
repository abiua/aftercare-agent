"""Launch the host application against only the isolated tau-chat-p4 stack."""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/tau-p4-demo"
PROFILE = "test"
API_PORT = 18224
FRONTEND_PORT = 5224


def configure(*, investigation=False, live=False):
    from aftercare import config

    if os.environ.get("AFTERCARE_CONTAINER") == "1":
        raise RuntimeError("P4 requires the local host runner")
    if live and not investigation:
        raise RuntimeError("Live P4 calls require an investigation worker")

    # Bind every app entry point to a distinct, checked PostgreSQL identity.
    config.TARGETS.clear()
    config.TARGETS.update({PROFILE: (15439, "tau_chat_p4", "tau_chat_p4", "tau-chat-p4")})
    os.environ["AFTERCARE_PROFILE"] = PROFILE
    os.environ["TAU_RAG_MILVUS_URI"] = "http://127.0.0.1:19538"

    def settings(profile=PROFILE):
        if profile != PROFILE:
            raise RuntimeError("P4 accepts only the isolated test configuration")
        return config.Settings(
            profile=PROFILE,
            demo_role_login_enabled=True,
            login_accounts_path=RUNTIME / "login-accounts.json",
            database_url="postgresql+psycopg://tau_chat_p4:tau_chat_p4_local@127.0.0.1:15439/tau_chat_p4",
            object_endpoint="http://127.0.0.1:18339",
            object_access_key="tau-chat-p4",
            object_secret_key="tau-chat-p4-local",
            object_bucket="tau-chat-p4-private",
            redis_url="redis://127.0.0.1:16389/0",
            chat_analysis_enabled=True,
            chat_decisions_enabled=True,
            chat_investigation_enabled=investigation,
            chat_investigation_protocol="compact-v7",
            chat_investigation_output_reserve=4096 if investigation else 1800,
            chat_investigation_issue_max_calls=10,
            chat_investigation_issue_max_microusd=350000,
            model_enabled=live,
            model_config_path=(RUNTIME / "live-registry.json")
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
    from verify_chat_p5_multimodal_source import _inputs

    seed, _, _, _ = _inputs()
    seed_path = RUNTIME / "data/v0.1/agent/business.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(seed, ensure_ascii=False, sort_keys=True)
    if seed_path.exists():
        if seed_path.read_text() != serialized:
            raise RuntimeError("Existing P4 seed differs")
    else:
        with seed_path.open("x") as output:
            output.write(serialized)
    (RUNTIME / ".runtime").mkdir(mode=0o700, exist_ok=True)
    command.upgrade(Config(str(ROOT / "backend/alembic.ini")), "head")
    manage.ROOT = RUNTIME
    manage.bootstrap(PROFILE)


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


def configure_live_registry():
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
            "version": "tau-p4-synthetic-chat",
            "models": [spec],
            "allowed_providers": ["openai"],
            "native_streaming": False,
            "max_attempts": 8,
            "max_run_attempts": 8,
            "max_usd": "0.35",
            "max_run_usd": "0.35",
        }
    )
    destination = RUNTIME / "live-registry.json"
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
            "worker",
            "beat",
        ],
    )
    parser.add_argument("--investigation", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live and args.action != "worker":
        parser.error("--live is only accepted for the isolated worker")
    configure(investigation=args.investigation, live=args.live)
    if args.action == "init":
        initialize()
    elif args.action == "index":
        index()
    elif args.action == "configure-live-registry":
        configure_live_registry()
    elif args.action == "verify-reference":
        verify_reference()
    elif args.action == "api":
        import uvicorn
        from aftercare.api import create_app

        uvicorn.run(create_app(PROFILE), host="127.0.0.1", port=API_PORT)
    else:
        from aftercare.queue import make_queue

        queue = make_queue(PROFILE)
        if args.action == "worker":
            queue.worker_main(
                ["worker", "--pool=solo", "--loglevel=INFO", "--hostname=tau-chat-p4@%h"]
            )
        else:
            queue.start(["beat", "--loglevel=INFO", "--schedule", str(RUNTIME / "beat")])


if __name__ == "__main__":
    main()
