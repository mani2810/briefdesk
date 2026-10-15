"""Admin: users, platform health (LLM gateway, MCP servers), usage and failed runs."""
import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from ...core.ratelimit import limiter
from ...core.security import hash_password
from ...db.models import Brief, Run, User, utcnow
from ...llm.gateway import llm_status
from ...schemas import AdminUserIn, AdminUserUpdate, UserOut
from ...services import sessions
from ...tools.mcp_client import current_hub
from ...tools.registry import get_registry
from ..deps import DB, Admin

router = APIRouter(prefix="/admin", tags=["admin"])
audit = logging.getLogger("audit")


@router.get("/overview")
def overview(db: DB, admin: Admin):
    since = utcnow() - timedelta(days=1)
    by_status = dict(db.execute(select(Brief.status, func.count(Brief.id)).group_by(Brief.status)).all())
    runs = dict(db.execute(select(Run.status, func.count(Run.id)).where(Run.created_at >= since)
                           .group_by(Run.status)).all())
    usage = db.execute(select(func.coalesce(func.sum(Run.llm_calls), 0), func.coalesce(func.sum(Run.tokens), 0))
                       .where(Run.created_at >= since)).one()
    hub = current_hub()
    return {
        "users": db.scalar(select(func.count(User.id))),
        "briefs": db.scalar(select(func.count(Brief.id))),
        "briefs_by_status": by_status,
        "runs_24h": runs,
        "llm_calls_24h": usage[0], "tokens_24h": usage[1],
        "llm": llm_status(),
        "mcp_servers": hub.status() if hub else [],
        "tools": [{"name": t.name, "origin": t.origin, "description": t.description[:160]}
                  for t in get_registry().list()],
    }


@router.get("/runs")
def recent_runs(db: DB, admin: Admin, status_filter: str | None = None, limit: int = 50):
    q = select(Run, Brief).join(Brief, Brief.id == Run.brief_id).order_by(Run.id.desc()).limit(min(limit, 200))
    if status_filter:
        q = q.where(Run.status == status_filter)
    return [{"id": r.id, "brief_id": b.id, "brief": b.title, "owner": b.owner.email, "kind": r.kind,
             "status": r.status, "error": r.error, "llm_calls": r.llm_calls, "tokens": r.tokens,
             "created_at": r.created_at.isoformat()} for r, b in db.execute(q).all()]


@router.get("/users", response_model=list[UserOut])
def list_users(db: DB, admin: Admin):
    return db.scalars(select(User).order_by(User.created_at)).all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
def create_user(request: Request, body: AdminUserIn, db: DB, admin: Admin):
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(email=email, name=body.name.strip(), role=body.role, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    audit.info("user created by admin", extra={"admin_id": admin.id, "user_id": user.id, "role": user.role})
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: AdminUserUpdate, db: DB, admin: Admin):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (body.is_active is False or body.role == "user"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable or demote yourself")
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            sessions.revoke_user(db, user.id)
    if body.role:
        user.role = body.role
    db.commit()
    audit.info("user updated by admin", extra={"admin_id": admin.id, "user_id": user.id,
                                               "changes": body.model_dump(exclude_none=True)})
    return user
