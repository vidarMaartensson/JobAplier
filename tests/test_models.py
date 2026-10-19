import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from jobaplier.db import session_scope
from jobaplier.models import (
    Application,
    Channel,
    Draft,
    Job,
    JobSource,
    JobStatus,
    Run,
)


def _job(key: str = "k1") -> Job:
    return Job(dedupe_key=key, title="Utvecklare", company="Exempel AB")


def test_job_defaults(engine):
    with session_scope(engine) as s:
        s.add(_job())
    with session_scope(engine) as s:
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.new
        assert job.id is not None
        assert job.created_at is not None and job.created_at.tzinfo is None


def test_unique_dedupe_key(engine):
    with session_scope(engine) as s:
        s.add(_job("same"))
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(_job("same"))


def test_unique_source_external_id(engine):
    with session_scope(engine) as s:
        a, b = _job("a"), _job("b")
        s.add_all([a, b])
        s.flush()
        s.add(JobSource(job_id=a.id, source="platsbanken", external_id="123"))
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            b = s.exec(select(Job).where(Job.dedupe_key == "b")).one()
            s.add(JobSource(job_id=b.id, source="platsbanken", external_id="123"))


def test_unique_application_per_channel(engine):
    with session_scope(engine) as s:
        job = _job()
        s.add(job)
        s.flush()
        draft = Draft(job_id=job.id, version=1, letter_text="Hej", language="sv")
        s.add(draft)
        s.flush()
        s.add(Application(job_id=job.id, draft_id=draft.id, channel=Channel.email))
        job_id, draft_id = job.id, draft.id
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(Application(job_id=job_id, draft_id=draft_id, channel=Channel.email))


def test_foreign_keys_enforced(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(JobSource(job_id=999, source="x", external_id="1"))


def test_json_columns_roundtrip(engine):
    with session_scope(engine) as s:
        job = _job()
        s.add(job)
        s.flush()
        s.add(
            JobSource(
                job_id=job.id,
                source="platsbanken",
                external_id="1",
                raw_json={"id": "1", "åäö": [1, 2]},
            )
        )
    with session_scope(engine) as s:
        src = s.exec(select(JobSource)).one()
        assert src.raw_json == {"id": "1", "åäö": [1, 2]}


def test_session_scope_rolls_back(engine):
    with pytest.raises(RuntimeError):
        with session_scope(engine) as s:
            s.add(_job())
            s.flush()
            raise RuntimeError("boom")
    with session_scope(engine) as s:
        assert s.exec(select(Job)).all() == []


def test_json_columns_persist_in_place_mutation(engine):
    with session_scope(engine) as s:
        s.add(Run())
    with session_scope(engine) as s:
        run = s.exec(select(Run)).one()
        run.stats_json["x"] = 1
        run.errors_json["platsbanken"] = "x"
    with session_scope(engine) as s:
        run = s.exec(select(Run)).one()
        assert run.stats_json == {"x": 1}
        assert run.errors_json == {"platsbanken": "x"}


def test_draft_and_raw_json_mutation_persist(engine):
    with session_scope(engine) as s:
        job = _job()
        s.add(job)
        s.flush()
        s.add(Draft(job_id=job.id, letter_text="Hej"))
        s.add(JobSource(job_id=job.id, source="a", external_id="1", raw_json={}))
    with session_scope(engine) as s:
        draft = s.exec(select(Draft)).one()
        src = s.exec(select(JobSource)).one()
        draft.unsupported_claims_json.append("påstående")
        src.raw_json["k"] = "v"
    with session_scope(engine) as s:
        assert s.exec(select(Draft)).one().unsupported_claims_json == ["påstående"]
        assert s.exec(select(JobSource)).one().raw_json == {"k": "v"}


def test_score_reasons_is_text(engine):
    with session_scope(engine) as s:
        s.add(Job(dedupe_key="s", title="t", company="c", score_reasons="Bra match."))
    with session_scope(engine) as s:
        assert s.exec(select(Job)).one().score_reasons == "Bra match."
