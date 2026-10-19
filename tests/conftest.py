import os

# WeasyPrint hittar inte alltid Homebrews pango på macOS utan denna sökväg.
if os.path.isdir("/opt/homebrew/lib"):
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from jobaplier.config import Config, Paths, ProfileInfo  # noqa: E402


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths.from_data_dir(tmp_path / "data")
    for d in (p.data_dir, p.browser, p.logs, p.letter_examples, p.eval_dir):
        d.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def config() -> Config:
    return Config(profile=ProfileInfo(name="Test Testsson", email="test@example.se"))


@pytest.fixture
def make_pdf(tmp_path: Path):
    from weasyprint import HTML

    counter = 0

    def _make(text: str) -> Path:
        nonlocal counter
        counter += 1
        path = tmp_path / f"doc{counter}.pdf"
        html = "<html><body>" + "".join(
            f"<p>{line}</p>" for line in text.splitlines() or [""]
        ) + "</body></html>"
        HTML(string=html).write_pdf(path)
        return path

    return _make


@pytest.fixture
def engine(paths: Paths):
    from jobaplier.db import init_db, make_engine

    eng = make_engine(paths.db)
    init_db(eng)
    yield eng
    eng.dispose()
