"""Generic, schema-agnostic caching support for the crawler.

The frontier loop is the only place that knows, per page, the normalized content
and the two LLM outputs derived from it (screen verdict, link scores), so page
caching has to hook here. Extraction happens once, over the concatenated content
of every screened-in page, so its cache is keyed on the *set* of contributing
pages (see `extract_cache_key`). This module defines only the cache *interface*
and content-hashing helpers; the concrete persistent store is supplied by the
caller (so storage-path policy stays out of the crawler), and no domain knowledge
lives here — the extracted object is round-tripped as opaque JSON via the caller's
schema.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class KVCache(Protocol):
    """A namespaced string key/value cache that persists across runs.

    `get` returns None on a miss. Namespaces keep unrelated cache families
    (e.g. per-page outcomes vs. merged results) from colliding.
    """

    def get(self, namespace: str, key: str) -> str | None: ...

    def put(self, namespace: str, key: str, value: str) -> None: ...


class SqliteKVCache:
    """The default concrete `KVCache`: a single `kv(namespace, key, value)` table.

    On by default (the Extractor builds one at `AWE_LLM_CACHE` unless the caller
    passes their own store or disables caching), so the storage policy lives here
    rather than in the crawler. Persisting across runs is the whole point -- an
    unchanged page replays its screen/link-score outcomes (and the consolidated
    extraction replays when every contributing page hit the cache) with zero LLM
    calls. A composite primary key keeps namespaces from colliding, and
    `INSERT .. ON CONFLICT` makes a re-`put` idempotent.
    No domain knowledge lives here: values are opaque JSON strings the caller round-
    trips through its own schema.
    """

    def __init__(self, path: str | Path = "data/llm_cache.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so one Extractor's cache is safe to touch from a
        # helper thread; a lock serializes access since a raw connection isn't
        # concurrency-safe on its own.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY (namespace, key))"
        )
        self._conn.commit()

    def get(self, namespace: str, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        return row[0] if row is not None else None

    def put(self, namespace: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
                (namespace, key, value),
            )
            self._conn.commit()


PAGE_NAMESPACE = "page"
# The single consolidated extraction, keyed on the set of contributing pages.
EXTRACT_NAMESPACE = "extract"
# Per-chunk criteria-aware summaries produced when the concatenation overflows.
SUMMARY_NAMESPACE = "summary"


@dataclass
class CachedPage:
    """The cached, LLM-derived traversal outcome for one page at a content hash.

    Extraction is no longer per-page (it runs once over the concatenated content
    of every screened-in page), so only the two traversal decisions are cached
    here: the screen verdict and the scorer's `[url, score]` link pairs.
    Everything is JSON-native so the store need not know the caller's schema.
    """

    screen_match: bool
    screen_reason: str
    link_scores: list[list] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CachedPage":
        d = json.loads(raw)
        return cls(
            screen_match=d["screen_match"],
            screen_reason=d.get("screen_reason", ""),
            link_scores=d.get("link_scores") or [],
        )


def content_hash(markdown: str) -> str:
    """Stable SHA-256 of a page's normalized content, used as the cache key.

    Hashing the normalized markdown (not the raw bytes or HTTP headers) means
    cosmetic byte-level noise that markdownifies away does not bust the cache,
    while any change to the text the LLM actually sees does.
    """
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def extract_cache_key(page_cache_keys: Sequence[str]) -> str:
    """Fingerprint of the *set* of pages feeding the one consolidated extraction.

    Extraction is a pure function of the concatenated (and possibly summarized)
    content of every screened-in page, and each page's `page` cache key already
    embeds everything that determines its content and screen outcome (content hash
    + version stamp + URL + seed-domain signal). Hashing the *set* of those keys
    therefore means the extraction replays from cache exactly when the same set of
    pages with the same content recurs: change, add, or drop any source page and
    its key changes, so this key misses and the extraction re-runs. Sorted so the
    order pages were gathered in (nondeterministic under parallel waves) doesn't
    matter. The Extractor prefixes this with the version stamp and the context/
    encoding settings, which govern the summarize-or-not decision.
    """
    material = "\x00".join(sorted(page_cache_keys))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def page_cache_version(
    *,
    criteria: str,
    schema_json: str,
    prompt_signature: str,
    model_screen: str,
    model_extract: str,
    normalize: bool,
) -> str:
    """Version stamp mixed into page-cache keys.

    Derived from every input that determines a page's screen/extract/score
    outcome, so editing the criterion, the schema, the provider's prompt
    templates, the models, or the normalize setting auto-invalidates page-cache
    entries without a manual bump.
    """
    material = "\x00".join(
        [
            criteria,
            schema_json,
            prompt_signature,
            model_screen,
            model_extract,
            "1" if normalize else "0",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
