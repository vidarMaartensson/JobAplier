"""Testdubletter delade mellan testfiler."""

from __future__ import annotations

import time
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from jobaplier.ai import AIClient, AIResult, T
from jobaplier.gmail import Mail


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


class FakeGmail:
    """Gmail-dubblett: `search` returnerar `mails`, `send` samlar i `sent`."""

    def __init__(
        self,
        mails: list[Mail] | None = None,
        fail_send: Exception | None = None,
        send_delay: float = 0.0,
    ) -> None:
        self.mails = mails if mails is not None else []
        self.fail_send = fail_send
        self.send_delay = send_delay
        self.sent: list[EmailMessage] = []
        self.queries: list[str] = []

    def search(self, query: str) -> list[Mail]:
        self.queries.append(query)
        return list(self.mails)

    def send(self, msg: EmailMessage) -> str:
        if self.send_delay:
            time.sleep(self.send_delay)
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(msg)
        return f"fake-{len(self.sent)}"
