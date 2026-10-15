"""Renderers: turn a report (plus shaped data) into the file format the user asked for."""
from .base import FORMATS, RenderInput, normalise_format
from .data import render_csv, render_xlsx
from .documents import render_docx, render_html, render_json, render_md, render_pdf, render_txt
from .slides import render_pptx
from .visuals import render_image

RENDERERS = {
    "pdf": render_pdf, "docx": render_docx, "html": render_html, "md": render_md, "txt": render_txt,
    "json": render_json, "pptx": render_pptx, "xlsx": render_xlsx, "csv": render_csv,
    "png": lambda inp: render_image(inp, "png"), "jpg": lambda inp: render_image(inp, "jpg"),
}


def render(fmt: str, inp: RenderInput) -> bytes:
    return RENDERERS[fmt](inp)


__all__ = ["FORMATS", "RENDERERS", "RenderInput", "normalise_format", "render"]
