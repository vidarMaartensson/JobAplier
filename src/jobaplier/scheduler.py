"""Schemalagda sökkörningar (APScheduler i samma process som webbappen)."""

from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

DEFAULT_TZ = "Europe/Stockholm"
# En missad körning (datorn sov) görs när datorn vaknar, om det är högst sex timmar sedan.
MISFIRE_GRACE_SECONDS = 6 * 3600
JOB_PREFIX = "run-"


def _job_id(time: str) -> str:
    hh, mm = time.split(":")
    return f"{JOB_PREFIX}{hh}{mm}"


def reschedule(
    scheduler: BackgroundScheduler,
    run_job: Callable[[], None],
    times: list[str],
    tz: str = DEFAULT_TZ,
) -> None:
    """Ersätter alla körningsjobb med ett per tid i `times` (HH:MM). Andra jobb lämnas."""
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX):
            job.remove()
    for time in dict.fromkeys(times):  # dubbletter ger samma jobb-id
        hh, mm = time.split(":")
        scheduler.add_job(
            run_job,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
            id=_job_id(time),
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            max_instances=1,
            replace_existing=True,
        )


def build_scheduler(
    run_job: Callable[[], None], times: list[str], tz: str = DEFAULT_TZ
) -> BackgroundScheduler:
    """Schemaläggare (ej startad) med en `CronTrigger` per tid."""
    scheduler = BackgroundScheduler(timezone=tz)
    reschedule(scheduler, run_job, times, tz)
    return scheduler
