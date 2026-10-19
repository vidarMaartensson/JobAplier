from datetime import datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel
from sqlmodel import select

from jobaplier.ai import (
    SCORING_MODEL,
    WRITING_MODEL,
    AIError,
    AnthropicAI,
    BudgetExceeded,
    cost_usd,
    monthly_spend,
)
from jobaplier.db import session_scope
from jobaplier.models import AICall


class Schema(BaseModel):
    score: int


def response(parsed="ok", stop_reason="end_turn", input_tokens=1000, output_tokens=200):
    if parsed == "ok":
        parsed = Schema(score=7)
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class StubClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make(engine, client, limit=10.0) -> AnthropicAI:
    return AnthropicAI(client, engine, monthly_limit_usd=limit)


def complete(ai: AnthropicAI, **over):
    kwargs = dict(
        model=SCORING_MODEL,
        system="sys",
        prompt="prompt",
        schema=Schema,
        purpose="score",
        effort="low",
    )
    kwargs.update(over)
    return ai.complete(**kwargs)


def test_cost_usd():
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)


def test_cost_usd_unknown_model_raises():
    with pytest.raises(AIError):
        cost_usd("okänd-modell", 1, 1)


def test_complete_returns_parsed_and_records_call(engine):
    client = StubClient(response())
    result = complete(make(engine, client))
    assert result.value == Schema(score=7)
    expected = cost_usd(SCORING_MODEL, 1000, 200)
    assert result.cost_usd == pytest.approx(expected)
    with session_scope(engine) as s:
        rows = [r.model_dump() for r in s.exec(select(AICall)).all()]
    assert len(rows) == 1
    row = rows[0]
    assert (row["purpose"], row["model"]) == ("score", SCORING_MODEL)
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["cost_usd"] == pytest.approx(expected)


def test_passes_schema_and_effort(engine):
    client = StubClient(response())
    complete(make(engine, client), model=WRITING_MODEL, max_tokens=999)
    kw = client.calls[0]
    assert kw["output_format"] is Schema
    assert kw["output_config"] == {"effort": "low"}
    assert kw["model"] == WRITING_MODEL
    assert kw["system"] == "sys"
    assert kw["max_tokens"] == 999
    assert kw["messages"] == [{"role": "user", "content": "prompt"}]
    assert "temperature" not in kw and "thinking" not in kw


def test_retries_once_when_parsed_output_none(engine):
    client = StubClient(response(parsed=None), response())
    assert complete(make(engine, client)).value.score == 7
    assert len(client.calls) == 2
    with session_scope(engine) as s:
        assert len(s.exec(select(AICall)).all()) == 2


def test_raises_after_two_invalid(engine):
    client = StubClient(response(parsed=None), response(parsed=None))
    with pytest.raises(AIError):
        complete(make(engine, client))
    assert len(client.calls) == 2


def test_refusal_raises_without_retry(engine):
    client = StubClient(response(stop_reason="refusal"), response())
    with pytest.raises(AIError):
        complete(make(engine, client))
    assert len(client.calls) == 1


def test_budget_blocks_call(engine):
    with session_scope(engine) as s:
        s.add(AICall(model=SCORING_MODEL, purpose="x", cost_usd=10.0))
    client = StubClient(response())
    with pytest.raises(BudgetExceeded):
        complete(make(engine, client, limit=10.0))
    assert client.calls == []


def test_budget_ignores_previous_months(engine):
    with session_scope(engine) as s:
        s.add(AICall(at=datetime(2026, 9, 30, 23, 59), model="m", purpose="x", cost_usd=50.0))
        s.add(AICall(at=datetime(2026, 10, 1, 0, 0), model="m", purpose="x", cost_usd=1.5))
        s.add(AICall(at=datetime(2026, 11, 1, 0, 0), model="m", purpose="x", cost_usd=99.0))
    now = datetime(2026, 10, 19, 12, 0)
    assert monthly_spend(engine, now) == pytest.approx(1.5)
    assert monthly_spend(engine, datetime(2026, 12, 5)) == 0.0


def test_api_errors_wrapped(engine):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    client = StubClient(err)
    with pytest.raises(AIError):
        complete(make(engine, client))
    assert len(client.calls) == 1


def test_api_status_error_wrapped_with_status(engine):
    req = httpx.Request("POST", "https://x")
    err = anthropic.APIStatusError(
        "overloaded", response=httpx.Response(529, request=req), body=None
    )
    with pytest.raises(AIError, match="529"):
        complete(make(engine, StubClient(err)))


def test_fake_ai_queue_function_and_exceptions():
    from fakes import FakeAI

    fake = FakeAI({Schema: [Schema(score=1), AIError("boom")]})
    first = fake.complete(model="m", system="s", prompt="p1", schema=Schema, purpose="x")
    assert first.value.score == 1 and first.cost_usd == 0.001
    with pytest.raises(AIError):
        fake.complete(model="m", system="s", prompt="p2", schema=Schema, purpose="x")
    assert [c["prompt"] for c in fake.calls] == ["p1", "p2"]
    assert fake.calls[0]["purpose"] == "x"

    by_func = FakeAI({Schema: lambda prompt: Schema(score=len(prompt))})
    assert by_func.complete(
        model="m", system="s", prompt="abc", schema=Schema, purpose="x"
    ).value.score == 3
