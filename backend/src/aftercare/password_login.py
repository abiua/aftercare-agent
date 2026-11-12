"""Local demo account login; business permissions still come from the user table."""
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.auth import token_hash
from aftercare.db import User

router = APIRouter()
_attempts = deque()
_lock = threading.Lock()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ':' + result.hex()


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post('/auth/login')
def login(body: Login, request: Request):
    # A bounded process-local limiter is sufficient for this loopback demo server.
    with _lock:
        now = time.monotonic()
        while _attempts and _attempts[0] < now - 60:
            _attempts.popleft()
        if len(_attempts) >= 30:
            raise HTTPException(429, {'code': 'LOGIN_RATE_LIMIT', 'message': '登录尝试过多，请稍后重试。'})
        _attempts.append(now)
    path = getattr(request.app.state.settings, 'login_accounts_path', None)
    try:
        accounts = json.loads(path.read_text()) if path else []
    except (OSError, ValueError):
        raise HTTPException(503, {'code': 'LOGIN_UNAVAILABLE', 'message': '演示账号暂不可用。'}) from None
    account = next((row for row in accounts if row['username'] == body.username.strip()), None)
    stored = account['password_hash'] if account else ('00' * 16 + ':' + '00' * 64)
    valid = hmac.compare_digest(password_hash(body.password, stored.split(':')[0]), stored)
    if valid and account:
        with Session(request.app.state.engine) as db:
            user = db.scalar(select(User).where(User.id == account['user_id'],
                User.token_hash == token_hash(account['token']), User.enabled.is_(True)))
            if user:
                return JSONResponse({'token': account['token']}, headers={'Cache-Control': 'no-store'})
    raise HTTPException(401, {'code': 'LOGIN_FAILED', 'message': '账号或密码不正确。'})


class DemoRoleLogin(BaseModel):
    role: Literal['customer', 'support', 'approver', 'tester']


@router.post('/auth/demo-role')
def demo_role_login(body: DemoRoleLogin, request: Request):
    if not getattr(request.app.state.settings, 'demo_role_login_enabled', False):
        raise HTTPException(404, {'code': 'DEMO_LOGIN_DISABLED', 'message': '演示登录未开启。'})
    aliases = {'customer': 'customer', 'support': 'support', 'approver': 'finance', 'tester': 'tester'}
    path = request.app.state.settings.login_accounts_path
    try:
        accounts = json.loads(path.read_text()) if path else []
    except (OSError, ValueError):
        raise HTTPException(503, {'code': 'LOGIN_UNAVAILABLE', 'message': '演示账号暂不可用。'}) from None
    account = next((row for row in accounts if row['username'] == aliases[body.role]), None)
    if account:
        with Session(request.app.state.engine) as db:
            user = db.scalar(select(User).where(User.id == account['user_id'],
                User.role == body.role, User.enabled.is_(True),
                User.token_hash == token_hash(account['token'])))
            if user:
                return JSONResponse({'token': account['token']}, headers={'Cache-Control': 'no-store'})
    raise HTTPException(503, {'code': 'LOGIN_UNAVAILABLE', 'message': '该演示角色暂不可用。'})
