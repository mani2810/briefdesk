"""Personal API tokens (for the BriefDesk MCP server and scripts) and the public app metadata."""
import logging
import secrets

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ...core.config import get_settings
from ...db.models import ApiToken, utcnow
from ...llm.gateway import llm_status
from ...renderers import FORMATS
from ...schemas import ApiTokenIn
from ...tools.mcp_client import current_hub
from ..deps import DB, SessionUser, hash_token

router = APIRouter(tags=["account"])
audit = logging.getLogger("audit")
MAX_TOKENS = 10


def _token_dict(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at.isoformat(),
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None}


@router.get("/tokens")
def list_tokens(db: DB, user: SessionUser):
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
                      .order_by(ApiToken.created_at.desc()))
    return [_token_dict(t) for t in rows]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
def create_token(body: ApiTokenIn, db: DB, user: SessionUser):
    active = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))).all()
    if len(active) >= MAX_TOKENS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"At most {MAX_TOKENS} active tokens - revoke one first")
    token = "bd_" + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user.id, name=body.name.strip(), prefix=token[:9], token_hash=hash_token(token))
    db.add(row)
    db.commit()
    audit.info("api token created", extra={"user_id": user.id, "token_id": row.id})
    return {**_token_dict(row), "token": token}          # shown ONCE; only the hash is stored


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(token_id: int, db: DB, user: SessionUser):
    row = db.get(ApiToken, token_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")
    row.revoked_at = utcnow()
    db.commit()
    audit.info("api token revoked", extra={"user_id": user.id, "token_id": token_id})


@router.get("/meta")
def meta():
    """Public, non-sensitive facts the login screen and the composer need."""
    s = get_settings()
    llm = llm_status()
    hub = current_hub()
    return {"app": s.app_name, "allow_registration": s.allow_registration, "llm_ready": llm.get("ready", False),
            "llm_mode": s.llm_mode, "llm_problem": llm.get("problem"),
            "formats": [{"format": k, "label": v.label, "family": v.family} for k, v in FORMATS.items()],
            "mcp_tools": [t.name for t in hub.tools()] if hub else []}
