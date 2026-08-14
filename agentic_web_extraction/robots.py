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

Domains in ``AWE_ROBOTS_OVERRIDES`` skip the check entirely -- for hosts whose
robots.txt blanket-disallows automated clients but whose content the operator is
authorized to read anyway.
"""

import threading
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import fetch as fetch_module
from . import logsink
from .frontier import domain_of, normalize_domains

# robots.txt is a small static file and it gates the page fetch behind it, so a
# slow origin must not hold up a whole wave for the crawl client's full 30s.
ROBOTS_TIMEOUT = httpx.Timeout(10.0, connect=10.0)


def _http_get(url: str) -> tuple[int, str]:
    """Fetch ``url`` with the crawl's own client; (status, body).

    Deliberately the same client as the pages: identical User-Agent (the agent the
    rules are matched against), identical connection pool, and no retry wrapper --
    a robots.txt that fails once fails open, and re-asking three times only
    lengthens the stall.
    """
    response = fetch_module.get_client().get(url, timeout=ROBOTS_TIMEOUT)
    return response.status_code, response.text


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
        fetcher: Callable[[str], tuple[int, str]] | None = None,
    ) -> None:
        self.user_agent = user_agent
        # Registrable domains (or bare hosts) exempt from the check.
        self.overrides = normalize_domains(overrides)
        self._fetch = fetcher or _http_get
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

    def _load(self, origin: str) -> RobotFileParser | None:
        """Fetch and parse ``origin``'s robots.txt; None when there isn't a usable
        one (see the module docstring on failing open)."""
        url = f"{origin}/robots.txt"
        try:
            status, body = self._fetch(url)
        except Exception as e:  # noqa: BLE001 - any failure to obtain it fails open
            logsink.emit(
                f"    [robots] {url} unavailable ({type(e).__name__}: {e}) "
                f"— treating {origin} as unrestricted"
            )
            return None
        if not 200 <= status < 300:
            logsink.emit(
                f"    [robots] {url} returned {status} — treating {origin} "
                f"as unrestricted"
            )
            return None
        parser = RobotFileParser()
        # `parse` (not `read`, which would fetch it again with urllib and a
        # different User-Agent) and it sets the parser's last-checked stamp, which
        # `can_fetch` requires before it will answer.
        parser.parse(body.splitlines())
        logsink.emit(f"    [robots] loaded {url} ({len(body)}B)")
        return parser
