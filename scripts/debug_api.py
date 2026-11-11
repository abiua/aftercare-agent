"""Loopback-only business debugger with a disposable database and credentials."""

import argparse
import json
import os
import pdb
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aftercare.auth import UserContext, current_user, token_hash
from aftercare.business.api import router as business_router
from aftercare.business.errors import BusinessError
from aftercare.business.service import BusinessService, initialize
from aftercare.config import ROOT, Settings
from aftercare.db import Base, User

HOST, PORT = "127.0.0.1", 18092
BUSINESS_ENDPOINTS = {"orders", "order", "cases", "create_case", "case_detail", "events"}


class DebugBusinessService(BusinessService):
    debugger = Lock()

    def create_case(self, user, **values):
        if not self.debugger.acquire(blocking=False):
            raise BusinessError("DEBUGGER_BUSY", "请先在终端继续正在调试的请求。", 409)
        try:
            # Start the debugger on the actual production method in the request thread.
            result = pdb.Pdb(readrc=False).runcall(super().create_case, user, **values)
            if result is None:
                raise BusinessError("DEBUGGER_ABORTED", "已在调试终端中止请求，请核对工单列表。", 409)
            return result
        finally:
            self.debugger.release()


def create_debug_app(directory: Path, *, editor: bool = False):
    database_url = URL.create("sqlite", database=str(directory / "business.sqlite"))
    engine = create_engine(database_url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    data = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    initialize(engine, data)
    accounts = [
        {
            "user_id": f"debug-{customer_id}",
            "role": "customer",
            "customer_id": customer_id,
            "display_name": f"调试客户 {customer_id}",
            "token": secrets.token_urlsafe(32),
        }
        for customer_id in ("C001", "C002")
    ]
    with Session(engine) as session, session.begin():
        for account in accounts:
            session.add(
                User(
                    id=account["user_id"], role=account["role"],
                    customer_id=account["customer_id"], display_name=account["display_name"],
                    token_hash=token_hash(account["token"]), enabled=True,
                )
            )
    token_file = directory / "users-debug.json"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(accounts, stream, ensure_ascii=False, indent=2)

    settings = Settings(
        profile="test", database_url=str(database_url), object_endpoint="disabled",
        object_access_key="unused", object_secret_key="unused", redis_url="disabled",
    )
    app = FastAPI(title="Aftercare business debugger", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST])
    app.state.engine = engine
    # Only these routes are mounted. They do not call object storage or payments.
    service = BusinessService if editor else DebugBusinessService
    app.state.business = service(engine, settings, storage=object())
    app.include_router(
        APIRouter(routes=[route for route in business_router.routes if route.name in BUSINESS_ENDPOINTS]),
        prefix="/api",
    )

    @app.get("/api/me")
    def me(user: Annotated[UserContext, Depends(current_user)]):
        return {"schema_version": "ac-v1", **user.model_dump()}

    @app.get("/api/runtime")
    def runtime(_user: Annotated[UserContext, Depends(current_user)]):
        return {"schema_version": "ac-v1", "mode": "offline", "models": [], "features": {}}

    return app, token_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", action="store_true", help="Use the editor debugger instead of pdb")
    parser.add_argument("--port", type=int, default=PORT, help="Loopback port (default: %(default)s)")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if not args.editor and not sys.stdin.isatty():
        raise SystemExit("Run this command in an interactive terminal for pdb.")
    os.umask(0o077)
    with TemporaryDirectory(prefix="aftercare-debug-") as tmp:
        app, token_file = create_debug_app(Path(tmp), editor=args.editor)
        print(f"Private debug credentials file: {token_file}", flush=True)
        message = (
            "Set a breakpoint in BusinessService.create_case."
            if args.editor
            else "Submitting a case pauses this terminal."
        )
        print(f"Debug API: http://{HOST}:{args.port}. {message}", flush=True)
        try:
            uvicorn.run(app, host=HOST, port=args.port, access_log=False)
        finally:
            app.state.engine.dispose()


if __name__ == "__main__":
    main()
