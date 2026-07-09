import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient
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


_client: SyncCacheClient | None = None


def get_client() -> SyncCacheClient:
    global _client
    if _client is None:
        # Persist the HTTP response cache on disk (across runs) when a path is
        # configured, so weekly re-crawls can issue conditional GETs; fall back
        # to an in-memory cache when the path is empty.
        cache_path = get_settings().http_cache
        if cache_path:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            storage = SyncSqliteStorage(database_path=cache_path)
        else:
            storage = SyncSqliteStorage(
                connection=sqlite3.connect(":memory:", check_same_thread=False),
            )
        _client = SyncCacheClient(
            storage=storage,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            # `Cache-Control: no-cache` forces hishel to revalidate every stored
            # response against the origin (conditional GET) instead of serving a
            # still-"fresh" cached body — so a page that changed since last week
            # is never hidden behind a long max-age. On a 304 the stored body is
            # returned (cheap); the normalized-content hash remains the source of
            # truth for whether the expensive LLM stages get skipped.
            headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
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
