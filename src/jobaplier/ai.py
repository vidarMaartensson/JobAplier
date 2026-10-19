"""AI-klienter: Claude Code-prenumerationen (standard) och Anthropic API."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine
from sqlmodel import func, select

from jobaplier.config import Config, Paths, anthropic_api_key
from jobaplier.db import session_scope
from jobaplier.models import AICall, utcnow

T = TypeVar("T", bound=BaseModel)

SCORING_MODEL = "claude-haiku-5-5"
WRITING_MODEL = "claude-sonnet-5-5"

# USD per 1M tokens: (in, ut)
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    SCORING_MODEL: (0.10, 0.50),
    WRITING_MODEL: (2.00, 10.00),
}

USAGE_LIMIT_MESSAGE = "Claude-prenumerationens användningsgräns är nådd"


class AIError(Exception):
    """AI-anrop misslyckades. Meddelandet är på svenska och kan visas för användaren."""


class BudgetExceeded(AIError):
    """Månadstaket (api) eller prenumerationens användningsgräns (claude_code) är nått."""


@dataclass
class AIResult(Generic[T]):
    value: T
    cost_usd: float


class AIClient(Protocol):
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
    ) -> AIResult[T]: ...


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    try:
        price_in, price_out = PRICES_PER_MTOK[model]
    except KeyError:
        raise AIError(f"Okänd modell '{model}': pris saknas") from None
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def monthly_spend(engine: Engine, now: datetime | None = None) -> float:
    """Summa ai_calls.cost_usd för innevarande kalendermånad (UTC)."""
    now = now or utcnow()
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    with session_scope(engine) as session:
        total = session.exec(
            select(func.coalesce(func.sum(AICall.cost_usd), 0.0)).where(
                AICall.at >= start, AICall.at < end
            )
        ).one()
    return float(total)


def _record_call(
    engine: Engine,
    *,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
) -> None:
    with session_scope(engine) as session:
        session.add(
            AICall(
                model=model,
                purpose=purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )
        )


class ClaudeCodeAI:
    """Anropar `claude -p` som subprocess; kostar inget extra (prenumerationen)."""

    def __init__(
        self,
        claude_bin: str,
        engine: Engine,
        cwd: Path,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: float = 180.0,
    ) -> None:
        self.claude_bin = claude_bin
        self.engine = engine
        self.cwd = cwd
        self.run = run
        self.timeout = timeout

    def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        purpose: str,
        max_tokens: int = 4096,  # ignoreras: CLI:n styr själv utdatalängden
        effort: str = "medium",
    ) -> AIResult[T]:
        last_problem = ""
        for _attempt in range(2):
            payload = self._invoke(model, effort, system, prompt, schema)
            self._record(payload, model, purpose)
            raw = payload.get("structured_output")
            if raw is None:
                last_problem = "svaret saknade structured_output"
                continue
            try:
                value = schema.model_validate(raw)
            except ValidationError as exc:
                last_problem = f"svaret följde inte schemat ({exc.error_count()} fel)"
                continue
            return AIResult(value=value, cost_usd=0.0)
        raise AIError(f"Claude Code gav inget giltigt svar efter två försök: {last_problem}")

    def _invoke(
        self, model: str, effort: str, system: str, prompt: str, schema: type[T]
    ) -> dict[str, Any]:
        argv = [
            self.claude_bin, "-p",
            "--model", model,
            "--effort", effort,
            "--output-format", "json",
            "--json-schema", json.dumps(schema.model_json_schema()),
            "--system-prompt", system,
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]  # fmt: skip
        try:
            proc = self.run(
                argv,
                input=prompt,
                cwd=self.cwd,
                timeout=self.timeout,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            raise AIError(
                f"Claude Code hittades inte ({self.claude_bin}). "
                "Installera det och logga in, eller byt till API-läget."
            ) from None
        except subprocess.TimeoutExpired:
            raise AIError(
                f"Claude Code svarade inte inom {self.timeout:.0f} sekunder"
            ) from None
        except OSError as exc:
            raise AIError(f"Claude Code kunde inte startas: {exc}") from exc

        payload = self._parse_stdout(proc.stdout)
        if payload is not None and payload.get("api_error_status") == 429:
            raise BudgetExceeded(USAGE_LIMIT_MESSAGE)
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise AIError(f"Claude Code avslutades med felkod {proc.returncode}: {detail}")
        if payload is None:
            raise AIError("Claude Code gav ett svar som inte är giltig JSON")
        if payload.get("is_error"):
            detail = payload.get("result") or payload.get("api_error_status") or "okänt fel"
            raise AIError(f"Claude Code rapporterade ett fel: {detail}")
        return payload

    @staticmethod
    def _parse_stdout(stdout: str | None) -> dict[str, Any] | None:
        try:
            data = json.loads(stdout or "")
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _record(self, payload: dict[str, Any], model: str, purpose: str) -> None:
        usage = payload.get("usage") or {}
        input_tokens = sum(
            usage.get(k) or 0
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        _record_call(
            self.engine,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=usage.get("output_tokens") or 0,
            cost=0.0,
        )


class AnthropicAI:
    """Anropar Anthropic API direkt; kostnad bokförs och begränsas av månadstaket."""

    def __init__(
        self, client: anthropic.Anthropic, engine: Engine, monthly_limit_usd: float
    ) -> None:
        self.client = client
        self.engine = engine
        self.monthly_limit_usd = monthly_limit_usd

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
        cost_usd(model, 0, 0)  # avvisa okända modeller innan något anropas
        total_cost = 0.0
        for _attempt in range(2):
            spent = monthly_spend(self.engine)
            if spent >= self.monthly_limit_usd:
                raise BudgetExceeded(
                    f"Månadstaket för AI-kostnad är nått "
                    f"({spent:.2f} av {self.monthly_limit_usd:.2f} USD)"
                )
            try:
                response = self.client.messages.parse(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema,
                    output_config={"effort": effort},
                )
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                status = getattr(exc, "status_code", None)
                label = f"statuskod {status}" if status else "anslutningsfel"
                raise AIError(f"Anthropic API-anrop misslyckades ({label}): {exc}") from exc
            except ValidationError:
                # SDK:t kunde inte tolka svaret mot schemat – behandla som ogiltigt svar.
                continue

            cost = cost_usd(model, response.usage.input_tokens, response.usage.output_tokens)
            total_cost += cost
            _record_call(
                self.engine,
                model=model,
                purpose=purpose,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost=cost,
            )
            if response.stop_reason == "refusal":
                raise AIError("Modellen avböjde att svara på förfrågan")
            if response.parsed_output is not None:
                return AIResult(value=response.parsed_output, cost_usd=total_cost)
        raise AIError("Anthropic API gav inget giltigt svar efter två försök")


def claude_code_available(claude_bin: str) -> bool:
    return shutil.which(claude_bin) is not None


def make_ai(config: Config, engine: Engine, paths: Paths) -> AIClient:
    if config.ai.backend == "api":
        key = anthropic_api_key()
        if not key:
            raise AIError("ANTHROPIC_API_KEY saknas i .env (krävs för API-läget)")
        client = anthropic.Anthropic(api_key=key, max_retries=4)
        return AnthropicAI(client, engine, config.ai_monthly_budget_usd)
    return ClaudeCodeAI(config.ai.claude_bin, engine, paths.data_dir)
