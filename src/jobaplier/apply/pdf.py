"""Renderar ett personligt brev till PDF med WeasyPrint."""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from weasyprint import HTML

STYLE = """
@page { size: A4; margin: 2.5cm; }
body { font-family: sans-serif; font-size: 11pt; line-height: 1.4; }
.name { font-weight: bold; margin: 0; }
.date { margin: 0 0 1.5em 0; }
p { margin: 0 0 1em 0; }
"""


def _paragraph(text: str) -> str:
    return "<p>" + html.escape(text.strip()).replace("\n", "<br>") + "</p>"


def render_letter_pdf(
    letter: str, applicant_name: str, out: Path, *, today: date | None = None
) -> Path:
    today = today or date.today()
    paragraphs = [p for p in re.split(r"\n\s*\n", letter.strip()) if p.strip()]
    body = (
        f'<p class="name">{html.escape(applicant_name)}</p>'
        f'<p class="date">{today.isoformat()}</p>'
        + "".join(_paragraph(p) for p in paragraphs)
    )
    document = (
        "<html><head><meta charset='utf-8'>"
        f"<style>{STYLE}</style></head><body>{body}</body></html>"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=document).write_pdf(out)
    return out
