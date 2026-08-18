"""robots.txt policy for the crawl (opt-in, ``AWE_RESPECT_ROBOTS``).

One ``robots.txt`` per origin, fetched with the crawl's own client (so it carries
the same User-Agent the rules are then evaluated against) and parsed by the
stdlib :class:`urllib.robotparser.RobotFileParser`. Results are cached for the
life of the policy object, so an origin is asked once however many of its pages
the traversal visits.

Evaluated *before* the fetch, so a disallowed URL costs no request, no budget
slot and no LLM call. A URL that *redirects* is checked twice -- once as requested,
once where it landed. That second request is already spent (httpx follows redirects
inside a single call, and refusing to follow them would break the rebrand case the
crawl boundary depends on), but a body from a disallowed path is discarded unread
rather than screened and pooled into the extraction; a redirector on an allowed path
is otherwise a hole straight through this check, including across origins.
This is a politeness/permission control, not a security
boundary -- for that, see the hard crawl boundary in
[extractor.py](extractor.py) (``allowed_domains``), which decides which domains
may be queued at all. The two compose: the boundary says *where* the crawl may
go, robots says *what* it may read once there.

**Failures fail open.** If ``robots.txt`` cannot be obtained -- connection error,
timeout, 404, 401/403, 5xx -- the origin is treated as unrestricted rather than
fully disallowed. That is a deliberate choice, and the opposite of what RFC 9309
suggests for the 5xx/unreachable case: an origin that briefly 500s, or whose
robots.txt is behind the same edge rule that blocks the crawler itself, would
otherwise silently drop every page of an authorized crawl with a "disallowed"
line that looks like the site's own policy. A missing robots.txt has never meant
"stay out", and the honest signal here is the log line naming the failure.

**A 200 is not automatically a policy.** An origin fronted by a bot manager
routinely answers ``/robots.txt`` with 200 and an HTML sensor page. Handed to
:class:`RobotFileParser` that parses to *zero rules* -- read as blanket consent,
at precisely the sites likeliest to have meant the opposite, and silently. So a
body that isn't plausibly a policy (see :func:`looks_like_policy`) is treated as
*unavailable* rather than as permission. It still fails open, for the reason
above -- but knowingly, with a log line saying the policy could not be obtained,
which is the honest thing to hand an operator deciding whether to crawl a site
that won't tell them its rules.

**The policy is fetched over the same transport as the pages.** When
``AWE_IMPERSONATE`` covers a host, a robots.txt the default client cannot obtain
is retried through the escalated one (see [fallback.py](fallback.py)); otherwise
a deployment that reads a site's pages with a browser fingerprint would read its
policy over exactly the channel that site blocks, and proceed unrestricted every
time. Only that route -- never the reader/archive ones, which would answer with a
third party's copy of somebody's rules.

Domains in ``AWE_ROBOTS_OVERRIDES`` skip the check entirely -- for hosts whose
robots.txt blanket-disallows automated clients but whose content the operator is
authorized to read anyway.
"""

import threading
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import fallback as fallback_module
from . import fetch as fetch_module
from . import logsink
from .frontier import domain_of, normalize_domains

# robots.txt is a small static file and it gates the page fetch behind it, so a
# slow origin must not hold up a whole wave for the crawl client's full 30s.
ROBOTS_TIMEOUT = httpx.Timeout(10.0, connect=10.0)

# (status, content-type, body), or None when this transport has nothing to offer.
Fetched = tuple[int, str, str] | None
Fetcher = Callable[[str, str], Fetched]


def _http_get(url: str, user_agent: str = "") -> Fetched:
    """Fetch ``url`` with the crawl's own client; (status, content-type, body).

    Deliberately the same client as the pages, and deliberately sent under the same
    per-request User-Agent the rules are then matched against -- asking as one agent
    and obeying the rules for another is the kind of divergence nobody notices until
    it matters. Identical connection pool, and no retry wrapper: a robots.txt that
    fails once fails open, and re-asking three times only lengthens the stall.
    """
    response = fetch_module.get_client().get(
        url,
        timeout=ROBOTS_TIMEOUT,
        headers={"User-Agent": user_agent} if user_agent else None,
    )
    return (
        response.status_code,
        response.headers.get("content-type", ""),
        response.text,
    )


def _impersonated_get(url: str, user_agent: str = "") -> Fetched:
    """Fetch ``url`` with the escalated transport, or None if it isn't configured
    for this host (which is the default, so this normally does nothing)."""
    recovered = fallback_module.impersonate(url, user_agent=user_agent)
    if recovered is None:
        return None
    # The route only ever returns a body it got a 200 for.
    return 200, recovered.content_type, recovered.text


def looks_like_policy(content_type: str, body: str) -> bool:
    """Whether a 200 body is plausibly robots.txt rather than a page served at
    its URL.

    Two cheap signals, both chosen so a genuinely served policy passes: the
    content type does not claim to be a document (markup, JSON, an image), and the
    body does not open with a tag. An origin whose bot manager answers
    ``/robots.txt`` with ``<html><body><h1>It works!</h1>`` trips both.

    Deliberately lenient about a missing or unusual ``text/*`` type -- plenty of
    origins serve robots.txt with no charset, an odd subtype, or nothing at all,
    and rejecting those would discard real rules. The failure this guards against
    is the reverse one: reading a page as an empty ruleset, i.e. as consent.
    """
    main, _, sub = content_type.split(";")[0].strip().lower().partition("/")
    if main and main != "text":
        return False
    if sub in ("html", "xhtml+xml", "xml"):
        return False
    return not body.lstrip().startswith("<")


def _origin(url: str) -> str:
    """``scheme://host[:port]`` for an http(s) URL, else "" (no robots to fetch)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


class RobotsPolicy:
    """Per-crawl robots.txt cache and verdict source.

    Shared by the worker threads of a wave, so both the cache and its lock live
    here rather than in the traversal loop. Unlike the frontier, this is safe to
    touch from a worker: it owns its own state and mutating it changes no
    traversal decision other than the one being asked for.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        overrides: Iterable[str] = (),
        fetcher: Fetcher | None = None,
        escalated_fetcher: Fetcher | None = None,
    ) -> None:
        self.user_agent = user_agent
        # Registrable domains (or bare hosts) exempt from the check.
        self.overrides = normalize_domains(overrides)
        self._fetch = fetcher or _http_get
        # Second transport, tried only when the first yields no usable policy. It
        # declines by returning None unless impersonation is configured for the
        # host, so for every default deployment this is a no-op.
        self._escalate = escalated_fetcher or _impersonated_get
        self._lock = threading.Lock()
        # origin -> parser, or None for "no usable robots.txt" (fail open). The
        # None is cached too, so a dead robots.txt is fetched once, not per page.
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allows(self, url: str) -> bool:
        """Whether ``self.user_agent`` may fetch ``url`` per its origin's robots.txt."""
        if self.overrides and domain_of(url) in self.overrides:
            return True
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _parser_for(self, url: str) -> RobotFileParser | None:
        origin = _origin(url)
        if not origin:
            return None
        with self._lock:
            if origin in self._parsers:
                return self._parsers[origin]
        # Fetched OUTSIDE the lock: holding it across the network call would put
        # every worker in the wave behind one robots.txt. Two workers racing on the
        # same origin may both fetch it once; either result is equally valid, so
        # last write wins.
        parser = self._load(origin)
        with self._lock:
            self._parsers[origin] = parser
        return parser

    def _read(
        self, url: str, fetcher: Fetcher, *, escalated: bool = False
    ) -> str | None:
        """One transport's attempt at ``url``: the policy text, or None with a log
        line saying why there isn't one.

        Every ``None`` here ends in failing open, so each of these lines is the
        only record that a site's rules were never actually read.
        """
        where = " (impersonated)" if escalated else ""
        try:
            fetched = fetcher(url, self.user_agent)
        except Exception as e:  # noqa: BLE001 - any failure to obtain it fails open
            logsink.emit(
                f"    [robots] {url}{where} unavailable ({type(e).__name__}: {e})"
            )
            return None
        if fetched is None:
            # Only the escalated transport declines this way, and only because it
            # is not configured for this host: nothing to report.
            return None
        status, content_type, body = fetched
        if not 200 <= status < 300:
            logsink.emit(f"    [robots] {url}{where} returned {status}")
            return None
        if not looks_like_policy(content_type, body):
            # Parsing this would yield an empty ruleset, i.e. blanket consent, from
            # a site that told us nothing of the kind.
            logsink.emit(
                f"    [robots] {url}{where} returned a "
                f"{content_type.split(';')[0].strip() or 'non-text'} body, not a "
                f"policy — no rules obtained"
            )
            return None
        return body

    def _load(self, origin: str) -> RobotFileParser | None:
        """Fetch and parse ``origin``'s robots.txt; None when there isn't a usable
        one (see the module docstring on failing open)."""
        url = f"{origin}/robots.txt"
        body = self._read(url, self._fetch)
        if body is None:
            # The default transport got nothing usable. If this host is one the
            # deployment reads pages from with a browser fingerprint, ask again
            # that way rather than crawl it on a policy we never obtained.
            body = self._read(url, self._escalate, escalated=True)
        if body is None:
            logsink.emit(f"    [robots] treating {origin} as unrestricted")
            return None
        parser = RobotFileParser()
        # `parse` (not `read`, which would fetch it again with urllib and a
        # different User-Agent) and it sets the parser's last-checked stamp, which
        # `can_fetch` requires before it will answer.
        parser.parse(body.splitlines())
        logsink.emit(f"    [robots] loaded {url} ({len(body)}B)")
        return parser
