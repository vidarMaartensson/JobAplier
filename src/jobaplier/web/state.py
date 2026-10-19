"""Delat tillstånd för webbappen. Skapas en gång i `cli` och lagras på `app.state`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine

from jobaplier.apply.service import ApplyService
from jobaplier.config import Config, Paths


@dataclass
class AppState:
    engine: Engine
    paths: Paths
    config: Callable[[], Config]
    service: ApplyService
    # Startar en körning i en bakgrundstråd; kastar `RunInProgress` om en pågår.
    run_now: Callable[[], None]
    # Namn på saker som saknas för att tjänsten ska fungera (tom lista = allt klart).
    setup_missing: Callable[[], list[str]]
    # Anropas när inställningarna sparats (schemat läggs om).
    on_config_saved: Callable[[Config], None] = lambda config: None
