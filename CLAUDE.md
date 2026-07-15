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
- **Four pluggable stages**: Fetch (HTML, optionally linked PDFs), Normalize (HTML→Markdown via `markitdown` for token reduction; the library ships **no** site-specific text munging — it's domain-agnostic), Pre-screen (cheap binary LLM call against the user criterion), Score links (LLM ranks every outgoing link's promise), Extract (structured-output LLM call producing the user's Pydantic schema). Pre-screen and link-scorer share a model by default — both are cheap comparison calls; extraction uses a stronger model.
- **Caller-supplied text filters (cache-stability hacks live outside the library).** `to_markdown` and `Extractor(..., text_filters=)` accept a `Sequence[Callable[[str], str]]` applied in order to the normalized markdown. This is where a caller strips *volatile per-response fragments* (rotating anti-bot tokens, per-render timestamps, randomized honeypot labels, shuffled recommendation carousels) so a page's content hash stays stable across fetches and the page cache can actually hit. The library ships none of these — keeping it site-agnostic is deliberate; **do not** add site-specific strippers back into `normalize.py`. A ready-made set keyed to specific real-world sites (Cloudflare email obfuscation, Foundant/`grantinterface.com` footer tooltip, Gravity Forms honeypot, EREF `erefdn.org` related-products carousel, CyberGrants empty layout tables) lives in [examples/strippers.py](examples/strippers.py) as the reference for what callers pass. Each filter removes only content-free/invisible markup, never text an LLM would use. Text filters aren't hashed into the page-cache version stamp — they change the normalized text itself, so the content hash captures any change naturally.
- **Schema-agnostic by design.** No built-in domains (no "grants" or "companies" classes). The caller supplies the Pydantic schema, the natural-language screening criterion, and the seed URL. Don't add domain-specific defaults. The schema must be a `type[BaseModel]` — a bare `list[...]` can't be passed — so **per-page multiplicity is modeled as a list field inside a container schema** (e.g. `Opportunities.items: list[Opportunity]`), not by returning multiple objects per match. One `provider.extract` call fills one container instance per page; `merge_extractions` then fuses the per-page containers. [examples/grants.py](examples/grants.py) demonstrates both a scalar schema (`Opportunity`) and a collection schema (`Opportunities`) whose `merge_extractions` does an LLM dedup via `provider.extract(..., usage_tag="merge")` (with a deterministic link-dedup fallback).
- **Single-knob philosophy.** `max_fetches` (env: `AWE_MAX_FETCHES`, default 10) is the primary traversal lever in v0. Depth caps and per-link relevance thresholds are intentionally **not** user-configurable — the LLM's scoring is the policy. Don't add these knobs without an explicit ask.
- **Opt-in soft same-domain preference (single knob) — expressed to the LLM, not as a score weight.** The one deliberate exception to single-knob-traversal, **off by default**: `prefer_seed_domain` (env `AWE_PREFER_SEED_DOMAIN`, default `False`; constructor arg; CLI `--prefer-seed-domain/--no-prefer-seed-domain`). When on, the crawler computes an `on_seed_domain` signal in Python (via `same_registrable_domain` in [frontier.py](agentic_web_extraction/frontier.py)) and passes it — along with the seed URL and the page/link URL — into the pre-screen and link-scorer calls, whose prompts gain an instruction to *disfavor* off-domain content. The navigation decision stays the LLM's: it's a soft preference, **not** domain scoping and **not** a math penalty — off-domain pages/links are never excluded, so a clearly on-target off-domain page still matches / scores high. (This replaces the old `off_domain_weight` float, which multiplied the raw score at push time; that knob is gone.) The provider stays decoupled from navigation internals — the extractor computes the signal (screen: one bool for the page; score_links: a `{url: bool}` map) and passes it in only when the flag is on; with the flag off the screen/score prompts are byte-identical to the default path. The registrable-domain comparison (`registrable_domain` / `same_registrable_domain`) uses the **Public Suffix List** via `tldextract` (bundled snapshot, no runtime network fetch), so `co.uk` / `ac.za` / `nic.in` / `gov.uk` etc. resolve correctly with no hand-maintained suffix list and no domain-specific logic; comparison is at the registrable-domain (eTLD+1) level, so all of `*.wisc.edu` count as one domain. Unlike the raw LLM score, this signal **does** feed the model, so when the flag is on the seed's registrable domain is mixed into the page-cache key (a `seeddom=<domain>` segment); the raw `link_scores` are still cached unmodified, and the default (off) path keeps the exact prior cache-key shape so existing entries still hit.
- **Stop-on-first-match vs gather-all.** `stop_on_first_match` (env: `AWE_STOP_ON_FIRST_MATCH`, default `False`; per-call override on `extract`/`extract_batch`; CLI `--stop-on-first-match/--gather-all-matches`) toggles the loop's termination. Default `False` keeps the gather-all-then-merge behavior described above. When `True`, the loop breaks the moment a page matches and is extracted — it skips that page's link scoring and, deliberately, does **not** cache the page (its cache record would lack link scores and could mislead a later gather-all run). Either way `stopped_reason` is `"match"` when ≥1 page matched.
- **Result shape is uniform across success and failure.** Whether the agent matched or ran out of budget, it returns the same structure: `data` (Pydantic instance or `None`), `stopped_reason` ("match" | "budget_exhausted"), `pages_fetched`, `path` (URLs visited in order), `verdicts` (one `PageVerdict` per screened page, not just the match), and per-call-purpose token usage (`usage_by_function` + `function_model`, so cost is reconstructable per model). Plumbing this metadata is non-optional.
- **Optional content cache is schema-agnostic and opt-in.** `Extractor(..., cache=)` takes a generic `KVCache` (`get(ns, key)` / `put(ns, key, value)`, defined in [cache.py](agentic_web_extraction/cache.py)) and nothing else — no domain types. Inside the frontier loop, if a page's normalized-markdown hash (mixed with a version stamp over criterion/schema/models) is unchanged, the crawler replays that page's screen verdict, extracted data (round-tripped through `self.schema`), and link scores with zero LLM calls, and rebuilds the frontier from the cached scores. It still fetches every page, so `pages_fetched` and the `max_fetches` budget are unaffected. When `cache` is None the crawler behaves exactly as before. Transient stage errors are not cached. The same cache object is forwarded to the schema's `merge_extractions(..., cache=)` so callers can cache the merge too; keep that call's `try/except TypeError` degradation intact.

## Key dependencies and what they imply

- `httpx` + `hishel` — HTTP with on-disk caching (`AWE_HTTP_CACHE`, default `data/http_cache.sqlite`; empty = in-memory). The client sends `Cache-Control: no-cache` so every stored response is revalidated (conditional GET) rather than served blindly-fresh — callers relying on a content hash to detect change must see current bytes.
- `markitdown` — the chosen HTML→Markdown converter. Keep it pluggable but it's the default.
- `openai` — default provider. The provider layer must be abstracted enough to swap (env: `AWE_PROVIDER`).
- `pydantic` + `pydantic-settings` — schemas come from callers; settings load from env (`AWE_*`, `OPENAI_*`).
- `tenacity` — for retrying flaky fetches and rate-limited LLM calls.
- `tldextract` — Public Suffix List lookup for the `prefer_seed_domain` registrable-domain comparison (computing the `on_seed_domain` signal). Constructed with `suffix_list_urls=()` so it uses the bundled PSL snapshot (deterministic, offline) rather than fetching over the network at runtime.
- `typer` — CLI. The `--schema` flag uses an `import.path:ClassName` form to load the caller's Pydantic model dynamically.

## CLI contract

```
awe extract \
  --schema ./schemas.py:Opportunity \
  --criteria "..." \
  --seed-url https://... \
  --max-fetches 10 \
  [--stop-on-first-match | --gather-all-matches] \
  [--prefer-seed-domain | --no-prefer-seed-domain]
```

`--criteria` accepts either an inline string or `@path/to/file.txt`. `--schema` takes `import.path:ClassName` or `path/file.py:ClassName` (e.g. `examples/grants.py:Opportunities`). `text_filters` are **Python-API-only** (they're callables — not expressible on the CLI); a CLI crawl runs with no filters. The CLI must wire to the same `Extractor` class the Python API exposes — don't fork logic between the two entry points.

## Layout note

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` — `[tool.uv.build-backend].module-root = ""` enforces this.
