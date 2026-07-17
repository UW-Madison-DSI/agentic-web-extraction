"""Generic, schema-agnostic caching support for the crawler.

The frontier loop is the only place that knows, per page, the normalized content
and the three LLM outputs derived from it (screen verdict, extracted data, link
scores), so page caching has to hook here. This module defines only the cache
*interface* and content-hashing helpers; the concrete persistent store is
supplied by the caller (so storage-path policy stays out of the crawler), and no
domain knowledge lives here — extracted data is round-tripped as a plain dict via
the caller's schema, and the caller's `merge_extractions` uses the same generic
store for its own (domain-specific) caching.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class KVCache(Protocol):
    """A namespaced string key/value cache that persists across runs.

    `get` returns None on a miss. Namespaces keep unrelated cache families
    (e.g. per-page outcomes vs. merged results) from colliding.
    """

    def get(self, namespace: str, key: str) -> str | None: ...

    def put(self, namespace: str, key: str, value: str) -> None: ...


PAGE_NAMESPACE = "page"


@dataclass
class CachedPage:
    """The cached, LLM-derived outcome for one page at a given content hash.

    `extracted` is the extraction schema's `model_dump(mode="json")` (or None when
    the page did not match the screen); `link_scores` is the scorer's output as
    `[url, score]` pairs. Everything is JSON-native so the store need not know the
    caller's schema.
    """

    screen_match: bool
    screen_reason: str
    extracted: dict | None = None
    link_scores: list[list] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CachedPage":
        d = json.loads(raw)
        return cls(
            screen_match=d["screen_match"],
            screen_reason=d.get("screen_reason", ""),
            extracted=d.get("extracted"),
            link_scores=d.get("link_scores") or [],
        )


def content_hash(markdown: str) -> str:
    """Stable SHA-256 of a page's normalized content, used as the cache key.

    Hashing the normalized markdown (not the raw bytes or HTTP headers) means
    cosmetic byte-level noise that markdownifies away does not bust the cache,
    while any change to the text the LLM actually sees does.
    """
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def page_cache_version(
    *,
    criteria: str,
    schema_json: str,
    model_screen: str,
    model_extract: str,
    normalize: bool,
) -> str:
    """Version stamp mixed into page-cache keys.

    Derived from every input that determines a page's screen/extract/score
    outcome, so editing the criterion, the schema, the models, or the normalize
    setting auto-invalidates page-cache entries without a manual bump.
    """
    material = "\x00".join(
        [criteria, schema_json, model_screen, model_extract, "1" if normalize else "0"]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
