"""Testdubletter delade mellan testfiler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jobaplier.ai import AIClient, AIResult, T


class FakeAI(AIClient):
    """AI-dubblett. `responses` mappar schematyp till antingen en kö (lista) av
    svar eller en funktion prompt -> svar. Ett köat Exception-objekt kastas."""

    def __init__(self, responses: dict[type, list[Any] | Callable[[str], Any]]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        purpose: str,
        max_tokens: int = 4096,
        effort: str = "medium",
    ) -> AIResult[T]:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "purpose": purpose,
            }
        )
        source = self.responses[schema]
        result = source(prompt) if callable(source) else source.pop(0)
        if isinstance(result, Exception):
            raise result
        return AIResult(value=result, cost_usd=0.001)
