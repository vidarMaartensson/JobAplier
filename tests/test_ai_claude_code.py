import json
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlmodel import select

from jobaplier.ai import (
    SCORING_MODEL,
    AIError,
    AnthropicAI,
    BudgetExceeded,
    ClaudeCodeAI,
    claude_code_available,
    make_ai,
)
from jobaplier.db import session_scope
from jobaplier.models import AICall


class Score(BaseModel):
    score: int


def payload(structured=None, **over) -> dict:
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "structured_output": {"score": 42} if structured is None else structured,
        "result": "",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 1417,
            "cache_read_input_tokens": 0,
            "output_tokens": 30,
        },
    }
    data.update(over)
    return data


class FakeRun:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result), stderr="")
        return result


def make(engine, tmp_path, run) -> ClaudeCodeAI:
    return ClaudeCodeAI("claude", engine, tmp_path, run=run)


def complete(ai: ClaudeCodeAI):
    return ai.complete(
        model=SCORING_MODEL,
        system="Svara kort.",
        prompt="Ge poäng 42.",
        schema=Score,
        purpose="score",
        effort="low",
    )


def test_builds_exact_command(engine, tmp_path):
    run = FakeRun(payload())
    complete(make(engine, tmp_path, run))
    args, kwargs = run.calls[0]
    assert args == [
        "claude", "-p",
        "--model", "claude-haiku-5-5",
        "--effort", "low",
        "--output-format", "json",
        "--json-schema", json.dumps(Score.model_json_schema()),
        "--system-prompt", "Svara kort.",
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]
    assert kwargs["input"] == "Ge poäng 42."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 180.0
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_returns_validated_structured_output(engine, tmp_path):
    result = complete(make(engine, tmp_path, FakeRun(payload())))
    assert result.value == Score(score=42)
    assert result.cost_usd == 0.0
    with session_scope(engine) as s:
        rows = [r.model_dump() for r in s.exec(select(AICall)).all()]
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1419
    assert rows[0]["output_tokens"] == 30
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["purpose"] == "score"
    assert rows[0]["model"] == SCORING_MODEL


def test_retries_once_on_missing_structured_output(engine, tmp_path):
    missing = payload()
    del missing["structured_output"]
    run = FakeRun(missing, payload())
    result = complete(make(engine, tmp_path, run))
    assert result.value.score == 42
    assert len(run.calls) == 2


def test_retries_once_on_invalid_structured_output(engine, tmp_path):
    run = FakeRun(payload({"score": "inte ett tal"}), payload())
    assert complete(make(engine, tmp_path, run)).value.score == 42
    assert len(run.calls) == 2


def test_raises_after_two_invalid(engine, tmp_path):
    run = FakeRun(payload({"nope": 1}), payload({"nope": 2}))
    with pytest.raises(AIError):
        complete(make(engine, tmp_path, run))
    assert len(run.calls) == 2


def test_nonzero_exit_raises(engine, tmp_path):
    run = FakeRun(subprocess.CompletedProcess([], 1, stdout="", stderr="not logged in"))
    with pytest.raises(AIError, match="not logged in"):
        complete(make(engine, tmp_path, run))
    assert len(run.calls) == 1


def test_non_json_stdout_raises(engine, tmp_path):
    run = FakeRun(subprocess.CompletedProcess([], 0, stdout="hej", stderr=""))
    with pytest.raises(AIError):
        complete(make(engine, tmp_path, run))


def test_is_error_raises_without_retry(engine, tmp_path):
    run = FakeRun(payload(is_error=True, result="något gick fel"))
    with pytest.raises(AIError, match="något gick fel"):
        complete(make(engine, tmp_path, run))
    assert len(run.calls) == 1


def test_usage_limit_raises_budget_exceeded(engine, tmp_path):
    run = FakeRun(payload(is_error=True, api_error_status=429))
    with pytest.raises(BudgetExceeded) as exc:
        complete(make(engine, tmp_path, run))
    assert str(exc.value) == "Claude-prenumerationens användningsgräns är nådd"
    assert len(run.calls) == 1


def test_usage_limit_with_nonzero_exit_raises_budget_exceeded(engine, tmp_path):
    out = json.dumps(payload(is_error=True, api_error_status=429))
    run = FakeRun(subprocess.CompletedProcess([], 1, stdout=out, stderr=""))
    with pytest.raises(BudgetExceeded):
        complete(make(engine, tmp_path, run))


def test_timeout_raises(engine, tmp_path):
    run = FakeRun(subprocess.TimeoutExpired(cmd="claude", timeout=180.0))
    with pytest.raises(AIError):
        complete(make(engine, tmp_path, run))
    assert len(run.calls) == 1


def test_missing_binary_raises(engine, tmp_path):
    run = FakeRun(FileNotFoundError("claude"))
    with pytest.raises(AIError, match="Claude Code"):
        complete(make(engine, tmp_path, run))


def test_claude_code_available():
    assert claude_code_available("python3") is True
    assert claude_code_available("finns-inte-det-här-programmet") is False


def test_make_ai_selects_backend(engine, config, paths, monkeypatch):
    monkeypatch.setattr("jobaplier.ai.anthropic_api_key", lambda: "sk-test")
    config.ai.backend = "claude_code"
    ai = make_ai(config, engine, paths)
    assert isinstance(ai, ClaudeCodeAI)
    config.ai.backend = "api"
    assert isinstance(make_ai(config, engine, paths), AnthropicAI)


def test_make_ai_api_without_key_raises(engine, config, paths, monkeypatch):
    monkeypatch.setattr("jobaplier.ai.anthropic_api_key", lambda: None)
    config.ai.backend = "api"
    with pytest.raises(AIError, match="ANTHROPIC_API_KEY"):
        make_ai(config, engine, paths)
