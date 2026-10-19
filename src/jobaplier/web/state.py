"""Delat tillstånd för webbappen. Skapas en gång i `cli` och lagras på `app.state`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from jobaplier.apply.service import ApplyService
from jobaplier.config import Config, Paths
from jobaplier.profile.cv_document import CVDocument


@dataclass
class AppState:
    engine: Engine
    paths: Paths
    config: Callable[[], Config]
    service: ApplyService
    # Startar en körning i en bakgrundstråd; kastar `RunInProgress` om en pågår.
    run_now: Callable[[], None]
    # Startar en körning med bara de inklistrade LinkedIn-jobben (jobb-id:n) i en
    # bakgrundstråd; kastar `RunInProgress` om en körning pågår.
    import_links: Callable[[list[str]], None]
    # Namn på saker som saknas för att tjänsten ska fungera (tom lista = allt klart).
    setup_missing: Callable[[], list[str]]
    # CV:t för ett utkast (`draft_id`, målfil), som `apply.package.build_cv_pdf`:
    # `(fil, skräddarsytt)`; utan skräddarsytt CV är filen originalet `paths.cv`.
    cv_pdf: Callable[[int, Path], tuple[Path, bool]]
    # Det strukturerade CV:t, eller `None` om CV:t saknas, inte kan läsas eller struktureras.
    cv_document: Callable[[], CVDocument | None]
    # Anropas när inställningarna sparats (schemat läggs om).
    on_config_saved: Callable[[Config], None] = lambda config: None
    # Faktavarningar för det strukturerade CV:t (`cv_document_warnings`); tom lista om
    # inga finns eller CV:t saknas.
    cv_warnings: Callable[[], list[str]] = lambda: []
