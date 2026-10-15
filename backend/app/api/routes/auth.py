"""Auth: short-lived access token (kept in browser memory) + rotating refresh token in an httpOnly cookie.

    XSS cannot read the refresh cookie (httpOnly); CSRF cannot use it: SameSite=Strict, path-scoped to
    /api/v1/auth, and /refresh + /logout require the custom X-BriefDesk header (cross-site forms can't
    set headers, and cross-origin fetches with custom headers are blocked by CORS - we allow no origins).
"""
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from ...core.config import get_settings
from ...core.ratelimit import client_ip, limiter
from ...core.security import create_access_token, hash_password, login_guard, verify_password
from ...db.models import User
from ...schemas import LoginIn, PasswordChangeIn, RegisterIn, TokenOut, UserOut
from ...services import sessions
from ..deps import DB, CurrentUser, SessionUser

router = APIRouter(prefix="/auth", tags=["auth"])
audit = logging.getLogger("audit")
COOKIE = "bd_refresh"
COOKIE_PATH = "/api/v1/auth"


def _csrf(x_briefdesk: str | None) -> None:
    if x_briefdesk != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-BriefDesk header")


def _issue(response: Response, db, user: User, refresh: str | None = None) -> TokenOut:
    s = get_settings()
    refresh = refresh or sessions.issue(db, user)
    response.set_cookie(COOKIE, refresh, max_age=s.refresh_token_days * 86400, httponly=True,
                        secure=s.secure_cookies, samesite="strict", path=COOKIE_PATH)
    access, expires_in = create_access_token(user.id, user.role)
    return TokenOut(access_token=access, expires_in=expires_in, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
@limiter.limit("20/minute")
def login(request: Request, response: Response, body: LoginIn, db: DB):
    email = body.email.lower()
    client = client_ip(request)
    guard_key = f"{email}|{client}"                     # per (email, IP): an attacker can't lock the owner out
    if wait := login_guard.locked_for(guard_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = db.scalar(select(User).where(User.email == email))
    if not verify_password(body.password, user.password_hash if user else None):   # constant time
        locked = login_guard.record_failure(guard_key)
        audit.warning("login failed", extra={"email": email, "client": client, "locked": locked})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    login_guard.record_success(guard_key)
    audit.info("login ok", extra={"user_id": user.id, "client": client})
    return _issue(response, db, user)


@router.post("/refresh", response_model=TokenOut)
@limiter.limit("60/minute")
def refresh(request: Request, response: Response, db: DB, x_briefdesk: str | None = Header(None)):
    _csrf(x_briefdesk)
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    try:
        user, new_refresh = sessions.rotate(db, token)
    except HTTPException:
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        raise
    return _issue(response, db, user, new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DB, x_briefdesk: str | None = Header(None)):
    _csrf(x_briefdesk)
    if token := request.cookies.get(COOKIE):
        sessions.revoke(db, token)
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def register(request: Request, response: Response, body: RegisterIn, db: DB):
    if not get_settings().allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign-up is disabled. Ask an admin for an account.")
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Please log in.")
    user = User(email=email, name=body.name.strip(), role="user", password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.info("user registered", extra={"user_id": user.id, "client": client_ip(request)})
    return _issue(response, db, user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
def change_password(request: Request, body: PasswordChangeIn, db: DB, user: SessionUser):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user.password_hash = hash_password(body.password)
    db.commit()
    sessions.revoke_user(db, user.id)                     # sign out every other session
    audit.info("password changed", extra={"user_id": user.id})
