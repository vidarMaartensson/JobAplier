import itertools
import shutil

import pytest
from fakes import FakeAI
from fastapi.testclient import TestClient

from jobaplier.apply.base import SubmitResult
from jobaplier.apply.letter import LetterResult
from jobaplier.apply.service import ApplyService
from jobaplier.db import session_scope
from jobaplier.models import (
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Job,
    JobSource,
    JobStatus,
    Run,
)
from jobaplier.profile.cv import Profile
from jobaplier.web.app import create_app, local_origins
from jobaplier.web.state import AppState

PROFILE = Profile(
    summary="Backendutvecklare",
    skills=["Python"],
    years_experience=6,
    roles=["Backendutvecklare"],
    languages=["sv"],
    education=[],
)


class FakeApplier:
    def submit(self, job, draft) -> SubmitResult:
        return SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted)


class RunRecorder:
    """`run_now`-dubblett: räknar anrop och kastar `raises` om det är satt."""

    def __init__(self) -> None:
        self.calls = 0
        self.raises: Exception | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def ai() -> FakeAI:
    letter = LetterResult(language="sv", letter="Hej!", unsupported_claims=[])
    return FakeAI({Profile: lambda _prompt: PROFILE, LetterResult: [letter]})


@pytest.fixture
def run_now() -> RunRecorder:
    return RunRecorder()


@pytest.fixture
def state(engine, ai, paths, config, run_now, make_pdf) -> AppState:
    shutil.copy(make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."), paths.cv)
    service = ApplyService(
        engine,
        ai,
        paths,
        lambda: config,
        {ApplyMethod.email: FakeApplier(), ApplyMethod.form: FakeApplier()},
    )
    return AppState(
        engine=engine,
        paths=paths,
        config=lambda: config,
        service=service,
        run_now=run_now,
        setup_missing=lambda: [],
    )


@pytest.fixture
def client(state) -> TestClient:
    return TestClient(create_app(state, allowed_origins=local_origins() | {"http://testserver"}))


_keys = itertools.count()


@pytest.fixture
def add_job(engine):
    def _add(sources: tuple[str, ...] = ("platsbanken",), **overrides) -> int:
        n = next(_keys)
        values = {
            "dedupe_key": f"k{n}",
            "title": f"Utvecklare {n}",
            "company": "Exempel AB",
            "description": "Vi söker en utvecklare.",
            "url": f"https://example.se/jobb/{n}",
            "apply_method": ApplyMethod.email,
            "apply_email": "jobb@exempel.se",
            "status": JobStatus.scored,
            "score": 80,
            "score_reasons": "Bra match",
        }
        values.update(overrides)
        with session_scope(engine) as session:
            job = Job(**values)
            session.add(job)
            session.flush()
            for i, name in enumerate(sources):
                session.add(
                    JobSource(
                        job_id=job.id,
                        source=name,
                        external_id=f"{n}-{i}",
                        url=f"https://{name}.example/{n}",
                    )
                )
            return job.id

    return _add


@pytest.fixture
def add_run(engine):
    def _add(**values) -> None:
        with session_scope(engine) as session:
            session.add(Run(**values))

    return _add
