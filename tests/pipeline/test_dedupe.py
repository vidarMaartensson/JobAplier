import pytest
from sqlmodel import Session, select

from jobaplier.models import ApplyMethod, Job, JobSource, JobStatus
from jobaplier.pipeline.dedupe import dedupe_key, normalize, refresh_incomplete, upsert_raw_jobs
from jobaplier.sources.base import ApplyInfo, RawJob


def raw(
    source="platsbanken",
    external_id="1",
    company="Exempel AB",
    title="Utvecklare – Backend",
    apply=None,
) -> RawJob:
    return RawJob(
        source=source,
        external_id=external_id,
        title=title,
        company=company,
        location="Stockholm",
        remote=None,
        description="Beskrivning",
        url=f"https://example.se/{source}/{external_id}",
        published_at=None,
        apply=apply or ApplyInfo(method=ApplyMethod.unknown),
    )


def test_key_variants():
    assert dedupe_key("Exempel AB", "Utvecklare – Backend") == dedupe_key(
        "exempel", "utvecklare backend"
    )
    assert dedupe_key("Åkeri AB", "X") != dedupe_key("Akeri", "X")


def test_normalize_keeps_swedish_letters_and_collapses_separators():
    assert normalize("Utvecklare (Backend)/Frontend - Åre") == (
        "utvecklare backend frontend åre"
    )


def test_company_form_tokens_removed_only_in_company_name():
    assert dedupe_key("Exempel Aktiebolag", "AB Manager") == "exempel|ab manager"
    assert dedupe_key("Acme Inc.", "X") == "acme|x"


def test_same_job_two_sources_same_batch(engine):
    mail = raw(
        "platsbanken",
        "pb-1",
        apply=ApplyInfo(
            method=ApplyMethod.email, email="jobb@exempel.se", reference="REF-7"
        ),
    )
    linkedin = raw("linkedin", "li-9", company="Exempel", title="utvecklare backend")
    with Session(engine) as session:
        new = upsert_raw_jobs(session, [mail, linkedin])
        session.commit()
        assert len(new) == 1
        jobs = session.exec(select(Job)).all()
        sources = session.exec(select(JobSource)).all()
        assert len(jobs) == 1
        assert len(sources) == 2
        assert {s.job_id for s in sources} == {jobs[0].id}
        assert jobs[0].apply_method == ApplyMethod.email
        assert jobs[0].apply_email == "jobb@exempel.se"
        assert jobs[0].apply_reference == "REF-7"
        assert jobs[0].status == JobStatus.new


def test_email_not_downgraded_when_unknown_arrives_first_in_batch(engine):
    linkedin = raw("linkedin", "li-9")
    mail = raw(
        "platsbanken",
        "pb-1",
        apply=ApplyInfo(method=ApplyMethod.email, email="a@b.se"),
    )
    with Session(engine) as session:
        new = upsert_raw_jobs(session, [linkedin, mail])
        assert len(new) == 1
        assert new[0].apply_method == ApplyMethod.email
        assert new[0].apply_email == "a@b.se"


def test_upgrades_apply_method_later_run(engine):
    with Session(engine) as session:
        first = upsert_raw_jobs(session, [raw("linkedin", "li-1")])
        session.commit()
        assert first[0].apply_method == ApplyMethod.unknown
        again = upsert_raw_jobs(
            session,
            [
                raw(
                    "careers",
                    "c-1",
                    apply=ApplyInfo(
                        method=ApplyMethod.form, form_url="https://x.se/apply"
                    ),
                )
            ],
        )
        session.commit()
        assert again == []
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.form
        assert job.apply_form_url == "https://x.se/apply"
        assert len(session.exec(select(JobSource)).all()) == 2


def test_email_beats_form_and_form_never_downgrades_email(engine):
    with Session(engine) as session:
        upsert_raw_jobs(
            session,
            [
                raw(
                    "careers",
                    "c-1",
                    apply=ApplyInfo(
                        method=ApplyMethod.form, form_url="https://x.se/apply"
                    ),
                )
            ],
        )
        upsert_raw_jobs(
            session,
            [
                raw(
                    "platsbanken",
                    "pb-1",
                    apply=ApplyInfo(
                        method=ApplyMethod.email, email="a@b.se", reference="R1"
                    ),
                )
            ],
        )
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email
        assert job.apply_email == "a@b.se"
        assert job.apply_reference == "R1"
        upsert_raw_jobs(
            session,
            [
                raw(
                    "other",
                    "o-1",
                    apply=ApplyInfo(
                        method=ApplyMethod.form, form_url="https://y.se/apply"
                    ),
                ),
                raw("linkedin", "li-1"),
            ],
        )
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email
        assert job.apply_email == "a@b.se"


def test_skips_known_source_id(engine):
    with Session(engine) as session:
        assert len(upsert_raw_jobs(session, [raw()])) == 1
        session.commit()
        assert upsert_raw_jobs(session, [raw()]) == []
        session.commit()
        assert len(session.exec(select(Job)).all()) == 1
        assert len(session.exec(select(JobSource)).all()) == 1


def test_new_job_stores_unknown_location_and_remote_as_none(engine):
    with Session(engine) as session:
        r = raw()
        r.location = None
        r.remote = None
        (job,) = upsert_raw_jobs(session, [r])
        session.commit()
        session.refresh(job)
        assert job.location is None
        assert job.remote is None
        src = session.exec(select(JobSource)).one()
        assert src.raw_json is None
        assert src.url == r.url


_FORM = ApplyInfo(method=ApplyMethod.form, form_url="https://x.se/apply")
_EMAIL = ApplyInfo(method=ApplyMethod.email, email="a@b.se", reference="R1")


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.drafting,
        JobStatus.draft_ready,
        JobStatus.approved,
        JobStatus.submitting,
        JobStatus.submitted,
        JobStatus.failed,
        JobStatus.link,
        JobStatus.dismissed,
    ],
)
def test_apply_method_frozen_once_job_left_scoring(engine, status):
    """Ett jobb i ansökningsflödet får aldrig byta kanal: användaren har sett
    "Öppna formulär" och ska inte i stället skicka ett riktigt mejl."""
    with Session(engine) as session:
        (job,) = upsert_raw_jobs(session, [raw("careers", "c-1", apply=_FORM)])
        job.status = status
        session.commit()
        upsert_raw_jobs(session, [raw("platsbanken", "pb-1", apply=_EMAIL)])
        session.commit()
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.form
        assert job.apply_email is None
        assert job.apply_reference is None
        assert job.apply_form_url == "https://x.se/apply"
        assert len(session.exec(select(JobSource)).all()) == 2


@pytest.mark.parametrize("status", [JobStatus.new, JobStatus.filtered, JobStatus.scored])
def test_apply_method_upgraded_before_apply_flow(engine, status):
    with Session(engine) as session:
        (job,) = upsert_raw_jobs(session, [raw("careers", "c-1", apply=_FORM)])
        job.status = status
        session.commit()
        upsert_raw_jobs(session, [raw("platsbanken", "pb-1", apply=_EMAIL)])
        session.commit()
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email
        assert job.apply_email == "a@b.se"


# --- ofullständiga jobb som hämtas igen ---------------------------------------------------


def incomplete_raw(external_id="9") -> RawJob:
    r = raw(source="linkedin_links", external_id=external_id, company="Okänt",
            title=f"LinkedIn-jobb {external_id}")
    r.description, r.location, r.incomplete = "", None, True
    return r


def test_refresh_incomplete_updates_facts_and_key(engine):
    with Session(engine) as session:
        (job,) = upsert_raw_jobs(session, [incomplete_raw()])
        refreshed = refresh_incomplete(session, [raw(source="linkedin_links", external_id="9")])
        assert refreshed == [job]
        assert (job.title, job.company, job.location, job.description) == (
            "Utvecklare – Backend", "Exempel AB", "Stockholm", "Beskrivning"
        )
        assert job.incomplete is False
        assert job.dedupe_key == dedupe_key("Exempel AB", "Utvecklare – Backend")
        # Hämtas sidan igen är jobbet inte längre ofullständigt: inget ändras.
        assert refresh_incomplete(session, [raw(source="linkedin_links", external_id="9")]) == []


def test_refresh_incomplete_keeps_key_when_another_job_has_it(engine):
    with Session(engine) as session:
        (other,) = upsert_raw_jobs(session, [raw()])  # samma jobb från Platsbanken
        (job,) = upsert_raw_jobs(session, [incomplete_raw()])
        old_key = job.dedupe_key
        refresh_incomplete(session, [raw(source="linkedin_links", external_id="9")])
        session.flush()
        assert job.title == "Utvecklare – Backend"
        assert job.dedupe_key == old_key
        assert other.dedupe_key == dedupe_key("Exempel AB", "Utvecklare – Backend")


def test_refresh_incomplete_ignores_pages_that_still_fail_and_unknown_ids(engine):
    with Session(engine) as session:
        (job,) = upsert_raw_jobs(session, [incomplete_raw()])
        assert refresh_incomplete(session, [incomplete_raw(), raw(external_id="9")]) == []
        assert job.incomplete is True
        assert job.title == "LinkedIn-jobb 9"
