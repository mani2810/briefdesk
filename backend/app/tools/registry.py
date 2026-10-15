"""Tool registry: what the Researcher agent may call.

Built-in tools (web_search, read_page) and tools discovered on MCP servers are exposed to the model
through ONE interface, so the agent is tool-agnostic: adding an MCP server to mcp_servers.json gives the
researcher a new tool without any code change.

A tool returns text for the model plus optional *documents*. Documents become numbered sources [n]
that the report can cite and the verifier can check.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.config import get_settings
from . import web


@dataclass
class Document:
    url: str
    title: str
    content: str
    snippet: str = ""
    origin: str = "web"


@dataclass
class ToolOutput:
    text: str
    documents: list[Document] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolOutput]
    origin: str = "builtin"

    def schema(self) -> dict:
        """OpenAI-style function schema (what bind_tools expects)."""
        return {"type": "function", "function": {"name": self.name, "description": self.description[:1000],
                                                 "parameters": self.parameters}}


def _web_search(args: dict) -> ToolOutput:
    query = str(args.get("query", "")).strip()[:300]
    if not query:
        return ToolOutput("ERROR: query is required")
    hits = web.web_search(query)
    if not hits:
        return ToolOutput(f"No results for '{query}'. Try different words.")
    lines = [f"- {h.title[:120]}\n  URL: {h.url}\n  {h.snippet[:170]}" for h in hits]
    return ToolOutput(f"Search results for '{query}' (use read_page on the most useful URLs):\n" + "\n".join(lines))


def _read_page(args: dict) -> ToolOutput:
    url = str(args.get("url", "")).strip()
    focus = str(args.get("focus", "")).strip()[:300]
    try:
        page = web.fetch_page(url)
    except web.FetchError as exc:
        return ToolOutput(f"Could not read {url}: {exc}. Pick another result.")
    except Exception as exc:  # noqa: BLE001 - network errors are just a failed read for the agent
        return ToolOutput(f"Could not read {url}: {type(exc).__name__}. Pick another result.")
    passage = web.relevant_passages(page.text, focus or page.title, max_words=320)
    return ToolOutput(passage, [Document(page.url, page.title, page.text[:20000], passage[:400], "web")])


BUILTIN_TOOLS = [
    Tool("web_search", "Search the web. Returns titles, URLs and snippets.",
         {"type": "object", "properties": {"query": {"type": "string", "description": "search query"}},
          "required": ["query"]}, _web_search),
    Tool("read_page", "Read a web page and return the passages most relevant to `focus`. Each page you read "
                      "becomes a numbered source [n] you can cite.",
         {"type": "object", "properties": {
             "url": {"type": "string", "description": "http(s) URL from the search results"},
             "focus": {"type": "string", "description": "what you are looking for on this page"}},
          "required": ["url", "focus"]}, _read_page),
]


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or BUILTIN_TOOLS)}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> ToolOutput:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutput(f"ERROR: unknown tool {name}. Available: {', '.join(self._tools)}")
        out = tool.handler(args if isinstance(args, dict) else {})
        if tool.origin != "builtin":
            # MCP result: one citable document (stable pseudo-URL per tool + arguments)
            digest = hashlib.sha256(repr(sorted(args.items())).encode()).hexdigest()[:10]
            if out.text and not out.text.startswith("ERROR") and not out.documents:
                out.documents = [Document(f"mcp://{tool.origin.split(':', 1)[-1]}/{name}#{digest}",
                                          f"{tool.name} ({tool.origin})", out.text[:20000], out.text[:400],
                                          tool.origin)]
        return out


def get_registry() -> ToolRegistry:
    """Built-in tools + whatever MCP servers are ready right now (servers start in the background)."""
    tools = list(BUILTIN_TOOLS)
    if get_settings().mcp_client_enabled:
        from .mcp_client import current_hub
        hub = current_hub()
        if hub is not None:
            tools += hub.tools()
    return ToolRegistry(tools)
