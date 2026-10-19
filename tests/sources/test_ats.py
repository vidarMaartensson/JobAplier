import pytest

from jobaplier.sources.ats import detect_ats


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://exempel.teamtailor.com/jobs/123", "teamtailor"),
        ("https://foo.varbi.com/se/what:job/jobID:1", "varbi"),
        ("https://web103.reachmee.com/ext/I003/1234/job?site=5", "reachmee"),
        ("https://site1.reachmee.com/x", "reachmee"),
        ("https://boards.greenhouse.io/exempel/jobs/1", "greenhouse"),
        ("https://job-boards.greenhouse.io/exempel/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/exempel/abc", "lever"),
        ("https://exempel.wd3.myworkdayjobs.com/sv/Careers/job/1", "workday"),
        ("https://EXEMPEL.TeamTailor.com/jobs/1", "teamtailor"),
        ("https://exempel.se/karriar", None),
        ("https://teamtailor.com.evil.example/jobs/1", None),
        ("https://notteamtailor.com/jobs/1", None),
        ("https://exempel.se/?next=https://x.teamtailor.com", None),
        ("", None),
        ("inte en url", None),
        ("http://[::1", None),
    ],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected
