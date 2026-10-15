"""Markdown -> a small, format-neutral block model that every renderer understands.

    Heading(level, runs) | Paragraph(runs) | ListBlock(ordered, items=[(depth, runs)]) | TableBlock(header, rows)
    | CodeBlock(text) | Rule() | Quote(runs)

Runs are (text, Style) pairs with bold / italic / code / link, so PDF, Word, HTML and slides render
the same report identically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: str | None = None


Run = tuple[str, Style]


@dataclass
class Heading:
    level: int
    runs: list[Run]


@dataclass
class Paragraph:
    runs: list[Run]


@dataclass
class ListBlock:
    ordered: bool
    items: list[tuple[int, list[Run]]] = field(default_factory=list)


@dataclass
class TableBlock:
    header: list[list[Run]]
    rows: list[list[list[Run]]]


@dataclass
class CodeBlock:
    text: str


@dataclass
class Rule:
    pass


@dataclass
class Quote:
    runs: list[Run]


Block = Heading | Paragraph | ListBlock | TableBlock | CodeBlock | Rule | Quote
_md = MarkdownIt("commonmark").enable("table").enable("strikethrough")


def plain(runs: list[Run]) -> str:
    return "".join(t for t, _ in runs)


def _inline(token) -> list[Run]:
    runs: list[Run] = []
    bold = italic = 0
    link: list[str] = []
    for child in token.children or []:
        t = child.type
        if t == "text":
            runs.append((child.content, Style(bold > 0, italic > 0, False, link[-1] if link else None)))
        elif t == "code_inline":
            runs.append((child.content, Style(bold > 0, italic > 0, True, link[-1] if link else None)))
        elif t in ("softbreak", "hardbreak"):
            runs.append((" ", Style()))
        elif t == "strong_open":
            bold += 1
        elif t == "strong_close":
            bold -= 1
        elif t == "em_open":
            italic += 1
        elif t == "em_close":
            italic -= 1
        elif t == "link_open":
            href = str(child.attrs.get("href", ""))
            link.append(href if href.startswith(("http://", "https://", "mailto:")) else "")
        elif t == "link_close" and link:
            link.pop()
        elif t == "image":
            runs.append((child.content or "", Style()))
    merged: list[Run] = []
    for text, style in runs:                      # merge neighbours with the same style
        if merged and merged[-1][1] == style:
            merged[-1] = (merged[-1][0] + text, style)
        else:
            merged.append((text, style))
    return merged


def parse(markdown: str) -> list[Block]:
    tokens = _md.parse(markdown or "")
    blocks: list[Block] = []
    list_stack: list[ListBlock] = []
    table: TableBlock | None = None
    row: list[list[Run]] | None = None
    in_head = in_quote = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            blocks.append(Heading(int(tok.tag[1]), _inline(tokens[i + 1])))
            i += 3
            continue
        if t in ("bullet_list_open", "ordered_list_open"):
            if not list_stack:
                lb = ListBlock(t == "ordered_list_open")
                blocks.append(lb)
                list_stack.append(lb)
            else:
                list_stack.append(list_stack[-1])
        elif t in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
        elif t == "blockquote_open":
            in_quote = True
        elif t == "blockquote_close":
            in_quote = False
        elif t == "table_open":
            table = TableBlock([], [])
            blocks.append(table)
        elif t == "table_close":
            table = None
        elif t == "thead_open":
            in_head = True
        elif t == "thead_close":
            in_head = False
        elif t == "tr_open":
            row = []
        elif t == "tr_close" and table is not None and row is not None:
            if in_head:
                table.header = row
            else:
                table.rows.append(row)
            row = None
        elif t in ("th_open", "td_open") and row is not None:
            row.append(_inline(tokens[i + 1]))
            i += 3
            continue
        elif t == "inline":
            runs = _inline(tok)
            if list_stack:
                list_stack[-1].items.append((len(list_stack) - 1, runs))
            elif in_quote:
                blocks.append(Quote(runs))
            else:
                blocks.append(Paragraph(runs))
        elif t in ("fence", "code_block"):
            blocks.append(CodeBlock(tok.content.rstrip("\n")))
        elif t == "hr":
            blocks.append(Rule())
        i += 1
    return blocks


def title_of(markdown: str, default: str = "Research brief") -> str:
    m = re.search(r"^#\s+(.+)$", markdown or "", re.M)
    return m.group(1).strip() if m else default


def section(markdown: str, name: str) -> str | None:
    """The '## name' section (fuzzy heading match), with the report title on top."""
    parts = re.split(r"(?m)^(?=##\s)", markdown)
    want = set(re.findall(r"[a-z0-9]+", name.lower()))
    best, best_score = None, 0.0
    for part in parts[1:]:
        heading = part.splitlines()[0]
        have = set(re.findall(r"[a-z0-9]+", heading.lower()))
        score = len(want & have) / max(1, len(want))
        if score > best_score:
            best, best_score = part, score
    if best is None or best_score < 0.5:
        return None
    return f"# {title_of(markdown)}\n\n{best.strip()}"


def strip_citations(text: str) -> str:
    return re.sub(r"\s*\[\d{1,3}\]", "", text)
