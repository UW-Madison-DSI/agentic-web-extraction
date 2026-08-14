"""Recover pages the origin refuses to serve, via third-party readers/archives.

Sites behind an edge CDN routinely answer a non-browser client with an HTTP
error and an HTML body -- an "Access Denied" interstitial, a themed 404. Because
[fetch.py](fetch.py) classifies on Content-Type alone, such a body would sail
through as ``kind="html"`` and be screened, summarized and extracted as if it
were the page. The status guard in ``fetch`` stops that; this module is what
turns the resulting hole back into content.

Two retrieval routes, tried in the order named by ``AWE_FETCH_FALLBACKS``:

``jina``
    ``r.jina.ai`` renders the URL server-side and returns it. Live content, and
    it reads PDFs, so a blocked document is recovered as text. Requests the full
    DOM (``X-Return-Format: html``) by default so everything downstream --
    markitdown normalization, link extraction, the frontier -- behaves exactly as
    on a direct fetch. Its readability modes return markdown instead; see
    ``markdown_to_html`` for how that is adapted without losing content.

``wayback``
    The Internet Archive's newest successful capture, served with the ``id_``
    modifier so the bytes come back unrewritten. Not live -- bound the staleness
    with ``AWE_WAYBACK_MAX_AGE_DAYS`` when currency matters.

Both are opinionated only about *retrieval*. Content selection, normalization,
and link policy stay where they already live, and nothing here knows about any
particular site -- the chain is driven entirely by the response status.

Recovered content is always returned under the **caller's** URL, never the
proxy/archive address: ``page.url`` becomes ``result.path``, the ``--- SOURCE:``
markers the extraction prompt carries, and whatever citations a caller builds
from them. ``FetchedPage.via`` records which route supplied it (and, for the
archive, the capture timestamp), and the extractor surfaces the map as
``ExtractionResult.fallbacks_used``.

Note both routes disclose the URL being crawled to a third party. Set
``AWE_FETCH_FALLBACKS=`` to disable recovery entirely and keep only the guard.
"""

import html as html_module
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import logsink
from .config import get_settings

JINA_ENDPOINT = "https://r.jina.ai/"
WAYBACK_CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
WAYBACK_SNAPSHOT_TEMPLATE = "https://web.archive.org/web/{timestamp}id_/{url}"

# Identifies the library to the recovery services, which are separate hosts under
# their own rate policies -- not the origin the crawl's own User-Agent addresses.
# Fallback only: ``configure`` replaces it with the deployment's attributable
# string, so recovery requests are as traceable to their operator as origin ones.
FALLBACK_USER_AGENT = "agentic-web-extraction/0.1 (fallback reader)"

# Absolute markdown links in a reader's output. Relative targets are deliberately
# not matched: the readers emit absolute URLs, so a relative-looking match is far
# likelier to be prose containing brackets than a real link.
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)")

# Worth waiting out rather than giving up on: 429 when a free-tier reader
# throttles us (a wave of blocked pages gets there quickly) and the 5xx family
# when a renderer or the archive's replay tier briefly falls over -- a CDX 504
# was observed with the immediately following call succeeding.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Recovered:
    """Bytes obtained for a URL the origin refused, plus how they were obtained.

    Deliberately not a ``FetchedPage``: this module stays free of any import from
    [fetch.py](fetch.py) (which imports *it*), and classification/PDF policy
    belong to the fetch path that already implements them.
    """

    raw_bytes: bytes
    text: str
    content_type: str
    via: str


_client_lock = threading.Lock()
_client: httpx.Client | None = None
_user_agent = FALLBACK_USER_AGENT


def configure(*, user_agent: str = "") -> None:
    """Set the User-Agent recovery requests send (empty restores the default).

    Mirrors :func:`fetch.configure` and is called from the same place with the
    same string: the reader/archive hosts should see who is asking just as the
    origin does. Which *route* served a page is recorded in ``FetchedPage.via``,
    so nothing is lost by dropping the old route-specific marker.
    """
    global _user_agent
    with _client_lock:
        _user_agent = user_agent or FALLBACK_USER_AGENT
        if _client is not None:
            _client.headers["User-Agent"] = _user_agent


def get_client() -> httpx.Client:
    """Shared client for recovery requests, separate from the crawl's own.

    Its timeouts are far more generous: these services render pages (and OCR
    PDFs) or replay from cold storage before answering, and this path only runs
    for pages that are otherwise lost entirely.
    """
    global _client
    with _client_lock:
        if _client is None:
            headers = {"User-Agent": _user_agent}
            settings = get_settings()
            if settings.jina_api_key is not None:
                key = settings.jina_api_key.get_secret_value().strip()
                if key:
                    headers["Authorization"] = f"Bearer {key}"
            _client = httpx.Client(
                headers=headers,
                follow_redirects=True,
                timeout=httpx.Timeout(120.0, connect=15.0),
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


@retry(
    stop=stop_after_attempt(3),
    # Longer and gentler than the origin fetch's retry: backing off politely
    # matters more than latency on a page that is already lost.
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _get(
    url: str, *, headers: dict[str, str] | None = None, **kwargs
) -> httpx.Response:
    response = get_client().get(url, headers=headers, **kwargs)
    if response.status_code in _TRANSIENT_STATUS:
        response.raise_for_status()
    return response


def markdown_to_html(markdown: str) -> str:
    """Wrap reader markdown so the HTML-shaped pipeline handles it losslessly.

    Handing markdown to ``to_markdown`` as-is goes wrong twice. Angle-bracketed
    text is parsed as tags and *silently dropped* -- "a <threshold> of 5" becomes
    "a  of 5", data loss no reader of the output would catch. And
    ``extract_links`` finds no ``<a>`` elements, so a recovered page contributes
    nothing to the frontier and the crawl dead-ends there.

    Escaping fixes the first (markitdown unescapes on the way back out, so the
    text survives verbatim); promoting markdown links to real anchors fixes the
    second and round-trips to the identical ``[text](url)`` form. Blank lines
    between blocks are collapsed by the HTML parse -- headings, list items and
    paragraphs each keep their own line, so structure survives, only spacing
    does not.
    """
    escaped = html_module.escape(markdown, quote=False)

    def anchor(match: re.Match[str]) -> str:
        # Both groups come out of the *already escaped* text, so `&`, `<` and `>`
        # are entities by now. Escaping the href a second time would turn a query
        # string's `&amp;` into `&amp;amp;`, which the HTML parser hands back as a
        # literal `&amp;` -- a corrupted URL in both the frontier and the markdown
        # the extraction reads. Only the attribute delimiter still needs escaping.
        text, url = match.group(1), match.group(2).replace('"', "&quot;")
        return f'<a href="{url}">{text}</a>'

    return _MARKDOWN_LINK.sub(anchor, escaped)


def _jina_get(url: str, fmt: str) -> str | None:
    """One reader call; the body, or None if it wasn't usable."""
    headers = {"X-Return-Format": fmt} if fmt else {}
    try:
        response = _get(f"{JINA_ENDPOINT}{url}", headers=headers)
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort
        logsink.emit(f"    [fallback:jina] failed for {url}: {exc!r}")
        return None
    if response.status_code != 200:
        logsink.emit(f"    [fallback:jina] returned {response.status_code} for {url}")
        return None
    if not response.text.strip():
        logsink.emit(f"    [fallback:jina] returned an empty body for {url}")
        return None
    return response.text


def _has_dom(body: str) -> bool:
    """Whether an ``X-Return-Format: html`` body actually carries markup.

    A PDF (or anything else with no DOM to serialize) comes back from html mode
    as a stub whose entire content is the literal word ``undefined`` -- 162 bytes
    where the 32-page document should be. Nothing rejects it downstream: it is a
    200, it is text, and it normalizes to a short line of prose, so it lands in
    the extraction as if the document simply had nothing to say. Checking for any
    tag at all catches that without depending on the reader's exact wording.
    """
    return "<" in body[:2000]


def _via_jina(url: str) -> Recovered | None:
    """Read ``url`` through the Jina reader.

    In html mode a target with no DOM (a PDF, an Office document) yields a stub,
    so that case falls back to the reader's default extraction -- which does read
    PDFs -- rather than passing the stub off as the page.
    """
    fmt = get_settings().jina_return_format.strip()
    body = _jina_get(url, fmt)
    if body is None:
        return None

    used = fmt
    if fmt.lower() == "html" and not _has_dom(body):
        logsink.emit(
            f"    [fallback:jina] html mode returned no markup for {url} "
            f"({len(body)}B) — retrying with the default reader"
        )
        retried = _jina_get(url, "")
        if retried is None:
            return None
        body, used = retried, ""

    # A real DOM is what the normal path already handles; every other mode
    # returns markdown, which needs the wrapper above.
    body = body if used.lower() == "html" else markdown_to_html(body)
    logsink.emit(
        f"    [fallback:jina] recovered {url} ({len(body)}B, format={used or 'default'})"
    )
    return Recovered(
        raw_bytes=body.encode("utf-8"),
        text=body,
        content_type="text/html; charset=utf-8",
        via="jina",
    )


def _newest_capture(url: str) -> str | None:
    """Timestamp of the archive's most recent successful capture of ``url``.

    ``fastLatest`` lets the CDX index answer from the tail of the block instead
    of scanning every capture; ``statuscode:200`` skips captures where the
    archive's own crawler was served the very block page we are working around.
    """
    try:
        response = _get(
            WAYBACK_CDX_ENDPOINT,
            params={
                "url": url,
                "output": "json",
                "limit": "-1",
                "filter": "statuscode:200",
                "fastLatest": "true",
            },
        )
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort
        logsink.emit(f"    [fallback:wayback] CDX lookup failed for {url}: {exc!r}")
        return None
    if response.status_code != 200:
        logsink.emit(
            f"    [fallback:wayback] CDX returned {response.status_code} for {url}"
        )
        return None
    try:
        rows = json.loads(response.text)
    except json.JSONDecodeError:
        return None
    # Row 0 is the column header; an empty result set is the header alone, or
    # nothing at all.
    if len(rows) < 2:
        logsink.emit(f"    [fallback:wayback] no capture on record for {url}")
        return None
    timestamp = rows[-1][1]
    return timestamp if isinstance(timestamp, str) else None


def _too_old(timestamp: str) -> bool:
    """True when a capture predates ``AWE_WAYBACK_MAX_AGE_DAYS`` (0 = no limit)."""
    limit = get_settings().wayback_max_age_days
    if limit <= 0:
        return False
    try:
        # Captures are stamped in UTC; comparing against a naive local `now()`
        # would misjudge the age by the machine's offset.
        captured = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return False  # unparseable: let it through rather than lose the page
    return datetime.now(UTC) - captured > timedelta(days=limit)


def _via_wayback(url: str) -> Recovered | None:
    """Read ``url`` from the Internet Archive's newest acceptable capture."""
    timestamp = _newest_capture(url)
    if timestamp is None:
        return None
    if _too_old(timestamp):
        logsink.emit(
            f"    [fallback:wayback] newest capture of {url} is {timestamp[:8]}, older "
            f"than {get_settings().wayback_max_age_days}d — refusing stale content"
        )
        return None

    try:
        response = _get(WAYBACK_SNAPSHOT_TEMPLATE.format(timestamp=timestamp, url=url))
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort
        logsink.emit(f"    [fallback:wayback] snapshot fetch failed for {url}: {exc!r}")
        return None
    if response.status_code != 200:
        logsink.emit(
            f"    [fallback:wayback] capture {timestamp} returned "
            f"{response.status_code} for {url}"
        )
        return None
    if not response.content:
        return None

    logsink.emit(
        f"    [fallback:wayback] recovered {url} from capture {timestamp} "
        f"({len(response.content)}B)"
    )
    return Recovered(
        raw_bytes=response.content,
        # Mirrors the fetch contract: `text` carries the decoded body that
        # `extract_links` parses, and stays empty for PDFs.
        text=""
        if "pdf" in response.headers.get("content-type", "").lower()
        else response.text,
        content_type=response.headers.get("content-type", ""),
        via=f"wayback:{timestamp}",
    )


_ROUTES: dict[str, Callable[[str], Recovered | None]] = {
    "jina": _via_jina,
    "wayback": _via_wayback,
}


def configured_routes() -> list[str]:
    """Route names from ``AWE_FETCH_FALLBACKS``, in order, unknown ones dropped."""
    names = [
        name.strip().lower()
        for name in get_settings().fetch_fallbacks.split(",")
        if name.strip()
    ]
    routes = []
    for name in names:
        if name in _ROUTES:
            routes.append(name)
        else:
            logsink.emit(f"    [fallback] ignoring unknown route {name!r}")
    return routes


def recover(url: str) -> Recovered | None:
    """Try each configured route in order; the first that yields content wins.

    Returns ``None`` when recovery is disabled, every route declines, or the URL
    simply isn't available anywhere -- the caller then degrades the page to
    ``kind="error"``, which the traversal skips at no LLM cost.
    """
    for name in configured_routes():
        recovered = _ROUTES[name](url)
        if recovered is not None:
            return recovered
    return None
