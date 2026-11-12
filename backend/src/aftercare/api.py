import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from aftercare.auth import UserContext, current_user
from aftercare.conversations.service import ConversationService
from aftercare.evaluation.service import EvaluationService, require_current_code
from aftercare.runtime import open_runtime


def create_app(profile=None, *, dispatch=None):
    profile = profile or os.environ.get("AFTERCARE_PROFILE", "app")
    if dispatch is None:
        from aftercare.queue import publisher
        dispatch = publisher(profile)

    @asynccontextmanager
    async def lifespan(app):
        require_current_code()
        with open_runtime(profile, dispatch=dispatch) as resources:
            settings, engine, business, agent = resources
            app.state.settings = settings
            app.state.engine = engine
            app.state.business = business
            app.state.conversations = ConversationService(business)
            app.state.agent = agent
            app.state.intake_classifier = None
            if agent.model is not None and agent.model.registry.provider_limits:
                from decimal import Decimal
                from aftercare.intake import BoundedIntakeClassifier
                from aftercare.intake_audit import IntakeAuditFactory
                app.state.intake_classifier = BoundedIntakeClassifier(
                    agent.model,
                    IntakeAuditFactory(engine, provider_limiter=agent.provider_limiter),
                    max_usd=Decimal('0.02'),
                )
            from aftercare.queue import publisher
            app.state.evaluation = EvaluationService(
                engine, settings, model=agent.model,
                dispatch=publisher(profile, task_name='aftercare.evaluate'),
            )
            yield

    app = FastAPI(title="Aftercare", version="0.1.0", lifespan=lifespan)

    @app.get('/api/healthz')
    def health():
        return {'status': 'alive'}

    @app.get('/api/readyz')
    def ready():
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from fastapi.responses import JSONResponse
        from sqlalchemy import text
        from aftercare.config import ROOT
        try:
            directory = ScriptDirectory.from_config(Config(str(ROOT / 'backend/alembic.ini')))
            with app.state.engine.connect() as conn:
                applied = set(conn.scalars(text('SELECT version_num FROM alembic_version')))
                if applied != set(directory.get_heads()):
                    return JSONResponse({'status': 'not_ready', 'code': 'MIGRATION_REQUIRED'}, 503)
            return {'status': 'ready'}
        except Exception:
            return JSONResponse({'status': 'not_ready', 'code': 'DATABASE_UNAVAILABLE'}, 503)

    @app.get("/api/me")
    def me(user: Annotated[UserContext, Depends(current_user)]):
        return {"schema_version": "ac-v1", **user.model_dump()}

    @app.get("/api/runtime")
    def runtime(user: Annotated[UserContext, Depends(current_user)]):
        agent = app.state.agent
        models = []
        if agent.model is not None:
            models = [{"alias": m.alias, "model": m.model, "available": m.healthy and m.compatibility == "VERIFIED" and bool(os.environ.get(m.api_key_env)), "compatibility": m.compatibility, "configured": bool(os.environ.get(m.api_key_env))}
                      for m in agent.model.registry.models]
        return {"schema_version": "ac-v1", "mode": "live" if agent.model else "offline",
                "models": models, "features": {"multi_agent": True,
                "persistent_checkpoints": True, "simulated_payments": True}}

    from aftercare.business.api import router as business_router
    from aftercare.conversations.api import router as conversations_router
    from aftercare.intelligence.api import router as agent_router
    from aftercare.evaluation.api import router as evaluation_router
    from aftercare.intake import router as intake_router
    from aftercare.password_login import router as login_router
    app.include_router(login_router, prefix="/api")
    app.include_router(business_router, prefix="/api")
    app.include_router(conversations_router, prefix="/api")
    app.include_router(agent_router, prefix="/api")
    app.include_router(evaluation_router, prefix="/api")
    app.include_router(intake_router, prefix="/api")
    return app
