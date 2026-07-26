"""Fit the concatenated screened-in pages within the extraction context budget.

The consolidate-then-extract flow feeds one extraction call the concatenated
markdown of every page that passed screening. When that concatenation exceeds
``max_context_tokens`` it has to be compressed first. This module does that with
a criteria-aware map-reduce on the cheap *screen* model:

* **map** — summarize each page independently (a page is the natural, cache-
  stable unit; a page larger than the chunk budget is token-sliced first);
* **reduce** — concatenate the per-page summaries and, while still over budget,
  summarize the combined text again, until it fits or a pass stops shrinking it.

Each chunk summary is cached by its content hash, so an unchanged page replays
its summary with no LLM call. Sizes are logged at every level (and the final
"summarized N -> M tokens" line) so a caller can see what happened.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel

from .cache import SUMMARY_NAMESPACE, KVCache, content_hash
from .providers import Provider
from .tokens import count_tokens, split_by_tokens

# Hard cap on reduce passes so a model that fails to compress can't loop forever;
# a final hard-truncate guarantees the extraction input never overflows.
_MAX_REDUCE_LEVELS = 5

Page = tuple[str, str]  # (url, markdown)


def _concat(pages: Sequence[Page]) -> str:
    """Join pages into one document, each introduced by a provenance marker."""
    return "\n\n".join(f"--- SOURCE: {url}\n{md}" for url, md in pages)


def _summarize_chunk(
    chunk: str,
    *,
    criterion: str,
    schema: type[BaseModel] | None,
    provider: Provider,
    cache: KVCache | None,
    version: str,
) -> str:
    """Summarize one chunk, replaying from the summary cache when unchanged.

    Keyed on the version stamp (which already folds in the criterion, the schema's
    JSON, the screen model, and the summarize prompt) plus the chunk's content
    hash, so the same chunk text under the same crawl config replays for free --
    and editing the schema, which changes what the summarizer is told to keep,
    invalidates every stored summary without a key change here.
    """
    key = f"{version}:{content_hash(chunk)}"
    if cache is not None:
        hit = cache.get(SUMMARY_NAMESPACE, key)
        if hit is not None:
            return hit
    summary = provider.summarize(chunk, criterion, schema=schema)
    if cache is not None:
        try:
            cache.put(SUMMARY_NAMESPACE, key, summary)
        except Exception:  # noqa: BLE001 - caching is best-effort
            pass
    return summary


def _summarize_text(
    text: str,
    *,
    budget: int,
    criterion: str,
    schema: type[BaseModel] | None,
    provider: Provider,
    model: str,
    encoding_name: str,
    cache: KVCache | None,
    version: str,
) -> str:
    """Compress `text`: split into `budget`-token chunks, summarize each, join."""
    chunks = split_by_tokens(text, budget, model, encoding_name)
    parts = [
        _summarize_chunk(
            chunk,
            criterion=criterion,
            schema=schema,
            provider=provider,
            cache=cache,
            version=version,
        )
        for chunk in chunks
    ]
    return "\n\n".join(parts)


def fit_pages(
    pages: Sequence[Page],
    *,
    criterion: str,
    schema: type[BaseModel] | None = None,
    provider: Provider,
    max_context_tokens: int,
    model: str,
    encoding_name: str,
    cache: KVCache | None,
    version: str,
    log: Callable[[str], None],
) -> tuple[str, bool, int, int]:
    """Concatenate `pages` and shrink them to fit `max_context_tokens`.

    `schema` is the schema the consolidated extraction will produce; it is passed
    through to every summarize call so the compressor knows which concrete values
    (dates, numbers, identifiers, URLs) must survive, not just which topics are
    relevant. Optional, so a caller can compress criterion-only.

    Returns ``(text, summarized, content_tokens, extraction_input_tokens)`` where
    ``content_tokens`` is the raw concatenation's size and
    ``extraction_input_tokens`` is the size of what actually feeds extraction
    (equal to ``content_tokens`` when no summarization was needed).
    """
    concat = _concat(pages)
    content_tokens = count_tokens(concat, model, encoding_name)
    if content_tokens <= max_context_tokens:
        log(f"    [context] {content_tokens} tokens (<= budget {max_context_tokens})")
        return concat, False, content_tokens, content_tokens

    log(
        f"    [summarize] concatenated {content_tokens} tokens exceeds budget "
        f"{max_context_tokens}; summarizing on {provider.model_screen}"
    )
    budget = max(1, max_context_tokens)

    # Map: summarize each page independently (cache-stable unit), then reduce.
    summarized_pages = [
        (
            url,
            _summarize_text(
                md,
                budget=budget,
                criterion=criterion,
                schema=schema,
                provider=provider,
                model=model,
                encoding_name=encoding_name,
                cache=cache,
                version=version,
            ),
        )
        for url, md in pages
    ]
    current = _concat(summarized_pages)
    cur_tokens = count_tokens(current, model, encoding_name)
    level = 1
    log(f"    [summarize] level {level}: {content_tokens} -> {cur_tokens} tokens")

    while cur_tokens > max_context_tokens and level < _MAX_REDUCE_LEVELS:
        level += 1
        current = _summarize_text(
            current,
            budget=budget,
            criterion=criterion,
            schema=schema,
            provider=provider,
            model=model,
            encoding_name=encoding_name,
            cache=cache,
            version=version,
        )
        new_tokens = count_tokens(current, model, encoding_name)
        log(f"    [summarize] level {level}: -> {new_tokens} tokens")
        if new_tokens >= cur_tokens:
            cur_tokens = new_tokens
            break  # not converging; hard-truncate below
        cur_tokens = new_tokens

    if cur_tokens > max_context_tokens:
        # Final guard: a run of summaries that won't shrink below budget still must
        # not overflow the extraction call, so hard-truncate to the budget.
        current = split_by_tokens(current, max_context_tokens, model, encoding_name)[0]
        cur_tokens = count_tokens(current, model, encoding_name)
        log(f"    [summarize] still over budget; hard-truncated to {cur_tokens} tokens")

    log(f"    [summarize] summarized {content_tokens} -> {cur_tokens} tokens")
    return current, True, content_tokens, cur_tokens
