"""Data formats: Excel (openpyxl) and CSV from DataTables (shaped by the Publisher agent)."""
from __future__ import annotations

import csv
import io
import re

from .base import RenderInput

_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str) -> str:
    """Spreadsheet formula injection guard: text from the web must never run as a formula in Excel."""
    v = str(value)
    return "'" + v if v.startswith(_FORMULA) and not re.fullmatch(r"[-+]?\d[\d,.]*%?", v) else v


def _number(v: str):
    s = v.strip().replace(",", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s) if "." in s else int(s)
    return v


def render_xlsx(inp: RenderInput) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    header_fill, header_font = PatternFill("solid", fgColor="2563EB"), Font(bold=True, color="FFFFFF")
    used = set()
    for t in inp.shaped.tables[:10] or []:
        name = re.sub(r"[\[\]:*?/\\]", " ", t.name)[:28].strip() or "Data"
        while name in used:
            name = name[:25] + f" {len(used)}"
        used.add(name)
        ws = wb.create_sheet(name)
        ws.append([_safe_cell(c) for c in t.columns])
        for row in t.rows[:500]:
            ws.append([_number(_safe_cell(c)) for c in row])
        for cell in ws[1]:
            cell.fill, cell.font = header_fill, header_font
        for i, col in enumerate(ws.columns, 1):
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
            for c in col:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
    src = wb.create_sheet("Sources")
    src.append(["#", "Title", "URL"])
    for cell in src[1]:
        cell.fill, cell.font = header_fill, header_font
    for s in inp.sources:
        src.append([s["n"], _safe_cell(s["title"]), _safe_cell(s["url"])])
    src.column_dimensions["B"].width, src.column_dimensions["C"].width = 60, 70
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_csv(inp: RenderInput) -> bytes:
    """CSV holds one table: the largest one (Excel keeps them all)."""
    tables = inp.shaped.tables or []
    buf = io.StringIO()
    w = csv.writer(buf)
    if tables:
        t = max(tables, key=lambda t: len(t.rows))
        w.writerow([_safe_cell(c) for c in t.columns])
        for row in t.rows:
            w.writerow([_safe_cell(c) for c in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")        # BOM: Excel opens UTF-8 (₹) correctly
