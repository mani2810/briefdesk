"""Background job runner: agent runs never block an HTTP request or the WebSocket loop.

    research / approve / revise / retry  -> the LangGraph graph (agent pool: MAX_CONCURRENT_RUNS threads)
    answer / export / plan_edit          -> short LLM tasks       (task pool: 4 threads)

LangGraph state is checkpointed in SQLite after every node (thread id "brief-<id>"):
    * the plan-approval pause survives restarts (approve tomorrow, research continues)
    * a failed run can be retried from the last completed step - no repeated LLM spend
    * revisions resume with all notes, sources and the current draft
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import select

from ..agents.graph import RunCancelled, build_graph
from ..core.config import get_settings
from ..db.models import ACTIVE_STATUSES, Brief, Run, utcnow
from ..db.session import SessionLocal
from ..llm.gateway import LLMUnavailable, RunContext, get_gateway, run_context
from ..tools.registry import Document, ToolRegistry, get_registry
from . import briefs as svc
from .hub import hub

log = logging.getLogger(__name__)
current_run: ContextVar[int | None] = ContextVar("current_run", default=None)


class DbDeps:
    """The graph's view of the world (see agents.graph.Deps), backed by the database and the live hub."""

    def __init__(self, runner: "Runner"):
        self.runner = runner
        self._buf: dict[tuple[int, str], list] = {}
        self._buf_lock = threading.Lock()

    @property
    def gateway(self):
        return get_gateway()

    @property
    def tools(self) -> ToolRegistry:
        return get_registry()

    def emit(self, brief_id: int, kind: str, payload: dict, agent: str | None = None) -> None:
        self.flush(brief_id)
        svc.record(brief_id, "agent", kind, payload, agent=agent, run_id=current_run.get())

    def delta(self, brief_id: int, agent: str, text: str) -> None:
        """Writer tokens: batched (~every 100 ms) so a 1,500-token report is ~100 socket frames, not 1,500."""
        key = (brief_id, agent)
        with self._buf_lock:
            buf = self._buf.setdefault(key, ["", time.monotonic()])
            buf[0] += text
            if len(buf[0]) < 60 and time.monotonic() - buf[1] < 0.1:
                return
            chunk, self._buf[key] = buf[0], ["", time.monotonic()]
        hub.publish(brief_id, {"type": "delta", "agent": agent, "text": chunk})

    def flush(self, brief_id: int) -> None:
        with self._buf_lock:
            pending = [(k, v[0]) for k, v in self._buf.items() if k[0] == brief_id and v[0]]
            for k, _ in pending:
                self._buf[k] = ["", time.monotonic()]
        for (bid, agent), chunk in pending:
            hub.publish(bid, {"type": "delta", "agent": agent, "text": chunk})

    def set_status(self, brief_id: int, status: str) -> None:
        svc.set_status(brief_id, status)

    def check_cancel(self, brief_id: int) -> None:
        if self.runner.is_cancelled(brief_id):
            raise RunCancelled()

    def pull_steering(self, brief_id: int) -> list[str]:
        return svc.pull_steering(brief_id)

    def add_sources(self, brief_id: int, docs: list[Document]) -> list[dict]:
        return svc.add_sources(brief_id, docs)

    def source_texts(self, brief_id: int) -> dict[int, str]:
        return svc.source_texts(brief_id)

    def save_report(self, brief_id, title, markdown, verification, instruction) -> int:
        self.flush(brief_id)
        return svc.save_report(brief_id, title, markdown, verification, instruction, run_id=current_run.get())


class Runner:
    def __init__(self) -> None:
        s = get_settings()
        self._agents = ThreadPoolExecutor(max_workers=s.max_concurrent_runs, thread_name_prefix="agent")
        self._tasks = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task")
        self._cancel: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.deps = DbDeps(self)
        self._conn = sqlite3.connect(s.checkpoint_db, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.checkpointer = SqliteSaver(self._conn)
        self.graph = build_graph(self.deps, self.checkpointer)

    # ------------------------------------------------------------ public
    def start_graph(self, brief_id: int, kind: str, mode: str, payload: Any = None,
                    instruction: str | None = None) -> int:
        """mode: start | approve | revise | retry"""
        run_id = self._new_run(brief_id, kind, instruction)
        with self._lock:
            self._cancel[brief_id] = threading.Event()
        self._agents.submit(self._execute_graph, run_id, brief_id, mode, payload)
        return run_id

    def submit_task(self, brief_id: int, kind: str, fn: Callable[[], None], instruction: str | None = None) -> int:
        run_id = self._new_run(brief_id, kind, instruction)
        self._tasks.submit(self._execute_task, run_id, brief_id, fn)
        return run_id

    def cancel(self, brief_id: int) -> bool:
        with self._lock:
            ev = self._cancel.get(brief_id)
        if ev:
            ev.set()
            return True
        return False

    def is_cancelled(self, brief_id: int) -> bool:
        with self._lock:
            ev = self._cancel.get(brief_id)
        return bool(ev and ev.is_set())

    def has_checkpoint(self, brief_id: int) -> bool:
        return self.graph.get_state(self._cfg(brief_id)).values != {}

    def forget(self, brief_id: int) -> None:
        """Delete a brief's agent state (when the brief is deleted)."""
        try:
            self.checkpointer.delete_thread(f"brief-{brief_id}")
        except Exception:  # noqa: BLE001
            log.warning("could not delete checkpoint for brief %s", brief_id)

    def recover(self) -> None:
        """After a restart: jobs that were running are gone. Mark them so the user can say 'retry'."""
        with SessionLocal() as db:
            for run in db.scalars(select(Run).where(Run.status.in_(("queued", "running")))):
                run.status, run.error, run.finished_at = "failed", "server restarted", utcnow()
            stuck = db.scalars(select(Brief).where(Brief.status.in_(ACTIVE_STATUSES))).all()
            for b in stuck:
                b.status = "done" if b.report_md else "failed"
            db.commit()
            ids = [b.id for b in stuck]
        for bid in ids:
            svc.record(bid, "system", "error", {"message": "The server restarted while agents were working. "
                                                           "Say “retry” to continue from the last completed step.",
                                                "retryable": True})

    def shutdown(self) -> None:
        for ev in list(self._cancel.values()):
            ev.set()
        self._agents.shutdown(wait=False, cancel_futures=True)
        self._tasks.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ internals
    @staticmethod
    def _cfg(brief_id: int) -> dict:
        return {"configurable": {"thread_id": f"brief-{brief_id}"}, "recursion_limit": 120}

    @staticmethod
    def _new_run(brief_id: int, kind: str, instruction: str | None) -> int:
        with SessionLocal() as db:
            run = Run(brief_id=brief_id, kind=kind, instruction=(instruction or "")[:2000] or None)
            db.add(run)
            db.commit()
            return run.id

    @staticmethod
    def _finish_run(run_id: int, status: str, ctx: RunContext | None = None, error: str | None = None) -> None:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            run.status, run.error, run.finished_at = status, error, utcnow()
            if ctx:
                run.llm_calls, run.tokens = ctx.calls, ctx.tokens
                brief = db.get(Brief, run.brief_id)
                brief.llm_calls += ctx.calls
                brief.tokens += ctx.tokens
            db.commit()

    @staticmethod
    def _notifier(brief_id: int) -> Callable[[str], None]:
        last = [0.0]

        def notify(msg: str) -> None:
            if time.monotonic() - last[0] > 20:          # at most one pacing notice per 20 s
                last[0] = time.monotonic()
                svc.record(brief_id, "agent", "trace", {"event": "pacing", "detail": msg}, agent="gateway",
                           run_id=current_run.get())
        return notify

    def _graph_input(self, brief: Brief, mode: str, payload: Any):
        if mode == "start":
            return {"brief_id": brief.id, "topic": brief.topic, "audience": brief.audience, "depth": brief.depth,
                    "auto_approve": brief.auto_approve, "iterations": 0, "notes": [], "sources": []}
        if mode == "approve":
            return Command(resume=payload)
        if mode == "revise":
            return {"revision": payload, "last_worker": "revision", "iterations": 0, "review": None,
                    "verification": None, "reviews": 0, "verify_rounds": 0, "gap": None}
        return None                                       # retry: continue from the last checkpoint

    def _execute_graph(self, run_id: int, brief_id: int, mode: str, payload: Any) -> None:
        s = get_settings()
        ctx = RunContext(max_calls=s.run_max_llm_calls, notify=self._notifier(brief_id))
        t_ctx, t_run = run_context.set(ctx), current_run.set(run_id)
        with SessionLocal() as db:
            run, brief = db.get(Run, run_id), db.get(Brief, brief_id)
            run.status, run.started_at = "running", utcnow()
            db.commit()
            graph_input = self._graph_input(brief, mode, payload)
            had_report = bool(brief.report_md)
        hub.publish(brief_id, {"type": "run", "run_id": run_id, "status": "running", "mode": mode})
        try:
            result = self.graph.invoke(graph_input, self._cfg(brief_id))
            self.deps.flush(brief_id)
            if result.get("__interrupt__"):
                self._finish_run(run_id, "awaiting_approval", ctx)
                plan, title = result.get("plan", []), result.get("title")
                with SessionLocal() as db:
                    b = db.get(Brief, brief_id)
                    b.plan, b.title, b.status = plan, (title or b.title)[:200], "awaiting_approval"
                    db.commit()
                svc.record(brief_id, "assistant", "plan", {"plan": plan, "title": title, "awaiting": True},
                           agent="supervisor", run_id=run_id)
                hub.publish(brief_id, {"type": "status", "status": "awaiting_approval"})
            else:
                self._finish_run(run_id, "done", ctx)
                svc.set_status(brief_id, "done")
                v = result.get("verification") or {}
                pct = f" {round(v['ratio'] * 100)}% of citations were verified against their sources." \
                    if v.get("ratio") is not None else ""
                svc.record(brief_id, "assistant", "message", {"text": (
                    f"Report v{result.get('version')} is ready.{pct} Ask me anything about it, ask for changes, or "
                    "ask for it in any format - PDF, Word, slides, an infographic image, Excel …")},
                    agent="supervisor", run_id=run_id)
        except RunCancelled:
            self._finish_run(run_id, "cancelled", ctx)
            svc.set_status(brief_id, "done" if had_report else "cancelled")
            svc.record(brief_id, "system", "message", {"text": "Stopped. Say “retry” to continue where we left off."},
                       run_id=run_id)
        except (LLMUnavailable, GraphRecursionError) as exc:
            reason = exc.reason if isinstance(exc, LLMUnavailable) else "the agents did not converge"
            self._fail(run_id, brief_id, ctx, reason, had_report)
        except Exception as exc:  # noqa: BLE001 - any bug must still end the run cleanly
            log.exception("agent run failed", extra={"brief_id": brief_id, "run_id": run_id})
            self._fail(run_id, brief_id, ctx, f"unexpected error ({type(exc).__name__})", had_report)
        finally:
            run_context.reset(t_ctx)
            current_run.reset(t_run)
            with self._lock:
                self._cancel.pop(brief_id, None)
            hub.publish(brief_id, {"type": "run", "run_id": run_id, "status": "finished"})

    def _fail(self, run_id: int, brief_id: int, ctx: RunContext, reason: str, had_report: bool) -> None:
        self._finish_run(run_id, "failed", ctx, reason)
        svc.set_status(brief_id, "done" if had_report else "failed")
        svc.record(brief_id, "system", "error", {
            "message": f"The agents stopped: {reason}. Your progress is saved - say “retry” to continue "
                       "from the last completed step.", "retryable": True}, run_id=run_id)

    def _execute_task(self, run_id: int, brief_id: int, fn: Callable[[], None]) -> None:
        ctx = RunContext(max_calls=8, notify=self._notifier(brief_id))
        t_ctx, t_run = run_context.set(ctx), current_run.set(run_id)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            run.status, run.started_at = "running", utcnow()
            db.commit()
        try:
            fn()
            self._finish_run(run_id, "done", ctx)
        except LLMUnavailable as exc:
            self._finish_run(run_id, "failed", ctx, exc.reason)
            svc.record(brief_id, "system", "error", {"message": f"Sorry - {exc.reason}. Please try again.",
                                                     "retryable": False}, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("task failed", extra={"brief_id": brief_id, "run_id": run_id})
            self._finish_run(run_id, "failed", ctx, type(exc).__name__)
            svc.record(brief_id, "system", "error", {"message": "Sorry - that failed unexpectedly. Please try again.",
                                                     "retryable": False}, run_id=run_id)
        finally:
            self.deps.flush(brief_id)
            run_context.reset(t_ctx)
            current_run.reset(t_run)


_runner: Runner | None = None


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        _runner.shutdown()
        _runner = None
