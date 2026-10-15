"""Conversation agent: every chat message (web or MCP) goes through here.

    message -> intent (keyword fast path, else the fast LLM with the brief's state, else rules)
            -> action: start research | steer the running agents | approve / edit the plan | answer a
               question from the sources | revise the report | export a file | cancel | retry

The user can type at ANY time. While agents run, a message becomes guidance the Supervisor picks up at
its next step (or a question answered from the sources gathered so far) - that is the two-way, live part.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage

from ..agents import prompts
from ..agents.publisher import KEYWORDS as EXPORT_KEYWORDS
from ..agents.publisher import export as publish
from ..agents.schemas import ChatIntent, ExportSpec, RevisedPlan
from ..core.config import get_settings
from ..db.models import ACTIVE_STATUSES, Brief, File, User
from ..db.session import SessionLocal
from ..llm.gateway import LLMUnavailable, get_gateway
from ..tools.web import relevant_passages, tokens
from . import briefs as svc
from .hub import hub
from .runner import current_run, get_runner

log = logging.getLogger(__name__)

APPROVE = re.compile(r"^\s*(yes|yep|yeah|ok(ay)?|go( ahead)?|start|approve[d]?|looks good|sounds good|perfect|lgtm|"
                     r"great|do it|proceed|continue)\b", re.I)
CANCEL = re.compile(r"^\s*(stop|cancel|abort|halt)\b", re.I)
RETRY = re.compile(r"^\s*(retry|try again|resume|continue)\b", re.I)
EXPORT_VERB = re.compile(r"\b(give|send|export|download|convert|make|create|generate|turn|want|need|as|in|into|"
                         r"save|produce|prepare|share|have|get|like|please|print|email|attach)\b", re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|cool|nice|great)\b[\s!.]*$", re.I)

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_rate: dict[tuple[int, str], deque] = defaultdict(deque)


def _limit(user: User, kind: str, per: int, window: float) -> None:
    q = _rate[(user.id, kind)]
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= per:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "You're sending messages too fast - slow down a bit.")
    q.append(now)


def reply(brief_id: int, text: str, **extra) -> None:
    svc.record(brief_id, "assistant", "message", {"text": text, **extra}, agent="chat", run_id=current_run.get())


# ---------------------------------------------------------------- intent
def _state(brief: Brief) -> str:
    return (f"Status: {brief.status}\nReport exists: {'yes, v' + str(brief.version) if brief.report_md else 'no'}\n"
            f"Topic: {brief.topic or '-'}\nPlan: {brief.plan or '-'}")


def detect_intent(brief: Brief, text: str) -> ChatIntent:
    def it(intent: str, fmt: str = "") -> ChatIntent:
        return ChatIntent(intent=intent, instruction=text, format=fmt, scope="full", section="")

    if brief.status == "new" and not brief.topic:
        return it("start")
    if CANCEL.match(text) and len(text) < 40:
        return it("cancel")
    if brief.status == "awaiting_approval" and APPROVE.match(text) and len(text) < 60:
        return it("approve_plan")
    if brief.status in ("failed", "cancelled") and RETRY.match(text) and len(text) < 40:
        return it("retry")
    if brief.report_md and EXPORT_VERB.search(text):
        for pattern, fmt in EXPORT_KEYWORDS:
            m = re.search(pattern, text.lower())
            if m:
                return it("export", fmt or m.group(1))
    if GREETING.match(text):
        return it("smalltalk")
    try:
        return get_gateway().structured("chat", [SystemMessage(prompts.CHAT), HumanMessage(
            f"STATE\n{_state(brief)}\n\nUSER MESSAGE: {text}")], ChatIntent)
    except LLMUnavailable:
        if brief.status in ACTIVE_STATUSES:
            return it("question" if text.rstrip().endswith("?") else "steer")
        if brief.status == "awaiting_approval":
            return it("edit_plan")
        if not brief.report_md:
            return it("retry" if brief.status in ("failed", "cancelled") else "start")
        return it("question" if text.rstrip().endswith("?") else "revise")


# ---------------------------------------------------------------- entry points
def create_brief(user: User, audience: str = "business readers", depth: str = "standard",
                 auto_approve: bool = False) -> Brief:
    with SessionLocal() as db:
        svc.check_daily_quota(db, user)
        b = Brief(owner_id=user.id, audience=audience[:120] or "business readers",
                  depth=depth if depth in ("quick", "standard", "deep") else "standard", auto_approve=auto_approve)
        db.add(b)
        db.commit()
        db.refresh(b)
        _ = b.owner
        return b


def handle_message(user: User, brief_id: int, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty message")
    if len(text) > 4000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Message too long (max 4,000 characters)")
    _limit(user, "msg", get_settings().messages_per_minute, 60)
    with SessionLocal() as db:
        brief = svc.get_owned(db, brief_id, user)
    svc.record(brief_id, "user", "message", {"text": text}, agent=None)
    with _locks[brief_id]:
        with SessionLocal() as db:
            brief = db.get(Brief, brief_id)
            intent = detect_intent(brief, text)
            log.info("chat intent", extra={"brief_id": brief_id, "intent": intent.intent})
            _dispatch(user, brief, intent, text)
    return {"intent": intent.intent}


def _dispatch(user: User, brief: Brief, intent: ChatIntent, text: str) -> None:
    runner, bid, active = get_runner(), brief.id, brief.status in ACTIVE_STATUSES
    i = intent.intent
    if i == "start" and (active or brief.report_md or brief.status == "awaiting_approval"):
        i = "steer" if active else ("edit_plan" if brief.status == "awaiting_approval" else "revise")
    if i == "steer" and not active:
        i = "edit_plan" if brief.status == "awaiting_approval" else ("revise" if brief.report_md else "start")
    if i == "revise" and active:
        i = "steer"
    if i == "approve_plan" and brief.status != "awaiting_approval":
        i = "smalltalk"

    if i == "start":
        start_research(user, brief, text)
    elif i == "steer":
        svc.add_steer(bid, intent.instruction or text)
        reply(bid, "Got it - I'll pass that to the team at their next step.")
    elif i == "approve_plan":
        approve_plan(user, bid, None)
    elif i == "edit_plan":
        runner.submit_task(bid, "plan_edit", lambda: _edit_plan(bid, intent.instruction or text), text)
    elif i == "question":
        runner.submit_task(bid, "answer", lambda: _answer(bid, text), text)
    elif i == "revise":
        if not brief.report_md:
            reply(bid, "There's no report yet. Tell me a topic to research first.")
        else:
            reply(bid, f"Revising the report: “{intent.instruction or text}”. You'll see the agents work below.")
            runner.start_graph(bid, "revise", "revise", intent.instruction or text, instruction=text)
            svc.set_status(bid, "planning")
    elif i == "export":
        if not brief.report_md:
            reply(bid, "I'll be able to export once the report is ready.")
        else:
            spec = ExportSpec(format=intent.format, scope=intent.scope, section=intent.section, style=text[:200]) \
                if intent.format else None
            request_export(user, bid, spec or text)
    elif i == "cancel":
        if runner.cancel(bid):
            reply(bid, "Stopping after the current step…")
        else:
            reply(bid, "Nothing is running right now.")
    elif i == "retry":
        retry(user, brief)
    else:
        hints = {
            "awaiting_approval": "Say “go” to approve the plan, or tell me what to change.",
            "done": "Ask a question about the report, ask for changes, or ask for a PDF, Word file, slides or image.",
            "failed": "Say “retry” to continue from the last completed step.",
        }
        reply(bid, "Happy to help! " + hints.get(brief.status, "The team is working - you can steer them anytime."))


def start_research(user: User, brief: Brief, topic: str) -> None:
    with SessionLocal() as db:
        b = db.get(Brief, brief.id)
        if b.topic is None:
            svc.check_daily_quota(db, user)
        b.topic, b.title, b.status = topic[:1000], topic[:80], "planning"
        db.commit()
    hub.publish(brief.id, {"type": "status", "status": "planning", "title": topic[:80]})
    reply(brief.id, "Planning the research" + (" - the team will start right away." if brief.auto_approve else
                                               ". I'll show you the plan before the team starts."))
    get_runner().start_graph(brief.id, "research", "start", instruction=topic)


def approve_plan(user: User, brief_id: int, plan: list[str] | None, title: str | None = None) -> None:
    with _locks_guard(brief_id):
        with SessionLocal() as db:
            b = svc.get_owned(db, brief_id, user)
            if b.status != "awaiting_approval":
                raise HTTPException(status.HTTP_409_CONFLICT, "There is no plan waiting for approval")
            if plan is not None:
                clean = [str(q).strip()[:300] for q in plan if str(q).strip()][:6]
                if not clean:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The plan needs at least one question")
                b.plan = clean
            if title:
                b.title = title.strip()[:200]
            b.status = "researching"
            db.commit()
            final_plan, final_title = b.plan, b.title
    svc.record(brief_id, "assistant", "message", {"text": "Plan approved - the team is starting."}, agent="chat")
    hub.publish(brief_id, {"type": "status", "status": "researching"})
    get_runner().start_graph(brief_id, "research", "approve", {"plan": final_plan, "title": final_title},
                             instruction="approve plan")


class _locks_guard:
    """Re-entrant-safe: approve_plan may be called from inside handle_message (lock already held)."""

    def __init__(self, brief_id: int):
        self.lock = _locks[brief_id]
        self.acquired = False

    def __enter__(self):
        self.acquired = self.lock.acquire(blocking=False)
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.lock.release()


def retry(user: User, brief: Brief) -> None:
    runner = get_runner()
    if brief.status in ACTIVE_STATUSES:
        reply(brief.id, "The team is already working on it.")
        return
    if brief.status == "awaiting_approval":
        reply(brief.id, "The plan is waiting for you - say “go” to start.")
        return
    if not brief.topic:
        reply(brief.id, "Tell me a topic to research first.")
        return
    if runner.has_checkpoint(brief.id):
        reply(brief.id, "Continuing from the last completed step…")
        svc.set_status(brief.id, "researching")
        runner.start_graph(brief.id, "research", "retry", instruction="retry")
    else:
        start_research(user, brief, brief.topic)


def request_export(user: User, brief_id: int, request: str | ExportSpec) -> int:
    _limit(user, "export", get_settings().exports_per_user_per_hour, 3600)
    with SessionLocal() as db:
        b = svc.get_owned(db, brief_id, user)
        if not b.report_md:
            raise HTTPException(status.HTTP_409_CONFLICT, "The report is not ready yet")
    label = request if isinstance(request, str) else request.format
    return get_runner().submit_task(brief_id, "export", lambda: _export(brief_id, user.id, request), str(label))


# ---------------------------------------------------------------- tasks (run in the task pool)
def _gateway_or_none():
    try:
        return get_gateway()
    except LLMUnavailable:
        return None


def _export(brief_id: int, owner_id: int, request: str | ExportSpec) -> None:
    from ..agents.publisher import understand
    from ..renderers import FORMATS, normalise_format

    gw = _gateway_or_none()
    spec = request if isinstance(request, ExportSpec) else understand(gw, request)
    fmt, _ = normalise_format(spec.format)
    label = FORMATS[fmt].label if fmt else spec.format
    svc.record(brief_id, "agent", "trace", {"event": "export", "detail": f"Preparing {label} ({spec.scope})"},
               agent="publisher", run_id=current_run.get())
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        srcs = svc.sources(db, brief_id)
        meta = {"version": b.version, "verified_ratio": b.verified_ratio, "audience": b.audience, "topic": b.topic,
                "generated": b.updated_at.strftime("%d %b %Y")}
        title, markdown = b.title, b.report_md
    result = publish(gw, spec, title, markdown, srcs, meta)
    file_id = secrets.token_hex(16)
    folder = Path(get_settings().data_dir) / "files" / str(brief_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}.{result.format}"
    path.write_bytes(result.data)
    with SessionLocal() as db:
        f = File(id=file_id, brief_id=brief_id, owner_id=owner_id, format=result.format, filename=result.filename,
                 mime=result.mime, size=len(result.data), path=str(path), description=result.description[:300])
        db.add(f)
        db.commit()
        data = svc.file_dict(f)
    svc.record(brief_id, "assistant", "file", {"file": data, "note": result.note}, agent="publisher",
               run_id=current_run.get())


def _answer(brief_id: int, question: str) -> None:
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        report = b.report_md or ""
    texts = svc.source_texts(brief_id)
    q = set(tokens(question))
    ranked = sorted(texts.items(), key=lambda kv: len(q & set(tokens(kv[1]))), reverse=True)[:4]
    excerpts = "\n\n".join(f"<source n={n}>\n{relevant_passages(t, question, 160)}\n</source>" for n, t in ranked)
    gw = get_gateway()
    from .runner import get_runner as _gr
    deps = _gr().deps
    svc.record(brief_id, "agent", "draft_start", {"target": "answer"}, agent="chat", run_id=current_run.get())
    text = gw.text("answer", [SystemMessage(prompts.ANSWER), HumanMessage(
        f"<draft>\n{report[:9000] or '(no report yet - research in progress)'}\n</draft>\n\n{excerpts}\n\n"
        f"QUESTION: {question}")], tier="fast", on_token=lambda t: deps.delta(brief_id, "chat", t), max_tokens=700)
    deps.flush(brief_id)
    reply(brief_id, text.strip(), markdown=True)


def _edit_plan(brief_id: int, change: str) -> None:
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        plan, title = list(b.plan or []), b.title
    try:
        r: RevisedPlan = get_gateway().structured("plan_edit", [SystemMessage(prompts.PLAN_EDIT), HumanMessage(
            f"Title: {title}\nPlan:\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(plan, 1))
            + f"\nChange: {change}")], RevisedPlan)
        new_plan = [q.strip() for q in r.sub_questions if q.strip()][:6] or plan
        new_title, summary = r.title.strip()[:200] or title, r.change_summary
    except LLMUnavailable:
        new_plan, new_title, summary = (plan + [change.strip()[:300]])[:6], title, "Added your point to the plan."
    with SessionLocal() as db:
        b = db.get(Brief, brief_id)
        if b.status != "awaiting_approval":
            return
        b.plan, b.title = new_plan, new_title
        db.commit()
    svc.record(brief_id, "assistant", "plan", {"plan": new_plan, "title": new_title, "awaiting": True,
                                               "change": summary}, agent="supervisor", run_id=current_run.get())
