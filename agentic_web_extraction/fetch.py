import sqlite3
from dataclasses import dataclass
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
        storage = SyncSqliteStorage(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
        )
        _client = SyncCacheClient(
            storage=storage,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
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
    except httpx.HTTPError as exc:
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
