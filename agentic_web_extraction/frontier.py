import heapq
from collections.abc import Iterable
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


def domain_of(value: str) -> str:
    """Allow/deny key for `value`, which may be a full URL, a bare host, or a
    host:port.

    Normally the registrable domain (so `www.example.com`, `grants.example.com`
    and `https://example.com/x` all key to `example.com`). Falls back to the bare
    lowercased host when there is no registrable domain -- `localhost`, an IP
    literal, an intranet name -- so those can still be named explicitly instead
    of being silently un-nameable. Returns "" when no host can be read at all.
    """
    text = value.strip()
    if not text:
        return ""
    # urlsplit only finds a host in the netloc, which needs the `//` marker; a
    # bare "example.com:8080" would otherwise parse as scheme "example.com".
    if "//" not in text:
        text = f"//{text}"
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        # Malformed authority (e.g. an unclosed IPv6 bracket). No host, so no key.
        return ""
    key = registrable_domain(host)
    if key:
        return key
    # urlsplit is lenient about what it will call a netloc, so guard the bare-host
    # fallback: whitespace means this was never a host, and returning it would
    # invent a key that matches nothing (a silently ineffective allowlist entry).
    return "" if not host or any(c.isspace() for c in host) else host.lower()


def normalize_domains(entries: Iterable[str]) -> frozenset[str]:
    """`domain_of` over an iterable, dropping entries that yield no key. Accepts
    hosts or URLs so a caller can hand over whichever it has."""
    return frozenset(key for key in (domain_of(entry) for entry in entries) if key)


def split_domains(value: str) -> frozenset[str]:
    """`normalize_domains` over a comma-separated string (an `AWE_*` setting)."""
    return normalize_domains(value.split(","))


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

    def snapshot(self) -> frozenset[str]:
        """Immutable copy of every canonical URL already seen or visited.

        Handed to worker threads so they can pre-filter links they'd otherwise
        score pointlessly, without reading the live (main-thread-mutated) sets.
        A link that slips through (queued by another page in the same wave) is
        still deduped by `push`, so the snapshot only needs to be good enough.
        """
        return frozenset(self._seen | self._visited)

    def __len__(self) -> int:
        return len(self._heap)
