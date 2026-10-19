from datetime import date
from pathlib import Path

from jobaplier.apply.pdf import render_letter_pdf
from jobaplier.profile.cv import extract_cv_text


def test_renders_pdf(tmp_path: Path):
    out = tmp_path / "sub" / "brev.pdf"
    letter = "Hej Exempel AB, jag söker tjänsten.\n\nAndra stycket <b>här</b> & mer.\nNy rad."
    result = render_letter_pdf(letter, "Jane Doe", out, today=date(2026, 10, 19))
    assert result == out
    assert out.read_bytes().startswith(b"%PDF")
    text = extract_cv_text(out)
    assert "Hej Exempel AB, jag söker tjänsten." in text
    assert "Jane Doe" in text
    assert "2026-10-19" in text
    assert "<b>här</b> & mer." in text


def test_default_date_is_today(tmp_path: Path):
    out = tmp_path / "brev.pdf"
    render_letter_pdf("Hej.", "Jane Doe", out)
    assert date.today().isoformat() in extract_cv_text(out)
