"""Shared types for renderers and the format catalogue the Publisher agent chooses from."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"     # DejaVu: wide Unicode coverage (₹, €, –)
FONT = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
FONT_ITALIC = FONT_DIR / "DejaVuSans-Oblique.ttf"
FONT_BOLD_ITALIC = FONT_DIR / "DejaVuSans-BoldOblique.ttf"
FONT_MONO = FONT_DIR / "DejaVuSansMono.ttf"

BRAND = (37, 99, 235)          # #2563eb
INK = (15, 23, 42)             # #0f172a
MUTED = (100, 116, 139)        # #64748b


@dataclass
class FormatInfo:
    ext: str
    mime: str
    family: str                 # document | slides | image | data
    label: str


FORMATS: dict[str, FormatInfo] = {
    "pdf": FormatInfo("pdf", "application/pdf", "document", "PDF document"),
    "docx": FormatInfo("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       "document", "Word document"),
    "html": FormatInfo("html", "text/html", "document", "Web page"),
    "md": FormatInfo("md", "text/markdown", "document", "Markdown"),
    "txt": FormatInfo("txt", "text/plain", "document", "Plain text"),
    "json": FormatInfo("json", "application/json", "data", "JSON"),
    "pptx": FormatInfo("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                       "slides", "PowerPoint deck"),
    "png": FormatInfo("png", "image/png", "image", "Infographic image (PNG)"),
    "jpg": FormatInfo("jpg", "image/jpeg", "image", "Infographic image (JPG)"),
    "xlsx": FormatInfo("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "data",
                       "Excel workbook"),
    "csv": FormatInfo("csv", "text/csv", "data", "CSV table"),
}

ALIASES = {
    "word": "docx", "doc": "docx", "msword": "docx", "powerpoint": "pptx", "ppt": "pptx", "slides": "pptx",
    "deck": "pptx", "presentation": "pptx", "image": "png", "picture": "png", "infographic": "png",
    "poster": "png", "jpeg": "jpg", "photo": "jpg", "excel": "xlsx", "xls": "xlsx", "spreadsheet": "xlsx",
    "sheet": "xlsx", "markdown": "md", "text": "txt", "web": "html", "htm": "html", "webpage": "html",
}
# What we offer instead of formats we cannot make
CLOSEST = {"mp4": "pptx", "video": "pptx", "gif": "png", "svg": "png", "webp": "png", "mp3": "txt", "audio": "txt",
           "epub": "pdf", "rtf": "docx", "odt": "docx", "odp": "pptx", "ods": "xlsx", "tex": "md", "latex": "md",
           "xml": "json", "yaml": "json", "tsv": "csv"}


def normalise_format(raw: str) -> tuple[str | None, str | None]:
    """-> (supported format, note). note explains a substitution; (None, note) if nothing fits."""
    f = (raw or "").strip().lower().lstrip(".")
    if f in FORMATS:
        return f, None
    if f in ALIASES:
        return ALIASES[f], None
    if f in CLOSEST:
        alt = CLOSEST[f]
        return alt, f"I can't produce {f.upper()} files, so here is a {FORMATS[alt].label} instead."
    return None, f"I can't produce '{raw}' files."


@dataclass
class RenderInput:
    title: str
    markdown: str                              # the report (or the chosen part of it)
    sources: list[dict] = field(default_factory=list)   # [{n, title, url}]
    meta: dict[str, Any] = field(default_factory=dict)  # audience, version, verified_ratio, generated
    shaped: Any = None                         # SlideDeck / Infographic / DataTables when the format needs it
    style: str = ""
