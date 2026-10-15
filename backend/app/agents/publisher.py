"""Publisher agent: "give me this as Word" / "make an infographic" / "slides for my manager" -> a file.

    1. understand   free text -> ExportSpec {format, scope, style} (fast model; keyword shortcut first)
    2. normalise    aliases (Word -> docx) and honest substitutions (video -> slides) with a note
    3. select       full report, an executive summary (LLM), or one section
    4. shape        formats that need a different structure get one: slides (SlideDeck), images
                    (Infographic), spreadsheets (DataTables). If the LLM is unavailable, a deterministic
                    shaper builds them from the Markdown - an export never fails because of the model.
    5. render       renderers/ produce the bytes
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from ..llm.gateway import LLMGateway, LLMUnavailable
from ..renderers import FORMATS, RenderInput, normalise_format, render
from ..renderers import blocks as B
from . import prompts
from .schemas import DataTables, ExportSpec, Infographic, KeyPoint, Slide, SlideDeck, StatCard, Table

KEYWORDS = [  # cheap first pass, no LLM call needed for the common phrasings
    (r"\b(power ?point|pptx?|slides?|deck|presentation)\b", "pptx"),
    (r"\b(word|docx?)\b", "docx"),
    (r"\b(excel|xlsx?|spreadsheet|workbook)\b", "xlsx"),
    (r"\bcsv\b", "csv"),
    (r"\b(jpe?g)\b", "jpg"),
    (r"\b(image|png|infographic|picture|poster|visual|graphic|card)\b", "png"),
    (r"\bpdf\b", "pdf"),
    (r"\b(markdown|\.md)\b", "md"),
    (r"\b(html|web ?page)\b", "html"),
    (r"\bjson\b", "json"),
    (r"\b(txt|plain text|text file)\b", "txt"),
    (r"\b(video|mp4|gif|svg|audio|mp3|podcast|epub|rtf|odt)\b", None),   # needs a substitution
]


@dataclass
class ExportResult:
    format: str
    data: bytes
    filename: str
    mime: str
    description: str
    note: str | None = None


def understand(gw: LLMGateway | None, text: str) -> ExportSpec:
    low = text.lower()
    scope = "summary" if re.search(r"\b(summary|summari[sz]e|one[- ]pager|short|brief version|tl;?dr)\b", low) \
        else "full"
    for pattern, fmt in KEYWORDS:
        m = re.search(pattern, low)
        if m:
            if fmt is None:
                fmt = m.group(1)
            section = ""
            sm = re.search(r"\b(?:the|only)\s+([\w\s-]{3,40}?)\s+section\b", low)
            if sm:
                scope, section = "section", sm.group(1)
            return ExportSpec(format=fmt, scope=scope, section=section, style=text[:200])
    if gw is not None:
        try:
            return gw.structured("export_spec", [SystemMessage(prompts.EXPORT_SPEC), HumanMessage(text[:500])],
                                 ExportSpec)
        except LLMUnavailable:
            pass
    return ExportSpec(format="pdf", scope=scope, section="", style=text[:200])


# ---------------------------------------------------------------- deterministic shapers (fallbacks)
def _sentences(markdown: str) -> list[str]:
    out = []
    for blk in B.parse(markdown):
        if isinstance(blk, B.ListBlock):
            out += [B.plain(r) for _, r in blk.items]
        elif isinstance(blk, B.Paragraph):
            out += [s for s in re.split(r"(?<=[.!?])\s+", B.plain(blk.runs)) if len(s.split()) > 4]
    return out


def shape_slides(markdown: str, title: str) -> SlideDeck:
    slides, current, bullets = [], None, []
    for blk in B.parse(markdown.split("\n## Sources")[0]):
        if isinstance(blk, B.Heading) and blk.level == 2:
            if current:
                slides.append(Slide(title=current, bullets=bullets[:5] or ["(see report)"], notes=""))
            current, bullets = B.plain(blk.runs), []
        elif current and isinstance(blk, B.ListBlock):
            bullets += [B.plain(r)[:160] for _, r in blk.items]
        elif current and isinstance(blk, B.Paragraph):
            bullets += [s[:160] for s in re.split(r"(?<=[.!?])\s+", B.plain(blk.runs)) if len(s.split()) > 3]
    if current:
        slides.append(Slide(title=current, bullets=bullets[:5] or ["(see report)"], notes=""))
    return SlideDeck(title=title, subtitle="Research brief", slides=slides[:9])


def shape_infographic(markdown: str, title: str) -> Infographic:
    sents = _sentences(markdown.split("\n## Sources")[0])
    stats = []
    for s in sents:
        m = re.search(r"(₹\s?[\d,.]+\s?(?:crore|lakh|bn|billion|million)?|\$[\d,.]+\s?(?:bn|billion|million)?|"
                      r"\d+(?:\.\d+)?\s?%)", s)
        if m and len(stats) < 3:
            label = B.strip_citations(s.replace(m.group(1), "")).strip(" .,:;")
            stats.append(StatCard(value=m.group(1).replace(" ", ""), label=" ".join(label.split()[:6])))
    heads = re.findall(r"^## (.+)$", markdown, re.M)
    points = []
    for h in heads:
        if h.lower().startswith(("sources", "executive")):
            continue
        body = markdown.split(f"## {h}", 1)[1].split("\n## ", 1)[0]
        first = next(iter(_sentences(body)), "")
        points.append(KeyPoint(headline=h[:60], detail=" ".join(B.strip_citations(first).split()[:22])))
    return Infographic(title=title, subtitle="Key facts at a glance", stats=stats, points=points[:5],
                       chart_title="", chart_labels=[], chart_values=[], chart_unit="")


def shape_tables(markdown: str) -> DataTables:
    tables = []
    for blk in B.parse(markdown):
        if isinstance(blk, B.TableBlock) and blk.header:
            tables.append(Table(name=f"Table {len(tables) + 1}", columns=[B.plain(c) for c in blk.header],
                                rows=[[B.plain(c) for c in r] for r in blk.rows]))
    facts = [[B.strip_citations(s), ", ".join(re.findall(r"\[(\d+)\]", s))] for s in _sentences(markdown)
             if "[" in s]
    tables.append(Table(name="Key facts", columns=["Fact", "Source"], rows=facts[:60]))
    return DataTables(tables=tables)


# ---------------------------------------------------------------- main entry
def export(gw: LLMGateway | None, request: str | ExportSpec, title: str, markdown: str, sources: list[dict],
           meta: dict) -> ExportResult:
    spec = request if isinstance(request, ExportSpec) else understand(gw, request)
    fmt, note = normalise_format(spec.format)
    if fmt is None:
        fmt, note = "pdf", (note or "") + " Here is a PDF instead."

    content, scope_label = markdown, "report"
    if spec.scope == "section" and spec.section:
        part = B.section(markdown, spec.section)
        if part:
            content, scope_label = part, spec.section
        else:
            note = (note + " " if note else "") + f"I couldn't find a '{spec.section}' section, so this is the " \
                                                  "full report."
    elif spec.scope == "summary":
        content, scope_label = summarise(gw, markdown, meta.get("audience", "")), "summary"

    shaped = None
    family = FORMATS[fmt].family
    style = f"User request / style: {spec.style}" if spec.style else ""
    body = content.split("\n## Sources")[0]
    try:
        if family == "slides":
            shaped = gw.structured("slides", [SystemMessage(prompts.SLIDES.format(style=style)),
                                              HumanMessage(body[:12000])], SlideDeck, tier="large")
            if not shaped.slides:
                raise LLMUnavailable("empty deck")
        elif family == "image":
            shaped = gw.structured("infographic", [SystemMessage(prompts.INFOGRAPHIC.format(style=style)),
                                                   HumanMessage(body[:12000])], Infographic, tier="large")
        elif fmt in ("xlsx", "csv"):
            shaped = gw.structured("tables", [SystemMessage(prompts.TABLES.format(style=style)),
                                              HumanMessage(body[:12000])], DataTables, tier="large")
            if not shaped.tables:
                raise LLMUnavailable("no tables")
    except (LLMUnavailable, AttributeError):          # AttributeError: gw is None (no model configured)
        shaped = None
    if shaped is None:
        if family == "slides":
            shaped = shape_slides(content, title)
        elif family == "image":
            shaped = shape_infographic(content, title)
        elif fmt in ("xlsx", "csv"):
            shaped = shape_tables(content)

    cited = {int(n) for n in re.findall(r"\[(\d{1,3})\]", content)}
    used_sources = [s for s in sources if s["n"] in cited] or sources
    inp = RenderInput(title=title, markdown=content, sources=used_sources, meta=meta, shaped=shaped,
                      style=spec.style)
    data = render(fmt, inp)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "brief"
    suffix = "" if scope_label == "report" else "-" + re.sub(r"[^a-z0-9]+", "-", scope_label.lower()).strip("-")[:30]
    info = FORMATS[fmt]
    return ExportResult(fmt, data, f"{slug}{suffix}.{info.ext}", info.mime,
                        f"{info.label} · {scope_label}", note)


def summarise(gw: LLMGateway | None, markdown: str, audience: str) -> str:
    try:
        if gw is None:
            raise LLMUnavailable("no model")
        return gw.text("summary", [SystemMessage(prompts.SUMMARY), HumanMessage(
            f"Audience: {audience}\n\n<draft>\n{markdown.split(chr(10) + '## Sources')[0][:12000]}\n</draft>")],
            tier="large", max_tokens=700)
    except LLMUnavailable:
        title = B.title_of(markdown)
        summary = markdown.split("## Executive summary", 1)[-1].split("\n## ", 1)[0].strip() \
            if "## Executive summary" in markdown else " ".join(_sentences(markdown)[:4])
        return f"# {title} - summary\n\n{summary}"
