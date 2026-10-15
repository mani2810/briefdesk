"""Briefs: one conversation + one evolving report each. REST mirrors the WebSocket actions so scripts and
the MCP server can do everything the browser can."""
import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import delete, select

from ...core.config import get_settings
from ...core.ratelimit import limiter
from ...core.security import create_ws_ticket
from ...db.models import ACTIVE_STATUSES, Brief, Event, File, Run
from ...schemas import BriefCreate, BriefUpdate, ExportIn, MessageIn, PlanApproveIn
from ...services import briefs as svc
from ...services import chat
from ...services.runner import get_runner
from ..deps import DB, CurrentUser

router = APIRouter(tags=["briefs"])
audit = logging.getLogger("audit")


def _full(db, b: Brief) -> dict:
    out = svc.brief_dict(b, db)
    out["sources"] = svc.sources(db, b.id)
    out["files"] = svc.files(db, b.id)
    out["citations"] = svc.citations(db, b.id, b.version) if b.version else []
    out["versions"] = [{k: v for k, v in x.items() if k != "markdown"} for x in svc.versions(db, b.id)]
    return out


@router.get("/briefs")
def list_briefs(db: DB, user: CurrentUser, all: bool = False, limit: int = Query(100, le=500)):
    q = select(Brief).order_by(Brief.updated_at.desc()).limit(limit)
    if not (all and user.role == "admin"):
        q = q.where(Brief.owner_id == user.id)
    return [svc.brief_dict(b) for b in db.scalars(q)]


@router.post("/briefs", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_brief(request: Request, body: BriefCreate, user: CurrentUser):
    brief = await asyncio.to_thread(chat.create_brief, user, body.audience, body.depth, body.auto_approve)
    if body.topic and body.topic.strip():
        await asyncio.to_thread(chat.handle_message, user, brief.id, body.topic)
    from ...db.session import SessionLocal
    with SessionLocal() as db:
        return _full(db, db.get(Brief, brief.id))


@router.get("/briefs/{brief_id}")
def get_brief(brief_id: int, db: DB, user: CurrentUser):
    return _full(db, svc.get_owned(db, brief_id, user))


@router.patch("/briefs/{brief_id}")
def update_brief(brief_id: int, body: BriefUpdate, db: DB, user: CurrentUser):
    b = svc.get_owned(db, brief_id, user)
    if b.status in ACTIVE_STATUSES and (body.depth or body.audience):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait until the agents finish to change depth or audience")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(b, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    return svc.brief_dict(b)


@router.delete("/briefs/{brief_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brief(brief_id: int, db: DB, user: CurrentUser):
    b = svc.get_owned(db, brief_id, user)
    runner = get_runner()
    runner.cancel(brief_id)
    runner.forget(brief_id)
    folder = Path(get_settings().data_dir) / "files" / str(brief_id)
    db.execute(delete(Event).where(Event.brief_id == brief_id))     # big table: bulk delete
    db.delete(b)
    db.commit()
    shutil.rmtree(folder, ignore_errors=True)
    audit.info("brief deleted", extra={"user_id": user.id, "brief_id": brief_id})


@router.get("/briefs/{brief_id}/events")
def get_events(brief_id: int, db: DB, user: CurrentUser, after: int = 0):
    svc.get_owned(db, brief_id, user)
    return svc.events_after(db, brief_id, after)


@router.post("/briefs/{brief_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def post_message(brief_id: int, body: MessageIn, user: CurrentUser):
    return await asyncio.to_thread(chat.handle_message, user, brief_id, body.text)


@router.post("/briefs/{brief_id}/plan/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve(brief_id: int, body: PlanApproveIn, user: CurrentUser):
    await asyncio.to_thread(chat.approve_plan, user, brief_id, body.plan, body.title)
    return {"status": "researching"}


@router.post("/briefs/{brief_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel(brief_id: int, db: DB, user: CurrentUser):
    svc.get_owned(db, brief_id, user)
    return {"cancelled": get_runner().cancel(brief_id)}


@router.post("/briefs/{brief_id}/exports", status_code=status.HTTP_202_ACCEPTED)
async def create_export(brief_id: int, body: ExportIn, user: CurrentUser):
    from ...agents.schemas import ExportSpec
    if body.format:
        req = ExportSpec(format=body.format, scope=body.scope, section=body.section, style=body.style)
    elif body.request:
        req = body.request
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give a format or a request")
    run_id = await asyncio.to_thread(chat.request_export, user, brief_id, req)
    return {"run_id": run_id}


@router.get("/briefs/{brief_id}/runs/{run_id}")
def get_run(brief_id: int, run_id: int, db: DB, user: CurrentUser):
    svc.get_owned(db, brief_id, user)
    run = db.get(Run, run_id)
    if not run or run.brief_id != brief_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return {"id": run.id, "kind": run.kind, "status": run.status, "error": run.error, "llm_calls": run.llm_calls,
            "tokens": run.tokens, "created_at": run.created_at.isoformat()}


@router.get("/briefs/{brief_id}/versions")
def list_versions(brief_id: int, db: DB, user: CurrentUser):
    svc.get_owned(db, brief_id, user)
    return svc.versions(db, brief_id)


@router.post("/briefs/{brief_id}/ws-ticket")
def ws_ticket(brief_id: int, db: DB, user: CurrentUser):
    svc.get_owned(db, brief_id, user)
    return {"ticket": create_ws_ticket(user.id, brief_id), "expires_in": 60}


# ---------------------------------------------------------------- files
@router.get("/files/{file_id}")
def download(file_id: str, db: DB, user: CurrentUser, inline: bool = False):
    f = db.get(File, file_id)
    if not f or (f.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    path = Path(f.path).resolve()
    if not path.is_file() or Path(get_settings().data_dir).resolve() not in path.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    disposition = "inline" if inline and f.mime.startswith("image/") else "attachment"
    return FileResponse(path, media_type=f.mime, filename=f.filename, content_disposition_type=disposition,
                        headers={"Cache-Control": "private, max-age=3600"})
