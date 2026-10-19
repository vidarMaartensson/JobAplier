from datetime import datetime
from pathlib import Path

import httpx
import pytest

from jobaplier.models import ApplyMethod
from jobaplier.sources.linkedin_links import (
    MAX_LINKS,
    LinkedInLinksSource,
    TooManyLinks,
    parse_linkedin_links,
)

JOB_PAGE = (Path(__file__).parent.parent / "fixtures" / "linkedin" / "job_page.html").read_text(
    encoding="utf-8"
)
SINCE = datetime(2026, 10, 1)


@pytest.mark.parametrize(
    ("text", "ids", "rejected"),
    [
        ("https://www.linkedin.com/jobs/view/4012345678/", ["4012345678"], []),
        ("https://www.linkedin.com/comm/jobs/view/4012345678?trackingId=x", ["4012345678"], []),
        (
            "https://se.linkedin.com/jobs/view/backend-developer-at-acme-4012345679",
            ["4012345679"],
            [],
        ),
        (
            "https://www.linkedin.com/jobs/view/4012345678/\n"
            "https://www.linkedin.com/jobs/view/4012345678/?refId=1\n"
            "https://www.linkedin.com/jobs/view/backend-dev-at-x-4012345678/",
            ["4012345678"],
            [],
        ),
        (
            "hej\nhttps://www.linkedin.com/jobs/view/1/\nhttps://example.com/jobs/view/1",
            ["1"],
            ["hej", "https://example.com/jobs/view/1"],
        ),
        (
            "\n  \nhttps://www.linkedin.com/jobs/view/7/  \r\n\r\n\thttps://www.linkedin.com/jobs/view/5/\n\n",
            ["7", "5"],
            [],
        ),
        ("", [], []),
    ],
)
def test_parse_links(text, ids, rejected):
    assert parse_linkedin_links(text) == (ids, rejected)


def test_parse_keeps_first_occurrence_order():
    text = "\n".join(f"https://www.linkedin.com/jobs/view/{n}/" for n in (3, 1, 3, 2, 1))
    assert parse_linkedin_links(text)[0] == ["3", "1", "2"]


def test_exactly_max_links_is_allowed():
    text = "\n".join(f"https://www.linkedin.com/jobs/view/{n}/" for n in range(1, MAX_LINKS + 1))
    assert len(parse_linkedin_links(text)[0]) == MAX_LINKS


def test_too_many_links():
    text = "\n".join(f"https://www.linkedin.com/jobs/view/{n}/" for n in range(1, MAX_LINKS + 2))
    with pytest.raises(TooManyLinks, match="Högst 20 länkar åt gången"):
        parse_linkedin_links(text)


def test_duplicates_do_not_count_towards_the_limit():
    text = "\n".join(
        f"https://www.linkedin.com/jobs/view/{n % MAX_LINKS}/" for n in range(MAX_LINKS * 2)
    )
    assert len(parse_linkedin_links(text)[0]) == MAX_LINKS


def test_source_fetches_pages():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/111"):
            return httpx.Response(200, text=JOB_PAGE)
        return httpx.Response(999)

    pauses: list[float] = []
    source = LinkedInLinksSource(
        ["111", "999"], httpx.Client(transport=httpx.MockTransport(handler)), pauses.append
    )

    complete, incomplete = source.fetch(SINCE)

    assert source.name == "linkedin_links"
    assert source.skip_filter is True
    assert requests == [
        "https://www.linkedin.com/jobs/view/111",
        "https://www.linkedin.com/jobs/view/999",
    ]
    assert pauses == [1.0]
    assert (complete.source, complete.external_id) == ("linkedin_links", "111")
    assert complete.title == "Backendutvecklare Python"
    assert complete.company == "Exempel Teknik AB"
    assert complete.location == "Stockholm, Stockholms län, Sverige"
    assert "Minst tre års erfarenhet av Python" in complete.description
    assert complete.url == "https://www.linkedin.com/jobs/view/111"
    assert complete.published_at is None
    assert complete.apply.method == ApplyMethod.unknown
    assert complete.incomplete is False
    assert (incomplete.source, incomplete.external_id) == ("linkedin_links", "999")
    assert incomplete.title == "LinkedIn-jobb 999"
    assert incomplete.company == "Okänt"
    assert incomplete.description == ""
    assert incomplete.url == "https://www.linkedin.com/jobs/view/999"
    assert incomplete.apply.method == ApplyMethod.unknown
    assert incomplete.incomplete is True


def test_source_without_ids_makes_no_requests_or_pauses():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("inga anrop väntades")

    source = LinkedInLinksSource(
        [], httpx.Client(transport=httpx.MockTransport(handler)), lambda s: pytest.fail("paus")
    )
    assert source.fetch(SINCE) == []
