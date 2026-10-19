"""Tillåtna statusövergångar för jobb (spec §5.1). Enda vägen att ändra status."""

from jobaplier.models import Job, JobStatus, utcnow

S = JobStatus

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    S.new: frozenset({S.filtered, S.scored}),
    # Tillbaka till `new` när ett ofullständigt jobb hämtats helt och poängsätts igen.
    S.filtered: frozenset({S.new}),
    S.scored: frozenset({S.dismissed, S.link, S.drafting, S.new}),
    S.dismissed: frozenset(),
    S.link: frozenset({S.submitted}),
    S.drafting: frozenset({S.draft_ready, S.scored}),
    S.draft_ready: frozenset({S.approved, S.dismissed, S.submitted}),
    S.approved: frozenset({S.submitting}),
    S.submitting: frozenset({S.submitted, S.failed, S.draft_ready}),
    S.submitted: frozenset(),
    S.failed: frozenset({S.draft_ready, S.link}),
}


class InvalidTransition(Exception):
    def __init__(self, frm: JobStatus, to: JobStatus) -> None:
        super().__init__(f"Otillåten statusövergång: {frm.value} → {to.value}")
        self.frm = frm
        self.to = to


def transition(job: Job, to: JobStatus) -> None:
    if to not in ALLOWED_TRANSITIONS[job.status]:
        raise InvalidTransition(job.status, to)
    job.status = to
    job.updated_at = utcnow()
