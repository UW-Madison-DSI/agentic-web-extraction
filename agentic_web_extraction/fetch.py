import threading
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    wait_exponential,
)

from . import fallback, logsink
from .config import get_settings
from .frontier import domain_of

USER_AGENT = "agentic-web-extraction/0.1 (+https://github.com/)"
"""Fallback User-Agent: what the crawl sends when nothing configures one.

Generic on purpose -- it names the library, not the operator, so a site owner
who wants the traffic to stop has nobody to write to. Set ``AWE_USER_AGENT`` (or
pass ``Extractor(user_agent=...)``) to an attributable string instead."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    raw_bytes: bytes
    text: str
    kind: Literal["html", "pdf", "skipped", "error"]
    via: str = ""
    """Which recovery route supplied this body, empty when the default transport
    did. ``"jina"``, ``"wayback:<capture timestamp>"``, or
    ``"impersonate:<target>"`` (that last one is still the origin, just reached
    with a browser fingerprint) — see [fallback.py](fallback.py)."""


_client_lock = threading.Lock()
_client: httpx.Client | None = None
_user_agent = USER_AGENT


def configure(*, user_agent: str = "") -> None:
    """Set the User-Agent every crawl fetch sends (empty restores the default).

    Module-level, like :func:`logsink.configure`, and called from
    ``Extractor.__init__``: the client is a process-wide singleton, so the last
    configuration wins for every crawl in the process. That is fine for the
    intended use (one identifying string per deployment) but means two Extractors
    in one process cannot send different User-Agents.
    """
    global _user_agent
    with _client_lock:
        _user_agent = user_agent or USER_AGENT
        # The client may already exist (a previous crawl in this process built it),
        # so update it in place rather than leaking its connection pool.
        if _client is not None:
            _client.headers["User-Agent"] = _user_agent


def user_agent() -> str:
    """The User-Agent currently in effect for crawl fetches."""
    with _client_lock:
        return _user_agent


def get_client() -> httpx.Client:
    """Shared client for origin fetches.

    Lock-guarded: the traversal fetches a whole wave of pages concurrently, so an
    unguarded lazy init would let several worker threads each build a client and
    leak all but the last one's connection pool.
    """
    global _client
    with _client_lock:
        if _client is None:
            # Plain httpx client, no HTTP-response cache of any kind. Fetching is
            # cheap relative to the LLM stages, and the frontier's visited set
            # already stops a URL from being fetched twice in one crawl, so an HTTP
            # cache saved too little to justify the memory/disk it took. The
            # expensive work is memoized by the content-addressed LLM cache instead
            # (see cache.py / extractor.py).
            _client = httpx.Client(
                headers={"User-Agent": _user_agent},
                follow_redirects=True,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# --- the "this host does not answer us" memo --------------------------------
#
# An origin that tarpits non-browser clients refuses every URL the same way, and
# it refuses by going silent, so each page spends its whole attempt budget (two
# read timeouts, ~35s) before recovery is reached -- once per page, for as many
# pages as the crawl visits there. This remembers the refusal per registrable
# domain and skips the default transport once it has been proven, so a wave costs
# one recovery attempt per page instead of a timeout plus a recovery attempt.
#
# Kept here rather than in [fallback.py](fallback.py) on purpose: it is a fact
# about the default transport, and that module must stay ignorant of which host is
# being asked (see its own note). Lock-guarded like `_client` -- a wave of workers
# increments this concurrently.
_memo_lock = threading.Lock()
_no_response: dict[str, int] = {}
"""Registrable domain -> fetches there that produced no response at all."""
_answered: set[str] = set()
"""Registrable domains that have answered us at least once, ever.

The memo's whole premise is a host that never talks to us. A host that served six
pages and then read-timed-out on two big ones is slow, not silent -- and since
workers interleave, the count alone cannot tell those apart (two failures can land
after six successes and look consecutive). So a domain in here is never written
off, whatever its count reaches.
"""


def reset_transport_memo() -> None:
    """Forget every written-off host, and every host known to answer.

    Called at the start of each crawl (see ``Extractor.extract``): the memo is
    evidence about the network gathered *during* a crawl, and a new crawl -- which
    may run under a different User-Agent, or with impersonation newly enabled --
    is entitled to re-test hosts an earlier one wrote off. Also process-wide state,
    like the http clients, so tests reset it around each case.
    """
    with _memo_lock:
        _no_response.clear()
        _answered.clear()


def _memo_threshold() -> int:
    return get_settings().transport_memo_failures


def counts_as_silence(exc: BaseException) -> bool:
    """Whether `exc` means the origin never answered, as opposed to *this URL*
    being unfetchable.

    The fetch handler catches bare ``Exception`` on purpose -- a malformed
    ``Location`` header raises ``UnicodeEncodeError``, a bad href raises
    ``httpx.InvalidURL`` -- and none of that is evidence about the *host*. Writing a
    domain off over two dead links would route a healthy site through the recovery
    routes. Only a timeout or a network-level failure counts: nothing came back.
    """
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError)


def _failed_host(url: str, exc: BaseException) -> str:
    """The URL whose host actually failed, which is not always the one asked for.

    The client follows redirects inside a single call, so a rebrand or a vanity
    domain pointing at a tarpit fails on the *second* request -- and attributing
    that to the first hop writes off a host that answers redirects fine in one
    round-trip while the silent landing domain never accumulates a count. httpx
    carries the failing request on the exception; fall back to the requested URL
    when it doesn't (a non-httpx failure, or one raised before a request existed).
    """
    request = getattr(exc, "request", None)
    return str(getattr(request, "url", "") or url)


def _note_no_response(url: str) -> None:
    """Record that `url` produced no response, and log the moment its domain is
    written off (once -- crossing the threshold, not every fetch after it)."""
    threshold = _memo_threshold()
    if threshold <= 0:
        return
    domain = domain_of(url)
    if not domain:
        return
    with _memo_lock:
        count = _no_response[domain] = _no_response.get(domain, 0) + 1
        latched = count == threshold and domain not in _answered
    if latched:
        logsink.emit(
            f"    [transport-memo] {domain} has not answered {count} time(s) — "
            f"later fetches there go straight to recovery"
        )


def _note_response(*urls: str) -> None:
    """Record that these hosts answered, whatever they answered with.

    Any status counts, 403 and 503 included: this tracks *silence*, and a host that
    refuses out loud is a host that is talking to us -- one round-trip per page,
    which is nothing to skip. Both the requested and the resolved URL are passed on
    a redirect: each host in the chain answered its part.
    """
    domains = {domain_of(url) for url in urls} - {""}
    if not domains:
        return
    with _memo_lock:
        _answered.update(domains)
        for domain in domains:
            _no_response.pop(domain, None)


def _written_off(url: str) -> bool:
    """Whether the default transport has been proven hopeless for `url`'s host."""
    threshold = _memo_threshold()
    if threshold <= 0:
        return False
    domain = domain_of(url)
    if not domain:
        return False
    with _memo_lock:
        return domain not in _answered and _no_response.get(domain, 0) >= threshold


def _classify(content_type: str) -> Literal["html", "pdf", "skipped"]:
    ct = content_type.lower()
    if "html" in ct or "xhtml" in ct:
        return "html"
    if "pdf" in ct:
        return "pdf"
    return "skipped"


def _attempts_for(exc: BaseException | None) -> int:
    """How many attempts `_send` is allowed, given what the last one raised.

    ``AWE_FETCH_ATTEMPTS`` (default 3) governs the transient failures -- a 5xx, a
    refused or reset connection -- where trying again is what fixes it. A *read*
    timeout is capped at two attempts however high that is set: the failure it
    stands for in practice is an edge CDN tarpitting a non-browser client, which
    is a deterministic decision, so attempts two and three spend the full read
    timeout each (and hold a worker slot the whole wave is waiting on) to be
    refused identically. Recovery is what can actually turn that page back into
    content -- get there sooner.
    """
    limit = max(1, get_settings().fetch_attempts)
    return min(limit, 2) if isinstance(exc, httpx.ReadTimeout) else limit


def _stop_after_attempts(retry_state: RetryCallState) -> bool:
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    return retry_state.attempt_number >= _attempts_for(exc)


@retry(
    stop=_stop_after_attempts,
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _send(url: str, user_agent: str = "") -> httpx.Response:
    # A per-request User-Agent overrides the client default for this call only. The
    # client is a process-wide singleton, so a caller-supplied header is the only way
    # two Extractors in one process can each send their own identifying string.
    headers = {"User-Agent": user_agent} if user_agent else None
    response = get_client().get(url, headers=headers)
    if response.status_code >= 500:
        response.raise_for_status()
    return response


def content_type_of(response: httpx.Response) -> str:
    return response.headers.get("content-type", "")


def _recover(url: str, follow_pdf: bool, user_agent: str = "") -> FetchedPage | None:
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

    recovered = fallback.recover(url, user_agent=user_agent)
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


def fetch(url: str, *, user_agent: str = "") -> FetchedPage:
    """Fetch one URL. `user_agent` overrides the configured default for this call
    only -- pass the caller's own string so the header on the wire always names the
    Extractor that asked, whatever another Extractor configured meanwhile."""
    settings = get_settings()

    # The host has already been proven not to answer this transport (see the memo
    # above), so don't spend the attempt budget finding that out again -- go to the
    # routes that can still read it. Gated on a route being *named* at all, which
    # skips the whole detour when recovery is disabled; whether the named routes can
    # actually act here is only knowable by asking them, which is why declining
    # falls back to the origin below rather than reporting an error.
    if _written_off(url) and fallback.configured_routes():
        logsink.emit(
            f"    [transport-memo] skipping the default transport for {url} — "
            f"{domain_of(url)} has not answered"
        )
        recovered = _recover(url, settings.follow_pdf, user_agent)
        if recovered is not None:
            return recovered
        # No route could act -- either they all declined, or the names in
        # AWE_FETCH_FALLBACKS can't act *here* (impersonate declines outright when
        # AWE_IMPERSONATE is unset, when the host is outside
        # AWE_IMPERSONATE_DOMAINS, or when curl_cffi isn't installed, and
        # `configured_routes` can't see any of that). So fall through and ask the
        # origin after all. The memo is an optimization; it must never be the
        # reason a page is lost, and a fetch that then succeeds clears it, which is
        # also the only way back once a domain is written off.
        logsink.emit(
            f"    [transport-memo] no route could read {url} — asking the origin anyway"
        )

    try:
        response = _send(url, user_agent)
    except httpx.HTTPStatusError as exc:
        # `_send` raises this for a 5xx it gave up retrying. That is still a
        # *response* whose status is outside 2xx -- exactly the hole the status
        # guard below exists for -- so it goes through the same guard (and the
        # same recovery attempt) rather than short-circuiting to kind="error".
        # A 503 from an edge CDN refusing a non-browser client is the common case.
        response = exc.response
    except Exception as exc:
        # Degrade ANY fetch-level failure to a uniform kind="error" page instead
        # of throwing, so a single bad URL is skipped like any other error page
        # and never aborts the traversal. httpx errors (timeouts, 5xx, transport)
        # are the common case, but a malformed response can raise other types --
        # e.g. a UnicodeEncodeError when a redirect target or response header
        # carries a non-ASCII character (an emoji in a Location/Link header).
        # Bare `Exception` (not BaseException) still lets KeyboardInterrupt /
        # SystemExit propagate.
        #
        # First, though: recover, exactly as the status guard below does. There
        # was no response at all here -- a tarpitting edge CDN, a dropped or
        # reset connection, a malformed redirect header -- and an origin that
        # refuses by going silent has denied us the page just as completely as
        # one that answers 403 with an interstitial. Handling only the second is
        # backwards: silence is the *less* polite refusal and it was the one
        # route that skipped recovery entirely. Logged distinctly because the
        # diagnostics differ -- a status is evidence, silence isn't -- and named
        # after the host that failed, which on a redirect is not the one asked for.
        # Recovery, though, is asked for the URL the *caller* wanted: a recovered
        # body is always returned under that address.
        failed = _failed_host(url, exc)
        logsink.emit(f"    [transport] {type(exc).__name__} on {failed} — no response")
        # Before recovering: bank the proof, but only if it is proof about the
        # *host* rather than about this one URL (see `counts_as_silence`), and
        # against the host that actually failed rather than the first hop of a
        # redirect. It is what lets the next URL there skip the attempt budget.
        if counts_as_silence(exc):
            _note_no_response(failed)
        recovered = _recover(url, settings.follow_pdf, user_agent)
        if recovered is not None:
            return recovered
        return FetchedPage(
            url=url,
            status=0,
            content_type="",
            raw_bytes=b"",
            text=f"fetch error: {exc!r}",
            kind="error",
        )

    resolved_url = str(response.url)
    # A response arrived, whatever its status: these hosts talk to us, so any memo
    # standing against them is stale and they are never written off again. Both
    # paths above that reach here have a response (the 5xx one arrives as
    # `exc.response`), and on a redirect both ends of the chain answered.
    _note_response(url, resolved_url)

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
        recovered = _recover(resolved_url, settings.follow_pdf, user_agent)
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
