"""Brief data access shared by the API, the chat agent, the job runner and the MCP server routes."""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.models import Brief, Citation, Event, File, ReportVersion, Run, Source, SteerNote, User, utcnow
from ..db.session import SessionLocal
from ..tools.registry import Document
from .hub import hub

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- events
def event_dict(e: Event) -> dict:
    return {"id": e.id, "run_id": e.run_id, "role": e.role, "kind": e.kind, "agent": e.agent,
            "payload": e.payload or {}, "created_at": e.created_at.isoformat()}


def record(brief_id: int, role: str, kind: str, payload: dict, agent: str | None = None,
           run_id: int | None = None, db: Session | None = None) -> dict:
    """Persist an event, then push it to live subscribers (persist first: replay is always complete)."""
    own = db is None
    db = db or SessionLocal()
    try:
        e = Event(brief_id=brief_id, role=role, kind=kind, payload=payload, agent=agent, run_id=run_id)
        db.add(e)
        db.commit()
        data = event_dict(e)
    finally:
        if own:
            db.close()
    hub.publish(brief_id, {"type": "event", "event": data})
    return data


def events_after(db: Session, brief_id: int, after: int = 0, limit: int = 2000) -> list[dict]:
    rows = db.scalars(select(Event).where(Event.brief_id == brief_id, Event.id > after)
                      .order_by(Event.id).limit(limit)).all()
    return [event_dict(e) for e in rows]


# ---------------------------------------------------------------- briefs
def get_owned(db: Session, brief_id: int, user: User) -> Brief:
    """Owner (or admin) only; 404 otherwise so ids of other users' briefs are not confirmed."""
    brief = db.get(Brief, brief_id)
    if not brief or (brief.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brief not found")
    return brief


def brief_dict(b: Brief, db: Session | None = None) -> dict:
    out = {"id": b.id, "title": b.title, "topic": b.topic, "audience": b.audience, "depth": b.depth,
           "auto_approve": b.auto_approve, "status": b.status, "plan": b.plan or [], "version": b.version,
           "verified_ratio": b.verified_ratio, "llm_calls": b.llm_calls, "tokens": b.tokens,
           "created_at": b.created_at.isoformat(), "updated_at": b.updated_at.isoformat(),
           "owner": {"id": b.owner_id, "name": b.owner.name, "email": b.owner.email} if b.owner else None}
    if db is not None:
        out["report_md"] = b.report_md
    return out


def set_status(brief_id: int, new_status: str) -> None:
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        if not b or b.status == new_status:
            return
        b.status = new_status
        db.commit()
    hub.publish(brief_id, {"type": "status", "status": new_status})


def check_daily_quota(db: Session, user: User) -> None:
    if user.role == "admin":
        return
    since = utcnow() - timedelta(days=1)
    n = db.scalar(select(func.count(Brief.id)).where(Brief.owner_id == user.id, Brief.created_at >= since,
                                                     Brief.topic.is_not(None)))
    if n >= get_settings().briefs_per_user_per_day:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Daily limit of {get_settings().briefs_per_user_per_day} research briefs reached.")


# ---------------------------------------------------------------- sources
def add_sources(brief_id: int, docs: list[Document]) -> list[dict]:
    """Number new documents [n] (dedupe by URL). Capped per brief to bound storage and prompts."""
    cap = get_settings().max_sources_per_brief
    out = []
    with SessionLocal() as db:
        existing = {s.url: s for s in db.scalars(select(Source).where(Source.brief_id == brief_id))}
        n = max((s.n for s in existing.values()), default=0)
        for d in docs:
            url = d.url[:2000]
            if url in existing or len(existing) >= cap:
                continue
            n += 1
            domain = urlparse(url).hostname or d.origin
            src = Source(brief_id=brief_id, n=n, url=url, title=(d.title or domain)[:300], domain=domain[:200],
                         origin=d.origin[:60], snippet=d.snippet[:500], content=d.content[:20000])
            db.add(src)
            existing[url] = src
            out.append({"n": n, "title": src.title, "url": url, "origin": src.origin, "domain": src.domain})
        db.commit()
    return out


def sources(db: Session, brief_id: int) -> list[dict]:
    return [{"n": s.n, "title": s.title, "url": s.url, "domain": s.domain, "origin": s.origin, "snippet": s.snippet}
            for s in db.scalars(select(Source).where(Source.brief_id == brief_id).order_by(Source.n))]


def source_texts(brief_id: int) -> dict[int, str]:
    with SessionLocal() as db:
        return {s.n: s.content for s in db.scalars(select(Source).where(Source.brief_id == brief_id))}


# ---------------------------------------------------------------- reports
def save_report(brief_id: int, title: str, markdown: str, verification: dict | None, instruction: str | None,
                run_id: int | None = None) -> int:
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        version = (b.version or 0) + 1
        ratio = verification.get("ratio") if verification else None
        db.add(ReportVersion(brief_id=brief_id, version=version, markdown=markdown, instruction=instruction,
                             verified_ratio=ratio))
        for r in (verification or {}).get("results", []):
            db.add(Citation(brief_id=brief_id, version=version, sentence=r["sentence"][:2000], source_n=r["source_n"],
                            status=r["status"], score=r["score"], evidence=(r.get("evidence") or "")[:1000]))
        b.report_md, b.version, b.verified_ratio, b.title = markdown, version, ratio, title[:200]
        db.commit()
    record(brief_id, "assistant", "report", {
        "version": version, "title": title, "markdown": markdown, "instruction": instruction,
        "verification": {k: v for k, v in (verification or {}).items() if k in ("ratio", "counts", "problems")}},
        agent="supervisor", run_id=run_id)
    return version


def citations(db: Session, brief_id: int, version: int) -> list[dict]:
    rows = db.scalars(select(Citation).where(Citation.brief_id == brief_id, Citation.version == version)
                      .order_by(Citation.id)).all()
    return [{"sentence": c.sentence, "n": c.source_n, "status": c.status, "score": c.score, "evidence": c.evidence}
            for c in rows]


def versions(db: Session, brief_id: int) -> list[dict]:
    rows = db.scalars(select(ReportVersion).where(ReportVersion.brief_id == brief_id)
                      .order_by(ReportVersion.version)).all()
    return [{"version": v.version, "instruction": v.instruction, "verified_ratio": v.verified_ratio,
             "created_at": v.created_at.isoformat(), "markdown": v.markdown} for v in rows]


def files(db: Session, brief_id: int) -> list[dict]:
    rows = db.scalars(select(File).where(File.brief_id == brief_id).order_by(File.created_at.desc())).all()
    return [file_dict(f) for f in rows]


def file_dict(f: File) -> dict:
    return {"id": f.id, "format": f.format, "filename": f.filename, "mime": f.mime, "size": f.size,
            "description": f.description, "created_at": f.created_at.isoformat()}


# ---------------------------------------------------------------- steering
def add_steer(brief_id: int, text: str) -> None:
    with SessionLocal() as db:
        db.add(SteerNote(brief_id=brief_id, text=text[:1000]))
        db.commit()


def pull_steering(brief_id: int) -> list[str]:
    with SessionLocal() as db:
        notes = db.scalars(select(SteerNote).where(SteerNote.brief_id == brief_id, SteerNote.consumed_at.is_(None))
                           .order_by(SteerNote.id)).all()
        for n in notes:
            n.consumed_at = utcnow()
        db.commit()
        return [n.text for n in notes]


def active_run(db: Session, brief_id: int) -> Run | None:
    return db.scalar(select(Run).where(Run.brief_id == brief_id, Run.status.in_(("queued", "running")),
                                       Run.kind.in_(("research", "revise"))).order_by(Run.id.desc()))
