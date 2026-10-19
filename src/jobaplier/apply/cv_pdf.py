"""Renderar det skräddarsydda CV:t till PDF via Jinja2 och WeasyPrint."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from jobaplier.apply.tailor import RenderedCV

HEADINGS: dict[str, dict[str, str]] = {
    "sv": {
        "summary": "Profil",
        "experience": "Erfarenhet",
        "education": "Utbildning",
        "skills": "Kompetenser",
        "languages": "Språk",
    },
    "en": {
        "summary": "Profile",
        "experience": "Experience",
        "education": "Education",
        "skills": "Skills",
        "languages": "Languages",
    },
}

TEMPLATE_DIR = Path(__file__).parent / "templates"


def one_line(text: object) -> str:
    """Slår ihop blanksteg och radbrytningar (Claude kan skicka punkter med radbrytningar)."""
    return " ".join(str(text).split())


_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"], default=True),
)
_env.filters["one_line"] = one_line


def render_cv_pdf(cv: RenderedCV, out: Path) -> Path:
    contact = [
        one_line(part)
        for part in (cv.email, cv.phone, cv.location, cv.linkedin_url)
        if one_line(part)
    ]
    html = _env.get_template("cv.html").render(
        cv=cv, headings=HEADINGS[cv.language], contact=contact
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(out)
    return out
