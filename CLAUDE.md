# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Important User Preferences

- Make sure README is up to date every time you finish a feature.

## Commands

Project is managed with **uv** (Python ≥3.13, build backend `uv_build`).

```bash
uv sync                        # install deps (incl. dev group)
uv run awe                     # run the CLI entry point (`awe extract ...`)
uv run ruff check              # lint
uv run ruff format             # format
uv run ty check                # type-check (Astral's ty, not mypy)
```

There is no test suite yet; if you add one, it will likely be `uv run pytest`.

## Architecture

The agent is a **best-first web traversal** over a frontier of unvisited links, where the LLM's relevance scoring is the *only* navigation policy. Per-stage notes:

- **Frontier loop** is the spine. Each iteration: pop highest-scoring unvisited link → fetch → normalize → pre-screen → on a match, extract and record the page; **either way**, score outgoing links and merge them into the frontier. The loop does **not** stop on the first match — it runs until the frontier empties or the `max_fetches` budget is exhausted, accumulating **every** matching page. After the loop, all matches are combined via the schema's optional `merge_extractions(matches, *, provider, cache)` classmethod (falling back to the first match if the schema doesn't define one). `stopped_reason` is `"match"` when at least one page matched, else `"budget_exhausted"`. Visited-set dedupe is required so the agent doesn't re-fetch within a traversal — dedupe is on the *resolved* URL (after redirects), so two requested URLs like `/foo` and `/foo/` that collapse to the same page are only screened/counted/cached once. `max_fetches` counts only **readable** pages — a fetch that errors (`kind="error"`) or returns a non-HTML/PDF body (`kind="skipped"`) does no LLM work, so it's marked visited and recorded in `path` but does **not** consume a budget slot. The budget caps the pages we spend model calls on, not raw fetch attempts, so a run of dead links or binary files can't starve the crawl of real pages.
- **Four pluggable stages**: Fetch (HTML, optionally linked PDFs), Normalize (HTML→Markdown via `markitdown` for token reduction; also strips volatile per-response tokens — currently Cloudflare's rotating `/cdn-cgi/l/email-protection#<hex>` and Foundant's `grantinterface.com` footer-link title tooltip (a per-render timestamp + rotating hex token) — so a page's content hash stays stable across fetches and the page cache can actually hit), Pre-screen (cheap binary LLM call against the user criterion), Score links (LLM ranks every outgoing link's promise), Extract (structured-output LLM call producing the user's Pydantic schema). Pre-screen and link-scorer share a model by default — both are cheap comparison calls; extraction uses a stronger model.
- **Schema-agnostic by design.** No built-in domains (no "grants" or "companies" classes). The caller supplies the Pydantic schema, the natural-language screening criterion, and the seed URL. Don't add domain-specific defaults. The schema must be a `type[BaseModel]` — a bare `list[...]` can't be passed — so **per-page multiplicity is modeled as a list field inside a container schema** (e.g. `Opportunities.items: list[Opportunity]`), not by returning multiple objects per match. One `provider.extract` call fills one container instance per page; `merge_extractions` then fuses the per-page containers. [examples/grants.py](examples/grants.py) demonstrates both a scalar schema (`Opportunity`) and a collection schema (`Opportunities`) whose `merge_extractions` does an LLM dedup via `provider.extract(..., usage_tag="merge")` (with a deterministic link-dedup fallback).
- **Single-knob philosophy.** `max_fetches` (env: `AWE_MAX_FETCHES`, default 10) is the *only* traversal lever in v0. Depth caps, domain scoping, and per-link relevance thresholds are intentionally **not** user-configurable — the LLM's scoring is the policy. Don't add these knobs without an explicit ask.
- **Stop-on-first-match vs gather-all.** `stop_on_first_match` (env: `AWE_STOP_ON_FIRST_MATCH`, default `False`; per-call override on `extract`/`extract_batch`; CLI `--stop-on-first-match/--gather-all-matches`) toggles the loop's termination. Default `False` keeps the gather-all-then-merge behavior described above. When `True`, the loop breaks the moment a page matches and is extracted — it skips that page's link scoring and, deliberately, does **not** cache the page (its cache record would lack link scores and could mislead a later gather-all run). Either way `stopped_reason` is `"match"` when ≥1 page matched.
- **Result shape is uniform across success and failure.** Whether the agent matched or ran out of budget, it returns the same structure: `data` (Pydantic instance or `None`), `stopped_reason` ("match" | "budget_exhausted"), `pages_fetched`, `path` (URLs visited in order), `verdicts` (one `PageVerdict` per screened page, not just the match), and per-call-purpose token usage (`usage_by_function` + `function_model`, so cost is reconstructable per model). Plumbing this metadata is non-optional.
- **Optional content cache is schema-agnostic and opt-in.** `Extractor(..., cache=)` takes a generic `KVCache` (`get(ns, key)` / `put(ns, key, value)`, defined in [cache.py](agentic_web_extraction/cache.py)) and nothing else — no domain types. Inside the frontier loop, if a page's normalized-markdown hash (mixed with a version stamp over criterion/schema/models) is unchanged, the crawler replays that page's screen verdict, extracted data (round-tripped through `self.schema`), and link scores with zero LLM calls, and rebuilds the frontier from the cached scores. It still fetches every page, so `pages_fetched` and the `max_fetches` budget are unaffected. When `cache` is None the crawler behaves exactly as before. Transient stage errors are not cached. The same cache object is forwarded to the schema's `merge_extractions(..., cache=)` so callers can cache the merge too; keep that call's `try/except TypeError` degradation intact.

## Key dependencies and what they imply

- `httpx` + `hishel` — HTTP with on-disk caching (`AWE_HTTP_CACHE`, default `data/http_cache.sqlite`; empty = in-memory). The client sends `Cache-Control: no-cache` so every stored response is revalidated (conditional GET) rather than served blindly-fresh — callers relying on a content hash to detect change must see current bytes.
- `markitdown` — the chosen HTML→Markdown converter. Keep it pluggable but it's the default.
- `openai` — default provider. The provider layer must be abstracted enough to swap (env: `AWE_PROVIDER`).
- `pydantic` + `pydantic-settings` — schemas come from callers; settings load from env (`AWE_*`, `OPENAI_*`).
- `tenacity` — for retrying flaky fetches and rate-limited LLM calls.
- `typer` — CLI. The `--schema` flag uses an `import.path:ClassName` form to load the caller's Pydantic model dynamically.

## CLI contract

```
awe extract \
  --schema ./schemas.py:Opportunity \
  --criteria "..." \
  --seed-url https://... \
  --max-fetches 10 \
  [--stop-on-first-match | --gather-all-matches]
```

`--criteria` accepts either an inline string or `@path/to/file.txt`. `--schema` takes `import.path:ClassName` or `path/file.py:ClassName` (e.g. `examples/grants.py:Opportunities`). The CLI must wire to the same `Extractor` class the Python API exposes — don't fork logic between the two entry points.

## Layout note

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` — `[tool.uv.build-backend].module-root = ""` enforces this.
