"""WebSocket: the live, two-way chat of one brief.

    connect   /api/v1/ws/briefs/{id}?ticket=<60-second single-use ticket from POST /briefs/{id}/ws-ticket>
    client -> {"type": "hello", "after": <last event id>}      replay what was missed, then live
              {"type": "message", "text": "..."}               anything: topic, guidance, question, "as PDF" ...
              {"type": "approve_plan", "plan": [...], "title"}  the plan card's Approve button
              {"type": "export", "format": "pdf"}               a format button
              {"type": "cancel"} | {"type": "ping"}
    server -> {"type": "event", "event": {...}}  persisted timeline item (message, trace, plan, report, file, error)
              {"type": "delta", "agent": "writer", "text": "..."}  live tokens (not persisted)
              {"type": "status", "status": "..."} | {"type": "run", ...} | {"type": "error", "detail": "..."}
"""
import asyncio
import json
import logging
import threading
import time
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ...core.security import decode_ws_ticket
from ...db.models import User
from ...db.session import SessionLocal
from ...services import briefs as svc
from ...services import chat
from ...services.hub import hub

router = APIRouter()
log = logging.getLogger(__name__)
_used: dict[str, float] = {}
_used_lock = threading.Lock()
MAX_FRAME = 16_000


def _single_use(ticket: str) -> bool:
    now = time.time()
    with _used_lock:
        for t, exp in list(_used.items()):
            if exp < now:
                del _used[t]
        if ticket in _used:
            return False
        _used[ticket] = now + 120
        return True


def _same_origin(ws: WebSocket) -> bool:
    """Block cross-site WebSocket hijacking: browsers always send Origin on WebSocket handshakes."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True                       # non-browser client (scripts, tests); it still needs a ticket
    return urlparse(origin).netloc == ws.headers.get("host")


@router.websocket("/ws/briefs/{brief_id}")
async def brief_socket(ws: WebSocket, brief_id: int, ticket: str = ""):
    try:
        claims = decode_ws_ticket(ticket)
    except jwt.PyJWTError:
        await ws.close(code=4401)
        return
    if claims.get("brief") != brief_id or not _single_use(ticket) or not _same_origin(ws):
        await ws.close(code=4403)
        return
    with SessionLocal() as db:
        user = db.get(User, int(claims["sub"]))
        if not user or not user.is_active:
            await ws.close(code=4401)
            return
        try:
            brief = svc.get_owned(db, brief_id, user)
        except HTTPException:
            await ws.close(code=4404)
            return
        brief_info = svc.brief_dict(brief)
    await ws.accept()
    queue = hub.subscribe(brief_id)          # subscribe BEFORE replay: nothing published in between is lost
    sender = None
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
        after = int(hello.get("after") or 0) if hello.get("type") == "hello" else 0
        with SessionLocal() as db:
            backlog = svc.events_after(db, brief_id, after)
        await ws.send_json({"type": "ready", "brief": brief_info})
        last_id = after
        for e in backlog:
            await ws.send_json({"type": "event", "event": e})
            last_id = e["id"]

        async def pump():
            while True:
                msg = await queue.get()
                if msg.get("type") == "_overflow":
                    await ws.close(code=4408)            # client reconnects and replays from the DB
                    return
                if msg.get("type") == "event" and msg["event"]["id"] <= last_id:
                    continue                             # already sent in the backlog
                await ws.send_json(msg)

        sender = asyncio.create_task(pump())
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_FRAME:
                await ws.send_json({"type": "error", "detail": "Message too large"})
                continue
            try:
                msg = json.loads(raw)
                await _handle(ws, user, brief_id, msg)
            except HTTPException as exc:
                await ws.send_json({"type": "error", "detail": exc.detail})
            except (ValueError, TypeError, KeyError):
                await ws.send_json({"type": "error", "detail": "Malformed message"})
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket error", extra={"brief_id": brief_id})
    finally:
        hub.unsubscribe(brief_id, queue)
        if sender:
            sender.cancel()


async def _handle(ws: WebSocket, user: User, brief_id: int, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "ping":
        await ws.send_json({"type": "pong"})
    elif kind == "message":
        await asyncio.to_thread(chat.handle_message, user, brief_id, str(msg.get("text", ""))[:4000])
    elif kind == "approve_plan":
        plan = msg.get("plan")
        plan = [str(q)[:300] for q in plan][:6] if isinstance(plan, list) else None
        await asyncio.to_thread(chat.approve_plan, user, brief_id, plan, msg.get("title"))
    elif kind == "export":
        from ...agents.schemas import ExportSpec
        spec = ExportSpec(format=str(msg.get("format", "pdf"))[:20], scope=msg.get("scope") if msg.get("scope") in
                          ("full", "summary", "section") else "full", section=str(msg.get("section", ""))[:100],
                          style=str(msg.get("style", ""))[:200])
        await asyncio.to_thread(chat.request_export, user, brief_id, spec)
    elif kind == "cancel":
        from ...services.runner import get_runner
        if not get_runner().cancel(brief_id):
            await ws.send_json({"type": "error", "detail": "Nothing is running"})
    else:
        await ws.send_json({"type": "error", "detail": f"Unknown message type {kind!r}"})
