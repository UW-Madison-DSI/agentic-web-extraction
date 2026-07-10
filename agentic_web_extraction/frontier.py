import heapq
from dataclasses import dataclass, field
from itertools import count
from urllib.parse import parse_qsl, urldefrag, urlencode, urlsplit, urlunsplit

import tldextract


def canonical(url: str) -> str:
    no_frag, _ = urldefrag(url)
    parts = urlsplit(no_frag)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


# Registrable-domain extraction backed by the Public Suffix List (via
# tldextract). Constructed with no `suffix_list_urls` so it uses the PSL
# snapshot bundled with tldextract rather than fetching it over the network at
# runtime -- deterministic and offline-safe, and still covers the full set of
# multi-label public suffixes (`co.uk`, `ac.za`, `nic.in`, `com.au`, ...) that a
# hand-maintained suffix list would inevitably miss. Schema-agnostic: no logic
# tied to any particular website or domain.
_extract = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(host: str) -> str:
    """Best-effort registrable domain (eTLD+1) for `host`, via the Public
    Suffix List. Returns "" when `host` is empty or has no registrable domain
    (e.g. a bare hostname like `localhost` or an IP address)."""
    if not host:
        return ""
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def same_registrable_domain(url: str, seed_domain: str) -> bool | None:
    """True if `url`'s host shares `seed_domain` (an already-registrable
    domain), False if it is on a different registrable domain, None if `url`'s
    host is missing/unparseable (so the caller can treat "unknown" as not a
    penalty)."""
    host = urlsplit(url).netloc if url else ""
    dom = registrable_domain(host)
    if not dom or not seed_domain:
        return None
    return dom == seed_domain


@dataclass
class Frontier:
    _heap: list[tuple[float, int, str, str]] = field(default_factory=list)
    _counter: "count[int]" = field(default_factory=count)
    _seen: set[str] = field(default_factory=set)
    _visited: set[str] = field(default_factory=set)

    def push(self, url: str, score: float, source: str) -> bool:
        key = canonical(url)
        if key in self._visited or key in self._seen:
            return False
        self._seen.add(key)
        heapq.heappush(self._heap, (-float(score), next(self._counter), url, source))
        return True

    def pop(self) -> tuple[str, float, str] | None:
        while self._heap:
            neg_score, _, url, source = heapq.heappop(self._heap)
            key = canonical(url)
            if key in self._visited:
                continue
            return url, -neg_score, source
        return None

    def mark_visited(self, url: str) -> None:
        self._visited.add(canonical(url))

    def is_visited(self, url: str) -> bool:
        return canonical(url) in self._visited

    def __len__(self) -> int:
        return len(self._heap)
