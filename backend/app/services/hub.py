"""Live fan-out of chat events to WebSocket subscribers.

Agents run in worker THREADS; WebSockets live on the asyncio loop. publish() is thread-safe: it hands
the message to the loop, which copies it into every subscriber's bounded queue. A subscriber that falls
too far behind is dropped (its socket closes, the browser reconnects and replays from the database), so
one slow client can never block the agents or other users.

Every *persistent* event is written to the database BEFORE it is published, so "replay after the last
event id" is always complete. Token deltas are ephemeral (only the final text is stored).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict

log = logging.getLogger(__name__)
QUEUE_SIZE = 2000


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, brief_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        with self._lock:
            self._subs[brief_id].add(q)
        return q

    def unsubscribe(self, brief_id: int, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs[brief_id].discard(q)
            if not self._subs[brief_id]:
                self._subs.pop(brief_id, None)

    def subscribers(self, brief_id: int) -> int:
        with self._lock:
            return len(self._subs.get(brief_id, ()))

    def publish(self, brief_id: int, message: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, brief_id, message)
        except RuntimeError:          # loop shutting down
            pass

    def _fanout(self, brief_id: int, message: dict) -> None:
        with self._lock:
            queues = list(self._subs.get(brief_id, ()))
        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("dropping slow subscriber", extra={"brief_id": brief_id})
                try:
                    q.put_nowait({"type": "_overflow"})
                except asyncio.QueueFull:
                    pass
                self.unsubscribe(brief_id, q)


hub = EventHub()
