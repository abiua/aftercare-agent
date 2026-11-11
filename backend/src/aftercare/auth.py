import hashlib
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.db import User

bearer = HTTPBearer(auto_error=False)


class UserContext(BaseModel):
    user_id: str
    role: Literal['customer', 'support', 'approver', 'tester']
    customer_id: str | None = None
    display_name: str = ''


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def current_user(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> UserContext:
    if credentials is None or not 32 <= len(credentials.credentials) <= 256:
        raise HTTPException(401, {'code':'AUTH_REQUIRED', 'message':'请填写本项目访问凭据。'})
    with Session(request.app.state.engine) as session:
        row = session.scalar(select(User).where(User.token_hash == token_hash(credentials.credentials), User.enabled.is_(True)))
        if row is None:
            raise HTTPException(401, {'code':'AUTH_REQUIRED','message':'访问凭据无效或已停用。'})
        return UserContext(user_id=row.id, role=row.role, customer_id=row.customer_id, display_name=row.display_name)
