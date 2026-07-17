from dataclasses import dataclass
from typing import Literal

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import get_settings

USER_AGENT = "agentic-web-extraction/0.1 (+https://github.com/)"


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    raw_bytes: bytes
    text: str
    kind: Literal["html", "pdf", "skipped", "error"]


_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        # Plain httpx client, no HTTP-response cache of any kind. Fetching is cheap
        # relative to the LLM stages, and the frontier's visited set already stops a
        # URL from being fetched twice in one crawl, so an HTTP cache saved too
        # little to justify the memory/disk it took. The expensive work is memoized
        # by the content-addressed LLM cache instead (see cache.py / extractor.py).
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _classify(content_type: str) -> Literal["html", "pdf", "skipped"]:
    ct = content_type.lower()
    if "html" in ct or "xhtml" in ct:
        return "html"
    if "pdf" in ct:
        return "pdf"
    return "skipped"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _send(url: str) -> httpx.Response:
    response = get_client().get(url)
    if response.status_code >= 500:
        response.raise_for_status()
    return response


def fetch(url: str) -> FetchedPage:
    settings = get_settings()
    try:
        response = _send(url)
    except Exception as exc:
        # Degrade ANY fetch-level failure to a uniform kind="error" page instead
        # of throwing, so a single bad URL is skipped like any other error page
        # and never aborts the traversal. httpx errors (timeouts, 5xx, transport)
        # are the common case, but a malformed response can raise other types --
        # e.g. a UnicodeEncodeError when a redirect target or response header
        # carries a non-ASCII character (an emoji in a Location/Link header).
        # Bare `Exception` (not BaseException) still lets KeyboardInterrupt /
        # SystemExit propagate.
        return FetchedPage(
            url=url,
            status=0,
            content_type="",
            raw_bytes=b"",
            text=f"fetch error: {exc!r}",
            kind="error",
        )

    content_type = response.headers.get("content-type", "")
    kind = _classify(content_type)
    if kind == "skipped":
        return FetchedPage(
            url=str(response.url),
            status=response.status_code,
            content_type=content_type,
            raw_bytes=b"",
            text="",
            kind="skipped",
        )
    if kind == "pdf" and not settings.follow_pdf:
        return FetchedPage(
            url=str(response.url),
            status=response.status_code,
            content_type=content_type,
            raw_bytes=b"",
            text="",
            kind="skipped",
        )

    raw = response.content
    text = "" if kind == "pdf" else response.text
    return FetchedPage(
        url=str(response.url),
        status=response.status_code,
        content_type=content_type,
        raw_bytes=raw,
        text=text,
        kind=kind,
    )
