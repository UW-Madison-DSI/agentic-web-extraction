from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import fallback, logsink
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
    via: str = ""
    """Which recovery route supplied this body, empty when the origin did.
    ``"jina"`` or ``"wayback:<capture timestamp>"`` — see [fallback.py](fallback.py)."""


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


def content_type_of(response: httpx.Response) -> str:
    return response.headers.get("content-type", "")


def _recover(url: str, follow_pdf: bool) -> FetchedPage | None:
    """Ask [fallback.py](fallback.py) for a body the origin wouldn't serve.

    Returned under ``url`` itself -- never the reader/archive address -- so the
    crawl path, and any citation a caller derives from it, names the real page.
    """
    # The refusing origin told us nothing about the type, so honour follow_pdf on
    # the only evidence available before spending a recovery request. Recovered
    # bodies are classified normally below, which catches the rest.
    if not follow_pdf and urlsplit(url).path.lower().endswith(".pdf"):
        logsink.emit(f"    [fallback] skipping {url} — follow_pdf is off")
        return None

    recovered = fallback.recover(url)
    if recovered is None:
        return None
    kind = _classify(recovered.content_type)
    if kind == "skipped" or (kind == "pdf" and not follow_pdf):
        logsink.emit(
            f"    [fallback] discarding {recovered.via} body for {url} "
            f"(content-type {recovered.content_type!r})"
        )
        return None
    return FetchedPage(
        url=url,
        status=200,
        content_type=recovered.content_type,
        raw_bytes=recovered.raw_bytes,
        text=recovered.text,
        kind=kind,
        via=recovered.via,
    )


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

    resolved_url = str(response.url)
    # Status guard. An error page is routinely served as text/html -- an edge-CDN
    # "Access Denied" interstitial, a themed 404 -- and classifying on
    # Content-Type alone would hand that body to the screener and the extraction
    # call as though it were the page. Worse under seed_is_content, where
    # screening is skipped and the error text is guaranteed into the extraction.
    # So: anything outside 2xx is not content. Try to recover the page from
    # elsewhere, and failing that report kind="error", which the traversal
    # already skips at no LLM cost and no budget slot.
    if not 200 <= response.status_code < 300:
        logsink.emit(
            f"    [status] {response.status_code} on {resolved_url} — not content"
        )
        recovered = _recover(resolved_url, settings.follow_pdf)
        if recovered is not None:
            return recovered
        return FetchedPage(
            url=resolved_url,
            status=response.status_code,
            content_type=content_type_of(response),
            raw_bytes=b"",
            text=f"http error: status {response.status_code}",
            kind="error",
        )

    content_type = content_type_of(response)
    kind = _classify(content_type)
    if kind == "skipped":
        return FetchedPage(
            url=resolved_url,
            status=response.status_code,
            content_type=content_type,
            raw_bytes=b"",
            text="",
            kind="skipped",
        )
    if kind == "pdf" and not settings.follow_pdf:
        return FetchedPage(
            url=resolved_url,
            status=response.status_code,
            content_type=content_type,
            raw_bytes=b"",
            text="",
            kind="skipped",
        )

    raw = response.content
    text = "" if kind == "pdf" else response.text
    return FetchedPage(
        url=resolved_url,
        status=response.status_code,
        content_type=content_type,
        raw_bytes=raw,
        text=text,
        kind=kind,
    )
