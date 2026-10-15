"""Example MCP server: a tiny "company knowledge base" BriefDesk's researcher can search.

It shows the MCP-client side of BriefDesk: add it to mcp_servers.json and the Researcher agent gets a
`company_kb__search_company_docs` tool next to web search - no code change in BriefDesk.

    python examples/company_kb_mcp/server.py        # speaks MCP over stdin/stdout

Replace docs/*.md with your own notes (market research, product docs, policies ...).
"""
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DOCS = Path(__file__).parent / "docs"
server = MCPServer("company-kb", instructions="Search internal company documents (market notes, strategy).")


def _sections() -> list[tuple[str, str]]:
    out = []
    for path in sorted(DOCS.glob("*.md")):
        for block in re.split(r"\n(?=## )", path.read_text(encoding="utf-8")):
            title = block.strip().splitlines()[0].lstrip("# ").strip() if block.strip() else path.stem
            out.append((f"{path.stem} / {title}", block.strip()))
    return out


@server.tool()
def search_company_docs(query: str, max_results: int = 3) -> str:
    """Search the internal knowledge base (company market notes and strategy documents).

    Returns the best-matching sections with their document name."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    scored = []
    for name, text in _sections():
        low = text.lower()
        score = sum(low.count(w) for w in words)
        if score:
            scored.append((score, name, text))
    scored.sort(reverse=True)
    if not scored:
        return f"No internal documents match '{query}'."
    top = scored[:max(1, min(5, max_results))]
    return "\n\n---\n\n".join(f"Document: {name}\n{text[:1500]}" for _, name, text in top)


@server.tool()
def list_company_docs() -> str:
    """List the internal documents that can be searched."""
    return "\n".join(name for name, _ in _sections())


if __name__ == "__main__":
    server.run("stdio")
