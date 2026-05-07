from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from itertools import count
from urllib.parse import parse_qsl, urldefrag, urlencode, urlsplit, urlunsplit


def canonical(url: str) -> str:
    no_frag, _ = urldefrag(url)
    parts = urlsplit(no_frag)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


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
