"""BriefDesk as an MCP server: let Claude Desktop, Claude Code or any MCP client run research briefs.

It is a thin client over the BriefDesk REST API, authenticated with a personal API token, so every
quota, rate limit and ownership rule of the web app applies to MCP calls too.

    BRIEFDESK_URL=http://127.0.0.1:8000  BRIEFDESK_TOKEN=bd_...  python mcp_server/briefdesk_mcp.py

Claude Desktop (Settings -> Developer -> Edit config):
    {"mcpServers": {"briefdesk": {"command": "<repo>/.venv/Scripts/python.exe",
                                  "args": ["<repo>/mcp_server/briefdesk_mcp.py"],
                                  "env": {"BRIEFDESK_URL": "http://127.0.0.1:8000", "BRIEFDESK_TOKEN": "bd_..."}}}}
"""
import os
import re
import time
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("BRIEFDESK_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
TOKEN = os.environ.get("BRIEFDESK_TOKEN", "")
DOWNLOADS = Path(os.environ.get("BRIEFDESK_DOWNLOADS", Path.home() / "Downloads" / "BriefDesk"))
DONE = {"done", "failed", "cancelled", "awaiting_approval"}

server = MCPServer("briefdesk", instructions=(
    "BriefDesk runs a team of research agents (planner, researcher, writer, reviewer, citation verifier) and "
    "returns cited, verified research briefs. Use create_brief for a new topic, ask to question or revise a "
    "brief, and export_brief to get it as pdf, docx, pptx, png, xlsx, csv, md, html, json or txt."))


def _client() -> httpx.Client:
    if not TOKEN.startswith("bd_"):
        raise RuntimeError("Set BRIEFDESK_TOKEN to a BriefDesk API token (Settings & API in the web app).")
    return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)


def _check(r: httpx.Response) -> dict | list:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise RuntimeError(f"BriefDesk error {r.status_code}: {detail}")
    return r.json() if r.content else {}


def _wait(c: httpx.Client, brief_id: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    while True:
        b = _check(c.get(f"/briefs/{brief_id}"))
        if b["status"] in DONE or time.time() > deadline:
            return b
        time.sleep(3)


def _summary(b: dict) -> str:
    pct = f"{round(b['verified_ratio'] * 100)}% of citations verified" if b.get("verified_ratio") is not None else ""
    head = f"Brief #{b['id']} · {b['title']} · status: {b['status']}" + (f" · v{b['version']} · {pct}" if b["version"]
                                                                          else "")
    if b.get("report_md"):
        return head + "\n\n" + b["report_md"]
    if b["status"] == "awaiting_approval":
        return head + "\n\nPlan waiting for approval:\n" + "\n".join(f"- {q}" for q in b["plan"])
    return head + "\n\n(Still working - call get_brief again in a minute.)"


@server.tool()
def create_brief(topic: str, audience: str = "business readers", depth: str = "standard",
                 wait_seconds: int = 420) -> str:
    """Research a topic and return a cited, verified brief (Markdown with a Sources list).

    depth: quick | standard | deep. Waits up to wait_seconds; if the brief is not finished by then,
    returns its id so you can call get_brief later."""
    with _client() as c:
        b = _check(c.post("/briefs", json={"topic": topic, "audience": audience, "depth": depth,
                                          "auto_approve": True}))
        return _summary(_wait(c, b["id"], max(10, min(wait_seconds, 900))))


@server.tool()
def get_brief(brief_id: int) -> str:
    """Status and current report of a brief."""
    with _client() as c:
        return _summary(_check(c.get(f"/briefs/{brief_id}")))


@server.tool()
def list_briefs(limit: int = 20) -> str:
    """Your most recent briefs."""
    with _client() as c:
        rows = _check(c.get("/briefs", params={"limit": max(1, min(limit, 100))}))
    return "\n".join(f"#{b['id']} [{b['status']}] {b['title']}" for b in rows) or "No briefs yet."


@server.tool()
def ask(brief_id: int, message: str, wait_seconds: int = 300) -> str:
    """Talk to the brief's agents: ask a question about the report, request a revision ("add a section on
    risks"), or steer research that is running. Returns the agents' reply (and the new report for revisions)."""
    with _client() as c:
        after = max([e["id"] for e in _check(c.get(f"/briefs/{brief_id}/events"))] or [0])
        _check(c.post(f"/briefs/{brief_id}/messages", json={"text": message}))
        deadline = time.time() + max(10, min(wait_seconds, 900))
        replies: list[str] = []
        while time.time() < deadline:
            time.sleep(2)
            for e in _check(c.get(f"/briefs/{brief_id}/events", params={"after": after})):
                after = e["id"]
                p = e["payload"]
                if e["role"] == "assistant" and e["kind"] == "message":
                    replies.append(p.get("text", ""))
                elif e["kind"] == "report":
                    return "\n\n".join(replies) + "\n\n" + p.get("markdown", "")
                elif e["kind"] == "error":
                    return "\n\n".join(replies + [p.get("message", "error")])
            b = _check(c.get(f"/briefs/{brief_id}"))
            if replies and b["status"] in DONE:
                return "\n\n".join(replies)
        return "\n\n".join(replies) or "Still working - call get_brief later."


@server.tool()
def export_brief(brief_id: int, format: str = "pdf", request: str = "") -> str:
    """Export a finished brief as a file and save it to the Downloads/BriefDesk folder.

    format: pdf, docx, pptx, png, jpg, xlsx, csv, md, html, json, txt. Or describe it in `request`
    (e.g. "a one-page summary as Word", "dark infographic"). Returns the saved file path."""
    with _client() as c:
        body = {"request": request} if request else {"format": format}
        run = _check(c.post(f"/briefs/{brief_id}/exports", json=body))
        for _ in range(90):
            r = _check(c.get(f"/briefs/{brief_id}/runs/{run['run_id']}"))
            if r["status"] in ("done", "failed"):
                break
            time.sleep(2)
        if r["status"] != "done":
            raise RuntimeError(f"Export failed: {r.get('error')}")
        f = _check(c.get(f"/briefs/{brief_id}"))["files"][0]          # newest first
        data = c.get(f"/files/{f['id']}")
        data.raise_for_status()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]+", "-", f["filename"])
    path = DOWNLOADS / name
    path.write_bytes(data.content)
    return f"Saved {f['description']} ({len(data.content) // 1024} KB) to {path}"


if __name__ == "__main__":
    server.run("stdio")
