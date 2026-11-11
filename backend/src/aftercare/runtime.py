"""Shared web/worker resources; migrations are an explicit deployment step."""
from contextlib import contextmanager
import json
import os

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from aftercare.business.service import BusinessService
from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine
from aftercare.intelligence.service import AgentService


@contextmanager
def open_runtime(profile="app", dispatch=None):
    settings = load_settings(profile)
    if settings.model_enabled and not os.environ.get("AFTERCARE_OPENAI_KEY"):
        secret_path = ROOT / ".runtime/model-secret.json"
        if secret_path.stat().st_mode & 0o077:
            raise ValueError("MODEL_SECRET_PERMISSIONS_REJECTED")
        os.environ["AFTERCARE_OPENAI_KEY"] = json.loads(secret_path.read_text())["api_key"]
    engine = guarded_engine(settings)
    url = make_url(settings.database_url).set(drivername="postgresql")
    try:
        with ConnectionPool(
            conninfo=url.render_as_string(hide_password=False), min_size=1, max_size=5,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        ) as pool:
            pool.wait(timeout=10)
            saver = PostgresSaver(pool)
            business = BusinessService(engine, settings)
            agent = AgentService(engine, settings, business, saver, dispatch=dispatch)
            agent.checkpointer = saver
            yield settings, engine, business, agent
    finally:
        engine.dispose()
