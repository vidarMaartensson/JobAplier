from jobaplier.scheduler import build_scheduler, reschedule


def _fields(job) -> dict[str, str]:
    return {field.name: str(field) for field in job.trigger.fields}


def test_scheduler_jobs_and_options():
    scheduler = build_scheduler(lambda: None, ["08:00", "12:00", "17:00"])
    jobs = sorted(scheduler.get_jobs(), key=lambda j: j.id)

    assert [j.id for j in jobs] == ["run-0800", "run-1200", "run-1700"]
    assert [_fields(j)["hour"] for j in jobs] == ["8", "12", "17"]
    assert all(_fields(j)["minute"] == "0" for j in jobs)
    for job in jobs:
        assert job.coalesce is True
        assert job.misfire_grace_time == 21600
        assert job.max_instances == 1
        assert str(job.trigger.timezone) == "Europe/Stockholm"


def test_scheduler_minutes_and_duplicate_times():
    scheduler = build_scheduler(lambda: None, ["07:45", "07:45"])
    jobs = scheduler.get_jobs()

    assert [j.id for j in jobs] == ["run-0745"]
    assert _fields(jobs[0])["hour"] == "7"
    assert _fields(jobs[0])["minute"] == "45"


def test_reschedule_replaces_run_jobs_and_keeps_others():
    calls: list[str] = []
    scheduler = build_scheduler(lambda: calls.append("run"), ["08:00", "12:00"])
    scheduler.add_job(lambda: None, "interval", hours=1, id="other")

    reschedule(scheduler, lambda: calls.append("run"), ["09:30"])

    assert sorted(j.id for j in scheduler.get_jobs()) == ["other", "run-0930"]
    job = scheduler.get_job("run-0930")
    assert _fields(job)["hour"] == "9"
    assert _fields(job)["minute"] == "30"
    assert job.coalesce is True and job.max_instances == 1


def test_reschedule_on_running_scheduler():
    scheduler = build_scheduler(lambda: None, ["08:00"])
    scheduler.start(paused=True)
    try:
        reschedule(scheduler, lambda: None, ["10:00", "18:15"])
        assert sorted(j.id for j in scheduler.get_jobs()) == ["run-1000", "run-1815"]
        assert all(j.next_run_time is not None for j in scheduler.get_jobs())
    finally:
        scheduler.shutdown(wait=False)
