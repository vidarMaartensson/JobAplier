import httpx
import pytest

from jobaplier.sources.base import SourceError
from jobaplier.sources.http import get_with_retry, make_http_client


def _client(statuses: list[int | Exception]):
    calls: list[httpx.Request] = []
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={"ok": item == 200})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_retries_then_succeeds():
    client, calls = _client([503, 503, 200])
    sleeps: list[float] = []
    response = get_with_retry(client, "https://example.test/x", sleep=sleeps.append)
    assert response.status_code == 200
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retries_on_transport_error_and_429():
    client, calls = _client([httpx.ConnectTimeout("slow"), 429, 200])
    sleeps: list[float] = []
    response = get_with_retry(client, "https://example.test/x", sleep=sleeps.append)
    assert response.status_code == 200
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_raises_source_error_after_attempts():
    client, calls = _client([503, 503, 503])
    sleeps: list[float] = []
    with pytest.raises(SourceError) as exc:
        get_with_retry(client, "https://example.test/x", sleep=sleeps.append)
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # ingen väntan efter sista försöket
    assert "https://example.test/x" in str(exc.value)
    assert "503" in str(exc.value)


def test_no_retry_on_404():
    client, calls = _client([404])
    sleeps: list[float] = []
    with pytest.raises(SourceError) as exc:
        get_with_retry(client, "https://example.test/x", sleep=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []
    assert "404" in str(exc.value)


def test_make_http_client_defaults():
    with make_http_client() as client:
        assert client.timeout == httpx.Timeout(30.0)
        assert client.headers["User-Agent"] == "JobAplier/0.1 (personal use)"
