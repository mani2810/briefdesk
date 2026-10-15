"""Shared FastAPI dependencies: DB session, current user (JWT or personal API token), role guards.

Access model (deny by default):
    user    only their own briefs, files and API tokens
    admin   everything, plus /admin
    API tokens (bd_...) act as their user for briefs and files, but can never manage tokens, passwords
    or admin endpoints (a leaked token cannot escalate or lock the owner out).
"""
import hashlib
import logging
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.security import decode_access_token
from ..db.models import ApiToken, User, utcnow
from ..db.session import get_db

bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_db)]
audit = logging.getLogger("audit")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: DB,
                 creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    token = creds.credentials
    if token.startswith("bd_"):
        row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token),
                                               ApiToken.revoked_at.is_(None)))
        if row is None:
            raise unauthorized
        user = db.get(User, row.user_id)
        if not user or not user.is_active:
            raise unauthorized
        if not row.last_used_at or utcnow() - row.last_used_at > timedelta(minutes=5):
            row.last_used_at = utcnow()
            db.commit()
        request.state.auth = "api_token"
        return user
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise unauthorized
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise unauthorized
    request.state.auth = "session"
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def session_user(request: Request, user: CurrentUser) -> User:
    if getattr(request.state, "auth", "") != "session":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed with an API token")
    return user


def admin_user(user: Annotated[User, Depends(session_user)]) -> User:
    if user.role != "admin":
        audit.warning("forbidden", extra={"user_id": user.id, "needs": "admin"})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


SessionUser = Annotated[User, Depends(session_user)]
Admin = Annotated[User, Depends(admin_user)]
