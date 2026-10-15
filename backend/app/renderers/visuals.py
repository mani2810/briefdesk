"""Images: a portrait infographic (1080x1350, LinkedIn/Instagram friendly) from an Infographic spec.

Pillow draws the layout; matplotlib draws the bar chart when the report has comparable numbers.
"""
from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .base import BRAND, FONT, FONT_BOLD, RenderInput

W, H = 1080, 1350
PAD = 64


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines or [""]


def _chart(labels: list[str], values: list[float], title: str, unit: str, dark: bool) -> Image.Image:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fg = "#e2e8f0" if dark else "#0f172a"
    fig, ax = plt.subplots(figsize=(9.5, 3.6), dpi=100)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    labels = [textwrap.shorten(lb, 22, placeholder="…") for lb in labels[:8]]
    bars = ax.barh(labels[::-1], values[:8][::-1], color="#3b82f6")
    for b, v in zip(bars, values[:8][::-1]):
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, f" {v:g}{unit if len(unit) <= 3 else ''}",
                va="center", color=fg, fontsize=12)
    ax.set_title(title, color=fg, fontsize=15, loc="left", fontweight="bold")
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(fg)
    ax.tick_params(colors=fg, labelsize=12)
    ax.set_xticks([])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def render_image(inp: RenderInput, fmt: str = "png") -> bytes:
    spec = inp.shaped
    dark = "dark" in (inp.style or "").lower()
    bg, fg, muted = ((11, 18, 32), (226, 232, 240), (148, 163, 184)) if dark else \
        ((248, 250, 252), (15, 23, 42), (100, 116, 139))
    card = (30, 41, 59) if dark else (255, 255, 255)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=BRAND)

    y = 70
    d.text((PAD, y), "RESEARCH BRIEF", font=_font(22, True), fill=BRAND)
    y += 44
    for line in _wrap(d, spec.title, _font(54, True), W - 2 * PAD)[:3]:
        d.text((PAD, y), line, font=_font(54, True), fill=fg)
        y += 64
    for line in _wrap(d, spec.subtitle, _font(28), W - 2 * PAD)[:2]:
        d.text((PAD, y + 4), line, font=_font(28), fill=muted)
        y += 38
    y += 30

    stats = spec.stats[:4]
    if stats:
        gap = 20
        cw = (W - 2 * PAD - gap * (len(stats) - 1)) // len(stats)
        for i, st in enumerate(stats):
            x = PAD + i * (cw + gap)
            d.rounded_rectangle([x, y, x + cw, y + 170], 22, fill=card)
            size = 58 if len(st.value) <= 6 else 42
            d.text((x + 24, y + 22), st.value[:12], font=_font(size, True), fill=BRAND)
            for j, line in enumerate(_wrap(d, st.label, _font(22), cw - 48)[:2]):
                d.text((x + 24, y + 104 + j * 28), line, font=_font(22), fill=muted)
        y += 200

    if spec.chart_title and len(spec.chart_values) >= 3 and len(spec.chart_labels) == len(spec.chart_values):
        chart = _chart(spec.chart_labels, spec.chart_values, spec.chart_title, spec.chart_unit, dark)
        chart.thumbnail((W - 2 * PAD, 340))
        img.paste(chart, (PAD, y), chart)
        y += chart.height + 20

    for pt in spec.points[:5]:
        if y > H - 190:
            break
        d.ellipse([PAD, y + 10, PAD + 18, y + 28], fill=BRAND)
        d.text((PAD + 36, y), pt.headline, font=_font(30, True), fill=fg)
        y += 42
        for line in _wrap(d, pt.detail, _font(25), W - 2 * PAD - 36)[:2]:
            d.text((PAD + 36, y), line, font=_font(25), fill=muted)
            y += 33
        y += 18

    footer = "Made with BriefDesk · sources: " + ", ".join(s["title"][:28] for s in inp.sources[:3])
    d.line([PAD, H - 92, W - PAD, H - 92], fill=muted, width=1)
    d.text((PAD, H - 72), textwrap.shorten(footer, 80, placeholder="…"), font=_font(20), fill=muted)
    buf = io.BytesIO()
    if fmt == "jpg":
        img.save(buf, "JPEG", quality=92, optimize=True)
    else:
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
