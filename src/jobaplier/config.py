"""Konfiguration (data/config.yaml), sökvägar och miljövariabler."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Dashboarden lyssnar bara här (spec §4.5).
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8000

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ProfileInfo(BaseModel):
    name: str
    email: str
    phone: str = ""
    linkedin_url: str = ""


class Preferences(BaseModel):
    roles: list[str] = []
    keywords: list[str] = []
    exclude_keywords: list[str] = []
    exclude_companies: list[str] = []
    locations: list[str] = []
    seniority: list[str] = []
    remote_ok: bool = True


class CareerSite(BaseModel):
    type: Literal["greenhouse", "lever", "teamtailor"]
    company: str
    id: str


class Toggle(BaseModel):
    enabled: bool = True


class SourcesConfig(BaseModel):
    platsbanken: Toggle = Field(default_factory=Toggle)
    linkedin_alerts: Toggle = Field(default_factory=Toggle)
    careers: list[CareerSite] = []


class ScoringConfig(BaseModel):
    threshold: int = 60


class ApplyConfig(BaseModel):
    dry_run: bool = True
    max_emails_per_day: int = 10


class AIConfig(BaseModel):
    backend: Literal["claude_code", "api"] = "claude_code"
    claude_bin: str = "claude"


class Config(BaseModel):
    profile: ProfileInfo
    preferences: Preferences = Field(default_factory=Preferences)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    schedule: list[str] = Field(default_factory=lambda: ["08:00", "12:00", "17:00"])
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    ai_monthly_budget_usd: float = 10.0

    @field_validator("schedule")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        for t in value:
            if not _TIME_RE.match(t):
                raise ValueError(f"ogiltig tid '{t}', förväntat HH:MM (00:00-23:59)")
        return value


def load_config(path: Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def save_config(config: Config, path: Path) -> None:
    """Skriver config.yaml atomiskt (temporär fil i samma katalog + `os.replace`), så att en
    krasch mitt i skrivningen aldrig lämnar en trasig fil."""
    path = Path(path)
    text = yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    db: Path
    cv: Path
    config: Path
    gmail_credentials: Path
    gmail_token: Path
    browser: Path
    logs: Path
    letter_examples: Path
    eval_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> Paths:
        data_dir = Path(data_dir)
        return cls(
            data_dir=data_dir,
            db=data_dir / "jobaplier.db",
            cv=data_dir / "cv.pdf",
            config=data_dir / "config.yaml",
            gmail_credentials=data_dir / "credentials.json",
            gmail_token=data_dir / "gmail_token.json",
            browser=data_dir / "browser",
            logs=data_dir / "logs",
            letter_examples=data_dir / "letter_examples",
            eval_dir=data_dir / "eval",
        )


def get_paths() -> Paths:
    return Paths.from_data_dir(Path(os.environ.get("JOBAPLIER_DATA_DIR", "./data")))


def anthropic_api_key() -> str | None:
    load_dotenv()
    return os.environ.get("ANTHROPIC_API_KEY") or None
