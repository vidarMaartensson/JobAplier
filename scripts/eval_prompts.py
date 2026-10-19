"""Manuell granskning av prompterna: poängsätter och skriver brev för sparade annonser.

Lägg 5–10 riktiga annonser som `data/eval/*.json` med fälten `title`, `company` och
`description` (valfritt `location`). Kör sedan:

    uv run python scripts/eval_prompts.py

AI-backenden är den konfigurerade (`ai.backend` i data/config.yaml). Med `api` kostar
varje annons ett poängsättnings- och ett brevanrop. Ingår inte i testsviten.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if os.path.isdir("/opt/homebrew/lib"):
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib")

from jobaplier.ai import AIError, make_ai  # noqa: E402
from jobaplier.apply.letter import load_examples, word_count, write_letter  # noqa: E402
from jobaplier.config import get_paths, load_config  # noqa: E402
from jobaplier.db import init_db, make_engine, session_scope  # noqa: E402
from jobaplier.models import Job  # noqa: E402
from jobaplier.pipeline.scoring import score_job  # noqa: E402
from jobaplier.profile.cv import load_or_build_profile, profile_of  # noqa: E402

RULE = "=" * 78


def load_ad(path: Path) -> Job:
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in ("title", "company", "description") if not data.get(key)]
    if missing:
        raise ValueError(f"{path.name}: saknar {', '.join(missing)}")
    return Job(
        dedupe_key=f"eval:{path.stem}",
        title=data["title"],
        company=data["company"],
        location=data.get("location"),
        description=data["description"],
    )


def main() -> int:
    paths = get_paths()
    ads = sorted(paths.eval_dir.glob("*.json"))
    if not ads:
        print(f"Inga annonser hittades i {paths.eval_dir} (*.json)", file=sys.stderr)
        return 1
    config = load_config(paths.config)
    engine = make_engine(paths.db)
    init_db(engine)
    ai = make_ai(config, engine, paths)
    examples = load_examples(paths.letter_examples)

    with session_scope(engine) as session:
        record = load_or_build_profile(session, paths.cv, ai)
        session.refresh(record)
        session.expunge(record)
    profile = profile_of(record)
    print(f"Backend: {config.ai.backend}. Profil: {profile.summary}\n")

    failures = 0
    for path in ads:
        print(RULE)
        try:
            job = load_ad(path)
            print(f"{path.name}: {job.title} – {job.company}\n")
            scored = score_job(job, profile, config.preferences, ai).value
            print(f"Poäng: {scored.score}")
            print(f"Motivering: {scored.reasons}")
            hint = scored.apply_method_hint
            if scored.apply_email:
                hint += f" ({scored.apply_email})"
            print(f"Ansökningsväg enligt AI: {hint}")
            letter = write_letter(job, record, ai, examples=examples).value
        except (AIError, ValueError) as exc:
            failures += 1
            print(f"FEL: {exc}")
            continue
        print(f"\nBrev ({letter.language}, {word_count(letter.letter)} ord):\n")
        print(letter.letter)
        print("\nPåståenden utan stöd i CV:t:")
        for claim in letter.unsupported_claims or ["(inga)"]:
            print(f"  - {claim}")
    print(RULE)
    engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
