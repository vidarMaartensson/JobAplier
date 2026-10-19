import pytest

from jobaplier.models import Job, JobStatus
from jobaplier.status import ALLOWED_TRANSITIONS, InvalidTransition, transition

S = JobStatus

EDGES = [
    (S.new, S.filtered),
    (S.new, S.scored),
    (S.scored, S.new),
    (S.filtered, S.new),
    (S.scored, S.dismissed),
    (S.scored, S.link),
    (S.scored, S.drafting),
    (S.drafting, S.draft_ready),
    (S.drafting, S.scored),
    (S.draft_ready, S.approved),
    (S.draft_ready, S.dismissed),
    (S.draft_ready, S.submitted),
    (S.approved, S.submitting),
    (S.submitting, S.submitted),
    (S.submitting, S.failed),
    (S.submitting, S.draft_ready),
    (S.failed, S.draft_ready),
    (S.failed, S.link),
    (S.link, S.submitted),
]


def _job(status: JobStatus) -> Job:
    return Job(dedupe_key="k", title="t", company="c", status=status)


@pytest.mark.parametrize(("frm", "to"), EDGES)
def test_allowed_transitions(frm, to):
    job = _job(frm)
    before = job.updated_at
    transition(job, to)
    assert job.status == to
    assert job.updated_at >= before


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.new, S.submitted),
        (S.scored, S.approved),
        (S.submitted, S.draft_ready),
        (S.dismissed, S.drafting),
        (S.filtered, S.scored),
    ],
)
def test_disallowed_transitions(frm, to):
    job = _job(frm)
    with pytest.raises(InvalidTransition):
        transition(job, to)
    assert job.status == frm


def test_transition_table_matches_edges():
    table = {(f, t) for f, tos in ALLOWED_TRANSITIONS.items() for t in tos}
    assert table == set(EDGES)
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)
