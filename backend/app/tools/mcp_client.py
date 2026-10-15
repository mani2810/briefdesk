"""MCP client: plug any MCP server into the Researcher as extra tools - configuration, not code.

mcp_servers.json (repo root, git-ignored; copy mcp_servers.example.json):

    {"servers": {
        "company_kb": {
            "command": "python", "args": ["examples/company_kb_mcp/server.py"],
            "allowed_tools": ["search_company_docs"],          # allow-list: nothing else is exposed
            "description": "Internal market notes"
        }}}

Design:
    * one background asyncio loop thread owns every MCP session (agents run in worker threads and call
      tools synchronously through run_coroutine_threadsafe)
    * each server is held open by its own long-lived task (anyio cancel scopes must exit in the task that
      entered them), started at app start-up and closed at shutdown
    * a server that fails to start is logged and skipped - it never takes the app down
    * tool output is untrusted text: the researcher treats it exactly like a web page
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.config import REPO_DIR, get_settings
from .registry import Tool, ToolOutput

log = logging.getLogger(__name__)


@dataclass
class ServerState:
    name: str
    config: dict
    status: str = "starting"           # starting | ready | failed | stopped
    error: str | None = None
    tools: list[Any] = field(default_factory=list)
    client: Any = None
    stop: asyncio.Event | None = None


class MCPHub:
    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.servers: dict[str, ServerState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- lifecycle
    def start(self, timeout: float = 25) -> None:
        servers = (self.config or {}).get("servers", {})
        if not servers:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-hub", daemon=True)
        self._thread.start()
        futures = {}
        for name, cfg in servers.items():
            state = self.servers[name] = ServerState(name, cfg)
            ready = asyncio.run_coroutine_threadsafe(self._ready_event(), self._loop).result()
            asyncio.run_coroutine_threadsafe(self._hold(state, ready), self._loop)
            futures[name] = ready
        for name, ready in futures.items():
            try:
                asyncio.run_coroutine_threadsafe(asyncio.wait_for(ready.wait(), timeout), self._loop).result()
            except Exception:  # noqa: BLE001
                self.servers[name].status, self.servers[name].error = "failed", "did not start in time"
            st = self.servers[name]
            log.info("mcp server %s: %s", name, st.status,
                     extra={"tools": [t.name for t in st.tools], "error": st.error})

    @staticmethod
    async def _ready_event() -> asyncio.Event:
        return asyncio.Event()

    async def _hold(self, st: ServerState, ready: asyncio.Event) -> None:
        from mcp import Client, StdioServerParameters

        cfg = st.config
        command = cfg.get("command", "")
        if command in ("python", "python3"):
            command = sys.executable                  # run example servers with this venv's Python
        params = StdioServerParameters(command=command, args=[str(a) for a in cfg.get("args", [])],
                                       env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()} or None,
                                       cwd=str(REPO_DIR))
        st.stop = asyncio.Event()
        try:
            async with Client(params, read_timeout_seconds=get_settings().mcp_call_timeout_seconds) as client:
                listed = await client.list_tools()
                allowed = set(cfg.get("allowed_tools") or [])
                st.tools = [t for t in listed.tools if not allowed or t.name in allowed]
                st.client, st.status = client, "ready"
                ready.set()
                await st.stop.wait()
        except Exception as exc:  # noqa: BLE001 - a broken server is reported, never fatal
            st.status, st.error = "failed", f"{type(exc).__name__}: {exc}"[:300]
            log.warning("mcp server %s failed: %s", st.name, st.error)
        finally:
            st.client = None
            if st.status == "ready":
                st.status = "stopped"
            ready.set()

    def stop(self) -> None:
        if not self._loop:
            return
        for st in self.servers.values():
            if st.stop is not None:
                self._loop.call_soon_threadsafe(st.stop.set)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None

    # ---------------------------------------------------------- tools
    def call(self, server: str, tool: str, args: dict) -> ToolOutput:
        st = self.servers.get(server)
        if not st or st.status != "ready" or not self._loop:
            return ToolOutput(f"ERROR: MCP server '{server}' is not available")
        timeout = get_settings().mcp_call_timeout_seconds
        try:
            result = asyncio.run_coroutine_threadsafe(st.client.call_tool(tool, args), self._loop).result(timeout)
        except Exception as exc:  # noqa: BLE001
            return ToolOutput(f"ERROR: {tool} failed ({type(exc).__name__})")
        texts = [getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"]
        if not texts and result.structured_content is not None:
            texts = [json.dumps(result.structured_content)[:20000]]
        text = "\n".join(t for t in texts if t).strip()[:20000]
        return ToolOutput(("ERROR: " if result.is_error else "") + (text or "(no text content)"))

    def tools(self) -> list[Tool]:
        out = []
        for st in self.servers.values():
            if st.status != "ready":
                continue
            for t in st.tools:
                schema = t.input_schema if isinstance(t.input_schema, dict) else {"type": "object", "properties": {}}
                desc = (st.config.get("description", "") + ": " if st.config.get("description") else "") \
                    + (t.description or t.name)
                out.append(Tool(f"{st.name}__{t.name}"[:64], f"[MCP {st.name}] {desc}", schema,
                                lambda args, s=st.name, n=t.name: self.call(s, n, args), origin=f"mcp:{st.name}"))
        return out

    def status(self) -> list[dict]:
        return [{"name": st.name, "status": st.status, "error": st.error,
                 "tools": [t.name for t in st.tools], "description": st.config.get("description", "")}
                for st in self.servers.values()]


_hub: MCPHub | None = None


def load_config() -> dict:
    path = get_settings().mcp_config_path
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read %s: %s", path, exc)
        return {}


def start_hub_background() -> None:
    """Start MCP servers without delaying API start-up (a slow server only delays its own tools)."""
    global _hub
    if _hub is not None:
        return
    _hub = MCPHub(load_config())
    threading.Thread(target=_hub.start, name="mcp-start", daemon=True).start()


def current_hub() -> MCPHub | None:
    return _hub


def shutdown_hub() -> None:
    global _hub
    if _hub is not None:
        _hub.stop()
        _hub = None
