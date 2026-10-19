"""HTTP-klient och GET med omförsök för jobbkällor."""

import time
from collections.abc import Callable

import httpx

from jobaplier.sources.base import SourceError

USER_AGENT = "JobAplier/0.1 (personal use)"
TIMEOUT_SECONDS = 30.0


def make_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET med exponentiell backoff vid nätverksfel, 429 och 5xx.

    Övriga 4xx ger `SourceError` direkt. Väntan före försök n+1 är
    `base_delay * 2**(n-1)`.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            problem = f"nätverksfel ({exc.__class__.__name__})"
        else:
            status = response.status_code
            if status < 400:
                return response
            if status != 429 and status < 500:
                raise SourceError(f"Hämtningen från {url} misslyckades: HTTP {status}.")
            problem = f"HTTP {status}"
        if attempt == attempts:
            raise SourceError(
                f"Hämtningen från {url} misslyckades efter {attempts} försök: {problem}."
            )
        sleep(base_delay * 2 ** (attempt - 1))
    raise SourceError(f"Hämtningen från {url} misslyckades: inga försök gjordes.")
