"""Recover pages the default transport could not obtain.

Sites behind an edge CDN routinely answer a non-browser client with an HTTP
error and an HTML body -- an "Access Denied" interstitial, a themed 404. Because
[fetch.py](fetch.py) classifies on Content-Type alone, such a body would sail
through as ``kind="html"`` and be screened, summarized and extracted as if it
were the page. The status guard in ``fetch`` stops that; this module is what
turns the resulting hole back into content.

An explicit refusal is only the polite half of that behavior. The other half is
a bot manager that simply stops answering -- no status, no body, just a read
timeout -- and a page lost that way is lost exactly as completely. So the chain
is driven by **failure to obtain content**, whatever shape the failure took:
``fetch`` calls in from its status guard *and* from its transport-error handler.

Three retrieval routes, tried in the order named by ``AWE_FETCH_FALLBACKS``:

``impersonate``
    Re-request the origin directly through curl_cffi, whose libcurl produces a
    browser's TLS/HTTP fingerprint. For origins that refuse on the *shape* of
    the handshake rather than on identity, this is the whole fix -- and unlike
    the two below it discloses nothing to a third party, needs no rate-limited
    free tier, and returns live content. Off unless ``AWE_IMPERSONATE`` names a
    target; see there and at ``AWE_IMPERSONATE_BROWSER_UA`` for what escalating
    costs. Ordering it first is strictly better than not, when it is enabled.

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

A route only wins if what it returned is plausibly the page. A 200 carrying a
client-rendered shell -- a few hundred bytes of empty containers, with the content
arriving later by script -- used to end the chain, because "a body arrived" was read
as "the page was obtained": one homepage came back as 554 bytes through
``impersonate`` (raw HTML, no JS) where ``jina`` rendered the same URL to 147KB. So
a body under ``AWE_MIN_RECOVERED_TEXT_CHARS`` of visible text counts as a decline
and the next route is tried. If none clears it, the fullest body obtained is
returned anyway -- the threshold reorders the chain, it never loses a page.

All three are opinionated only about *retrieval*. Content selection,
normalization, and link policy stay where they already live, and nothing here
knows about any particular site: the chain is driven by whether a body was
obtained, never by which host was asked. In particular a recovered body is still
adjudicated by the crawl boundary and by robots.txt exactly as a direct fetch is
-- recovery is not a way around either.

Recovered content is always returned under the **caller's** URL, never the
proxy/archive address: ``page.url`` becomes ``result.path``, the ``--- SOURCE:``
markers the extraction prompt carries, and whatever citations a caller builds
from them. ``FetchedPage.via`` records which route supplied it (and, for the
archive, the capture timestamp), and the extractor surfaces the map as
``ExtractionResult.fallbacks_used``.

Note ``jina`` and ``wayback`` disclose the URL being crawled to a third party;
``impersonate`` talks to the origin only. Set ``AWE_FETCH_FALLBACKS=`` to disable
recovery entirely and keep only the guard.
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
from .config import Settings, get_settings
from .frontier import domain_of, split_domains

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


def _jina_get(url: str, fmt: str, user_agent: str = "") -> str | None:
    """One reader call; the body, or None if it wasn't usable."""
    headers = {"X-Return-Format": fmt} if fmt else {}
    if user_agent:
        headers["User-Agent"] = user_agent
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


def _via_jina(url: str, user_agent: str = "") -> Recovered | None:
    """Read ``url`` through the Jina reader.

    In html mode a target with no DOM (a PDF, an Office document) yields a stub,
    so that case falls back to the reader's default extraction -- which does read
    PDFs -- rather than passing the stub off as the page.
    """
    fmt = get_settings().jina_return_format.strip()
    body = _jina_get(url, fmt, user_agent)
    if body is None:
        return None

    used = fmt
    if fmt.lower() == "html" and not _has_dom(body):
        logsink.emit(
            f"    [fallback:jina] html mode returned no markup for {url} "
            f"({len(body)}B) — retrying with the default reader"
        )
        retried = _jina_get(url, "", user_agent)
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


def _newest_capture(url: str, user_agent: str = "") -> str | None:
    """Timestamp of the archive's most recent successful capture of ``url``.

    ``fastLatest`` lets the CDX index answer from the tail of the block instead
    of scanning every capture; ``statuscode:200`` skips captures where the
    archive's own crawler was served the very block page we are working around.
    """
    try:
        response = _get(
            WAYBACK_CDX_ENDPOINT,
            headers={"User-Agent": user_agent} if user_agent else None,
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


def _via_wayback(url: str, user_agent: str = "") -> Recovered | None:
    """Read ``url`` from the Internet Archive's newest acceptable capture."""
    timestamp = _newest_capture(url, user_agent)
    if timestamp is None:
        return None
    if _too_old(timestamp):
        logsink.emit(
            f"    [fallback:wayback] newest capture of {url} is {timestamp[:8]}, older "
            f"than {get_settings().wayback_max_age_days}d — refusing stale content"
        )
        return None

    try:
        response = _get(
            WAYBACK_SNAPSHOT_TEMPLATE.format(timestamp=timestamp, url=url),
            headers={"User-Agent": user_agent} if user_agent else None,
        )
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


# --- impersonate ------------------------------------------------------------
#
# One session per (thread, target). A curl_cffi session wraps a libcurl handle,
# which is not thread-safe, and a wave runs up to `max_workers` recoveries at
# once -- so this deliberately does NOT follow the module-level `_client`
# singleton pattern above. `threading.local` gives each worker its own, and the
# connection reuse a session exists for still happens within a worker.
_sessions = threading.local()


def _new_session(target: str):
    """Build a curl_cffi session impersonating `target`.

    Imported here, not at module scope: curl_cffi is an optional dependency
    (``agentic-web-extraction[impersonate]``), and a deployment that never turns
    the route on must not need the wheel. The ImportError is caught by the route,
    which then declines like any other unavailable route.
    """
    # The optional "impersonate" extra: absent by design in a base install, which
    # is why the import is here and why the route catches ImportError.
    from curl_cffi import requests as cffi  # ty: ignore[unresolved-import]

    return cffi.Session(impersonate=target)


def _session_for_thread(target: str):
    sessions = getattr(_sessions, "by_target", None)
    if sessions is None:
        sessions = _sessions.by_target = {}
    session = sessions.get(target)
    if session is None:
        session = sessions[target] = _new_session(target)
    return session


def _impersonate_covers(url: str, settings: Settings) -> bool:
    """Whether ``AWE_IMPERSONATE_DOMAINS`` puts `url` in scope (empty = all).

    Keyed on the registrable domain via :func:`frontier.domain_of`, the same
    comparison the crawl boundary and the robots overrides use -- one host-matching
    rule for the whole library, so "example.org" covers "www.example.org" here too.
    """
    scope = split_domains(settings.impersonate_domains)
    if not scope:
        return True
    return domain_of(url) in scope


def _via_impersonate(url: str, user_agent: str = "") -> Recovered | None:
    """Re-request `url` with a browser TLS/HTTP fingerprint via curl_cffi.

    Unblocks origins that refuse on the shape of the handshake rather than on who
    is asking. Declines -- returning None, never raising -- when the route is
    unconfigured, curl_cffi isn't installed, the host is out of scope, or the
    origin refuses this client too.
    """
    settings = get_settings()
    target = settings.impersonate.strip()
    if not target:
        return None
    if not _impersonate_covers(url, settings):
        logsink.emit(
            f"    [fallback:impersonate] {url} is outside AWE_IMPERSONATE_DOMAINS"
        )
        return None
    try:
        session = _session_for_thread(target)
    except ImportError:
        logsink.emit(
            "    [fallback:impersonate] curl_cffi is not installed — install the "
            '"impersonate" extra to enable this route'
        )
        return None

    # Sending our own User-Agent beside a browser fingerprint is a mismatch some
    # bot managers reject outright; dropping it is a masquerade. Which of those a
    # deployment would rather do is the operator's call, not this module's, so the
    # default keeps the attributable string and the escalation is a second switch.
    headers = (
        None
        if settings.impersonate_browser_ua
        else {"User-Agent": user_agent or _user_agent}
    )
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=settings.impersonate_timeout,
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort
        logsink.emit(f"    [fallback:impersonate] failed for {url}: {exc!r}")
        return None
    if response.status_code != 200 or not response.content:
        logsink.emit(
            f"    [fallback:impersonate] returned {response.status_code} for {url}"
        )
        return None

    content_type = response.headers.get("content-type", "")
    logsink.emit(
        f"    [fallback:impersonate] recovered {url} as {target} "
        f"({len(response.content)}B)"
    )
    return Recovered(
        raw_bytes=response.content,
        # Mirrors the fetch contract, as the wayback route does: `text` carries the
        # decoded body `extract_links` parses, and stays empty for PDFs.
        text="" if "pdf" in content_type.lower() else response.text,
        content_type=content_type,
        via=f"impersonate:{target}",
    )


# --- is this body plausibly the page? ---------------------------------------

# `.*?` with DOTALL, closed on the same tag name: a page's inline scripts are where
# most of a shell's bytes live, so measuring length without dropping them measures
# the framework, not the content.
_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def visible_text(body: str) -> str:
    """The text a reader would see in ``body``, whitespace collapsed.

    Regex, deliberately, and not [normalize.py](normalize.py)'s markitdown pass:
    this runs on every recovered body and all it has to decide is a magnitude (554
    characters vs 147,000), which no amount of parser fidelity would change. It is
    also why this module still imports nothing from the fetch path.
    """
    text = _SCRIPT_OR_STYLE.sub(" ", body)
    text = _COMMENT.sub(" ", text)
    text = _TAG.sub(" ", text)
    return " ".join(html_module.unescape(text).split())


_ROUTES: dict[str, Callable[[str, str], Recovered | None]] = {
    "impersonate": _via_impersonate,
    "jina": _via_jina,
    "wayback": _via_wayback,
}


def impersonate(url: str, *, user_agent: str = "") -> Recovered | None:
    """The impersonate route alone, for a caller that needs the escalated
    transport but not the recovery chain.

    [robots.py](robots.py) is the one such caller: a deployment reading a site's
    *pages* through a browser fingerprint must read that site's *policy* the same
    way, or it fetches robots.txt over exactly the channel the origin blocks and
    then proceeds unrestricted every time. It must not, however, read a policy out
    of a third-party reader or a years-old archive capture, which is what going
    through :func:`recover` would allow.
    """
    return _via_impersonate(url, user_agent)


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


def recover(url: str, *, user_agent: str = "") -> Recovered | None:
    """Try each configured route in order; the first that yields the page wins.

    ``user_agent`` overrides the configured default for these requests only, so the
    reader/archive -- or, on the impersonate route, the origin itself -- sees the
    same operator the origin fetch named (see :func:`fetch.fetch`). Returns ``None``
    when recovery is disabled, every route declines, or the URL simply isn't
    available anywhere -- the caller then degrades the page to ``kind="error"``,
    which the traversal skips at no LLM cost.

    "Yields the page" means a body carrying at least
    ``AWE_MIN_RECOVERED_TEXT_CHARS`` of visible text. A route that answers with a
    client-rendered shell is treated as having declined, so a rendering route later
    in the chain still gets asked; the fullest shell is kept as a last resort, so
    the threshold can only change *which* body is returned, never whether one is.
    PDFs are exempt -- they carry their content as bytes, not as ``text``.
    """
    minimum = get_settings().min_recovered_text_chars
    best: Recovered | None = None
    best_chars = -1
    for name in configured_routes():
        recovered = _ROUTES[name](url, user_agent)
        if recovered is None:
            continue
        if minimum <= 0 or "pdf" in recovered.content_type.lower():
            return recovered
        chars = len(visible_text(recovered.text))
        if chars >= minimum:
            return recovered
        logsink.emit(
            f"    [fallback:{name}] {url} came back with {chars} characters of "
            f"text (minimum {minimum}) — reads like a client-rendered shell, "
            f"trying the next route"
        )
        if chars > best_chars:
            best, best_chars = recovered, chars
    if best is not None:
        logsink.emit(
            f"    [fallback] no route cleared the {minimum}-character minimum for "
            f"{url} — keeping the fullest body ({best.via}, {best_chars} characters)"
        )
    return best
