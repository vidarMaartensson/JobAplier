from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
FORBIDDEN = ("googleapiclient", "google.oauth2", "jobaplier.gmail")


def test_no_gmail_imports():
    offenders = [
        f"{path.relative_to(SRC)}: {word}"
        for path in SRC.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".yaml"}
        for word in FORBIDDEN
        if word in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
