import itertools
import shutil
from pathlib import Path

import pytest
from fakes import FakeAI
from fastapi.testclient import TestClient

from jobaplier.apply.base import SubmitResult
from jobaplier.apply.letter import LetterResult
from jobaplier.apply.tailor import TailoredCV
from jobaplier.apply.service import ApplyService
from jobaplier.config import Paths
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
from jobaplier.profile.cv import CVMissing, Profile
from jobaplier.profile.cv_document import CVDocument
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
        return SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting)


class RunRecorder:
    """`run_now`-dubblett: räknar anrop och kastar `raises` om det är satt."""

    def __init__(self) -> None:
        self.calls = 0
        self.raises: Exception | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.raises is not None:
            raise self.raises


class ImportRecorder:
    """`import_links`-dubblett: sparar id-listorna och kastar `raises` om det är satt."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.raises: Exception | None = None

    def __call__(self, ids: list[str]) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append(ids)


class CVPdfFake:
    """`cv_pdf`-dubblett. Som `build_cv_pdf`: utan skräddarsytt CV (standard) returneras
    `(paths.cv, False)`; med `tailored = True` skrivs `TAILORED` till målfilen. Saknas
    CV-filen kastas `CVMissing`, och `raises` kastas om det är satt."""

    TAILORED = b"%PDF-1.4 skraddarsytt CV"

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.tailored = False
        self.raises: Exception | None = None
        self.calls: list[tuple[int, Path]] = []

    def __call__(self, draft_id: int, out: Path) -> tuple[Path, bool]:
        self.calls.append((draft_id, out))
        if self.raises is not None:
            raise self.raises
        if not self.paths.cv.is_file():
            raise CVMissing("CV-filen saknas")
        if not self.tailored:
            return self.paths.cv, False
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.TAILORED)
        return out, True


class CVDocumentFake:
    """`cv_document`-dubblett: returnerar `document` (standard `None`, inget CV ännu)."""

    def __init__(self) -> None:
        self.document: CVDocument | None = None
        self.calls = 0

    def __call__(self) -> CVDocument | None:
        self.calls += 1
        return self.document


@pytest.fixture
def ai() -> FakeAI:
    letter = LetterResult(language="sv", letter="Hej!", unsupported_claims=[])
    tailored = TailoredCV(
        language="sv", headline="", summary="", experience=[], skills=[], education_order=[]
    )
    return FakeAI(
        {
            Profile: lambda _prompt: PROFILE,
            LetterResult: [letter],
            CVDocument: lambda _prompt: CVDocument(name="Jane Doe"),
            TailoredCV: lambda _prompt: tailored,
        }
    )


@pytest.fixture
def run_now() -> RunRecorder:
    return RunRecorder()


@pytest.fixture
def import_links() -> ImportRecorder:
    return ImportRecorder()


@pytest.fixture
def cv_pdf(paths) -> CVPdfFake:
    return CVPdfFake(paths)


@pytest.fixture
def cv_document() -> CVDocumentFake:
    return CVDocumentFake()


@pytest.fixture
def cv_warnings() -> list[str]:
    """Faktavarningarna som `state.cv_warnings` returnerar (lägg till i listan)."""
    return []


@pytest.fixture
def state(
    engine, ai, paths, config, run_now, import_links, make_pdf, cv_pdf, cv_document, cv_warnings
) -> AppState:
    shutil.copy(make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."), paths.cv)
    service = ApplyService(
        engine,
        ai,
        paths,
        lambda: config,
        {ApplyMethod.form: FakeApplier()},
    )
    return AppState(
        engine=engine,
        paths=paths,
        config=lambda: config,
        service=service,
        run_now=run_now,
        import_links=import_links,
        setup_missing=lambda: [],
        cv_pdf=cv_pdf,
        cv_document=cv_document,
        cv_warnings=lambda: list(cv_warnings),
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
