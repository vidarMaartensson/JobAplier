"""Datamodell (SQLModel-tabeller) och enums. Alla tidsstämplar är naiv UTC."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import NaiveDatetime
from sqlalchemy import JSON, Column, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Nuvarande tid som naiv UTC (så lagras alla datetime-kolumner)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JobStatus(str, Enum):
    new = "new"
    filtered = "filtered"
    scored = "scored"
    dismissed = "dismissed"
    link = "link"
    drafting = "drafting"
    draft_ready = "draft_ready"
    approved = "approved"
    submitting = "submitting"
    submitted = "submitted"
    failed = "failed"


class ApplyMethod(str, Enum):
    email = "email"
    form = "form"
    unknown = "unknown"


class Channel(str, Enum):
    email = "email"
    form = "form"
    manual = "manual"


class ApplicationStatus(str, Enum):
    submitting = "submitting"
    submitted = "submitted"
    failed = "failed"


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    dedupe_key: str = Field(unique=True, index=True)
    title: str
    company: str
    location: str | None = None
    remote: bool | None = None
    description: str = ""
    url: str = ""
    published_at: NaiveDatetime | None = None
    apply_method: ApplyMethod = ApplyMethod.unknown
    apply_email: str | None = None
    apply_form_url: str | None = None
    apply_reference: str | None = None
    incomplete: bool = False
    status: JobStatus = Field(default=JobStatus.new, index=True)
    filter_reason: str | None = None
    score: int | None = None
    score_reasons: str | None = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class JobSource(SQLModel, table=True):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    source: str
    external_id: str
    url: str = ""
    raw_json: dict[str, Any] | None = Field(
        default=None, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    fetched_at: NaiveDatetime = Field(default_factory=utcnow)


class ProfileRecord(SQLModel, table=True):
    __tablename__ = "profiles"

    id: int | None = Field(default=None, primary_key=True)
    cv_hash: str = Field(index=True)
    cv_text: str
    profile_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class CVDocumentRecord(SQLModel, table=True):
    __tablename__ = "cv_documents"

    id: int | None = Field(default=None, primary_key=True)
    cv_hash: str = Field(index=True)
    document_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class CVDocumentWarnings(SQLModel, table=True):
    """Faktafält i ett CV-dokument som inte hittades ordagrant i CV-filens text."""

    __tablename__ = "cv_document_warnings"

    id: int | None = Field(default=None, primary_key=True)
    cv_document_id: int = Field(foreign_key="cv_documents.id", unique=True)
    warnings_json: list[str] = Field(
        default_factory=list, sa_column=Column(MutableList.as_mutable(JSON))
    )
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Draft(SQLModel, table=True):
    __tablename__ = "drafts"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    version: int = 1
    letter_text: str
    language: str = "sv"
    unsupported_claims_json: list[str] = Field(
        default_factory=list, sa_column=Column(MutableList.as_mutable(JSON))
    )
    instruction: str | None = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class TailoredCVRecord(SQLModel, table=True):
    """Claudes anpassning av CV:t för en utkastversion (`TailoredCV` som JSON).

    `cv_hash` är CV-filen som anpassningen gjordes mot; har filen bytts sedan dess
    används inte anpassningen."""

    __tablename__ = "tailored_cvs"

    id: int | None = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="drafts.id", unique=True)
    cv_hash: str
    tailored_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    warnings_json: list[str] = Field(
        default_factory=list, sa_column=Column(MutableList.as_mutable(JSON))
    )
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", "channel"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    draft_id: int | None = Field(default=None, foreign_key="drafts.id")
    channel: Channel
    status: ApplicationStatus = ApplicationStatus.submitting
    receipt: str | None = None
    error: str | None = None
    dry_run: bool = False
    submitted_at: NaiveDatetime | None = None


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: int | None = Field(default=None, primary_key=True)
    started_at: NaiveDatetime = Field(default_factory=utcnow)
    finished_at: NaiveDatetime | None = None
    trigger: str = "manual"  # "schedule" | "manual"
    stats_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    errors_json: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(MutableDict.as_mutable(JSON))
    )
    ai_cost_usd: float = 0.0


class AICall(SQLModel, table=True):
    __tablename__ = "ai_calls"

    id: int | None = Field(default=None, primary_key=True)
    at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
