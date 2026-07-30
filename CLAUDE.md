# CLAUDE.md

Guidance for Claude Code working in this repository.

## User preferences

- Keep the README up to date whenever you finish a feature.

## Commands

Managed with **uv** (Python ≥3.13, build backend `uv_build`):

```bash
uv sync            # install deps (incl. dev group)
uv run awe         # CLI entry point (`awe extract ...`)
uv run ruff check  # lint
uv run ruff format # format
uv run ty check    # type-check (Astral's ty, not mypy)
```

No test suite yet; if you add one it'll be `uv run pytest`.

## Architecture

A **best-first web traversal that ends in one consolidated extraction**: a frontier
of unvisited links where the LLM's relevance scoring is the *only* navigation policy.
One or more seed URLs are pushed into a single shared frontier at a sentinel score
(`float("inf")`) so every seed is fetched first; the budget is `max_fetches` *per
seed*. The loop processes the frontier in **parallel waves** — pop the top-N links
(`max_workers`), then concurrently fetch → normalize (HTML→Markdown) → pre-screen →
score outgoing links per page — folding results back on the main thread (which owns
the frontier; workers never mutate it). Pages that pass screening have their markdown
**collected**, not extracted per-page. When the frontier empties or the budget is
spent, the collected pages are concatenated and — if over `max_context_tokens`, or
always under `always_summarize` —
summarized down (criteria-aware map-reduce on the screen model), then a **single**
extraction runs over the whole thing. No `merge_extractions`, no dedup. Screen, link-
scorer, and summarizer share a cheap model; extraction uses a stronger one.

Key files: [extractor.py](agentic_web_extraction/extractor.py) (wave loop + consolidate),
[summarize.py](agentic_web_extraction/summarize.py) (fit-or-summarize),
[schema_outline.py](agentic_web_extraction/schema_outline.py) (schema → compact prompt outline),
[tokens.py](agentic_web_extraction/tokens.py) (tiktoken counting/splitting),
[frontier.py](agentic_web_extraction/frontier.py) (heap + visited set + snapshot + PSL
domain compare), [normalize.py](agentic_web_extraction/normalize.py),
[providers/](agentic_web_extraction/providers/),
[result.py](agentic_web_extraction/result.py),
[config.py](agentic_web_extraction/config.py) (`AWE_*` settings).

## Conventions to respect

- **Schema-agnostic — no built-in domains.** The caller supplies the Pydantic schema,
  the NL criterion, and the seed URL(s). Don't add domain-specific defaults or classes.
  The schema must be a `type[BaseModel]`, so multiplicity is a list field in a container
  schema (see [examples/grants.py](examples/grants.py)) — the one consolidated extraction
  fills that list from the pooled content of every screened-in page.
- **Domain-agnostic normalization.** [normalize.py](agentic_web_extraction/normalize.py)
  ships **no** site-specific text munging. Cache-stability strippers are caller-supplied
  via `text_filters` (a `Sequence[Callable[[str], str]]`); the reference set lives in
  [examples/strippers.py](examples/strippers.py). Don't move site-specific filters into
  the library.
- **Budget is the traversal lever.** `max_fetches` (env `AWE_MAX_FETCHES`, default 10)
  is per seed; total frontier budget is `max_fetches * len(seeds)`. Don't add depth caps
  or per-link relevance thresholds without an explicit ask — LLM scoring is the policy.
  `max_workers` (env `AWE_MAX_WORKERS`, default 8) is a *concurrency* knob (wave/beam
  width), not a relevance policy — best-first ordering holds within a wave; `1` = strictly
  sequential. Deliberate single toggles off by default: `prefer_seed_domain` (soft
  off-domain disfavor expressed *to the LLM*, not a math penalty — nothing excluded;
  generalized to "on any seed domain" for multi-seed) and `seed_is_content` (seeds are
  the content: skip screen + link-scoring, consolidate + extract).
- **Consolidate, don't merge.** Extraction is a *single* call over the concatenated
  markdown of all screened-in pages — there is no per-page extraction and no
  `merge_extractions`/dedup. If the concatenation exceeds `max_context_tokens`, fit it
  with the criteria-aware map-reduce in [summarize.py](agentic_web_extraction/summarize.py)
  (screen model), never by silently truncating. Concatenation order and the extraction
  cache key are made deterministic by sorting contributing pages on canonical URL.
  `always_summarize` (env `AWE_ALWAYS_SUMMARIZE`, default off) makes the overflow check
  non-gating: `fit_pages(always=True)` runs the map pass unconditionally, for callers who
  want the compression itself (boilerplate → retention list, cheaper extraction input).
  It only affects the *map* pass — the reduce loop and the hard-truncate guard stay
  keyed on being over budget — and it joins `ctx`/`enc` in the extraction cache key
  since it changes the extraction input. `SUMMARY` entries are keyed on content alone,
  so they stay shared between always-on and overflow-triggered runs.
- **Summarization is schema-aware, but must not become extraction.** It's the only lossy
  step (the extract model never sees the original text), so `fit_pages` threads the target
  schema into every `provider.summarize` call and the provider appends
  `SUMMARIZE_SCHEMA_GUIDANCE` + a [schema_outline.py](agentic_web_extraction/schema_outline.py)
  rendering to the instructions. Keep that prompt framed as a *retention list* — copy
  literal values verbatim, keep list-record boundaries intact, output prose and never
  JSON. A summarizer that fills the schema is doing extraction on the cheap model: it
  locks in early mistakes and discards the context the strong model disambiguates with.
  The outline (not the raw `model_json_schema()`) is what's sent: each `$defs` entry is
  emitted once, so nesting survives, shared sub-models aren't duplicated, and recursive
  schemas render in finite space — dotted-path flattening does none of that. Rendering is
  best-effort (`schema_outline_safe` falls back to compact JSON, then to `""`); a prompt
  detail must never abort a crawl. `schema` stays optional on the `Provider` protocol so
  `summarize` remains a generic utility, but the Extractor always passes it.
- **Uniform result shape.** `extract` always returns the same structure (`data`,
  `stopped_reason`, `pages_fetched`, `path`, `verdicts`, `content_tokens`,
  `extraction_input_tokens`, `summarized`, per-function token usage) whether it matched
  or exhausted budget. Plumbing this metadata is non-optional. See
  [result.py](agentic_web_extraction/result.py).
- **Frontier is single-threaded; workers are pure.** Only the main thread pops/pushes/
  marks the `Frontier`. `_process_page` runs on pool threads and returns a `_PageOutcome`;
  it reads a `frontier.snapshot()` (frozen set) to pre-filter links but never mutates
  shared state. Provider usage accumulation is lock-guarded for the same reason. Keep
  new per-page work inside the worker and new frontier work in the fold loop.
- **Logging: never a bare `print`.** All diagnostics go through `logsink.emit` → stderr
  (stdout is reserved for result JSON). A `log_file` path (env `AWE_LOG_FILE`, empty =
  off) also appends timestamped lines. See [logsink.py](agentic_web_extraction/logsink.py).
- **On-by-default LLM cache is generic, at three levels.** Caching is on by default: the
  Extractor builds a `SqliteKVCache` at `AWE_LLM_CACHE` (`data/llm_cache.sqlite`) unless
  the caller passes their own `KVCache`, passes `cache=None` to disable, or the setting is
  empty. All of [cache.py](agentic_web_extraction/cache.py) stays domain-agnostic — values
  are opaque JSON round-tripped through the caller's schema. A version stamp
  (`page_cache_version`) over the criterion, schema JSON, the provider's `prompt_signature`,
  the models, and the normalize flag is mixed into every key, so editing any
  prompt/schema/criterion — or requesting a different schema for the same URL — misses.
  Namespaces: (1) `PAGE` — per-page screen verdict + link scores (no per-page extraction
  anymore); replays with zero LLM calls on an unchanged content hash. (2) `EXTRACT` — the
  single consolidated extraction, keyed on `extract_cache_key(sorted page-cache keys)` plus
  the `max_context_tokens`/encoding settings; replays only when the *exact same set* of
  screened-in pages (same content) recurs. Its value wraps the object **and** the
  context-size metadata (`content_tokens`/`extraction_input_tokens`/`summarized`) so a hit
  replays the full result. (3) `SUMMARY` — per-chunk summaries keyed on the version stamp +
  chunk content hash. Don't reintroduce a merge namespace. The version stamp already
  folds in `schema_json` *and* `prompt_signature`, so schema-aware summarization needed no
  key change — editing the schema or the retention prompt invalidates stored summaries on
  its own. Keep the *rendered* outline out of `prompt_signature` (the schema JSON it
  derives from is already in the stamp; adding both is redundant).
- **Don't fork CLI vs Python logic.** The CLI wires to the same `Extractor` the Python
  API exposes. `--max-context-tokens`/`--always-summarize`/`--max-workers` are
  settings-only knobs, so the CLI injects them via `settings.model_copy(update=...)`,
  not `extract()` args.

## Dependency gotchas

- `httpx` — plain client, **no HTTP-response cache** (no hishel). Fetching is cheap and
  the frontier never re-fetches a URL within a crawl, so an HTTP cache wasn't worth the
  memory/disk; the content-addressed LLM cache handles the expensive re-work instead.
- `tldextract` — PSL lookup for the domain comparison; constructed with
  `suffix_list_urls=()` to use the bundled offline snapshot (no runtime network fetch).
- `tiktoken` — token counting + token-aware splitting ([tokens.py](agentic_web_extraction/tokens.py)).
  `encoding_for_model` only knows shipped OpenAI models, so unknown names (a future OpenAI
  model, or a non-OpenAI model over a compatible endpoint) fall back to a configurable base
  encoding (`AWE_TIKTOKEN_ENCODING`, default `o200k_base`). Counts are approximate for non-
  OpenAI models — fine, they only drive the fit-or-summarize decision, not billing.
- `markitdown` (HTML→MD), `openai` (default provider, swappable via `AWE_PROVIDER`;
  client tuned to `max_retries=5` / `connect=30s`; `AWE_USE_FLEX` sends every call at
  `service_tier="flex"` for Batch-API rates — 50% off, synchronous, stacks with prompt
  caching — with a *per-call* fallback to `"auto"` on the uncharged
  `429 resource_unavailable`, and a raised read timeout. Off by default. All four call
  sites route through `_tiered(send)`, which hands the tier to a lambda so each keeps
  its typed argument list; with flex off it passes `omit` so nothing reaches the wire.
  The tier is deliberately **not** in any cache key — it changes price and latency, not
  response content), `pydantic`/`pydantic-settings`
  (`AWE_*`, `OPENAI_*` env), `tenacity` (retries), `typer` (CLI; `--schema` =
  `import.path:ClassName`).

## CLI contract

```
awe extract --schema ./schemas.py:Opportunities --criteria "..." \
  --seed-url https://... [--seed-url https://... ...] \
  [--max-fetches 10] [--max-context-tokens 128000] [--max-workers 8] \
  [--always-summarize | --no-always-summarize] \
  [--seed-is-content | --no-seed-is-content] \
  [--prefer-seed-domain | --no-prefer-seed-domain] [--log-file log.txt] [--no-cache]
```

`--criteria` accepts an inline string or `@path/to/file.txt`. `--schema` takes
`import.path:ClassName` or `path/file.py:ClassName`. `--seed-url` is repeatable (pools
seeds into one extraction; budget is per seed). `text_filters` are Python-API-only
(callables — not CLI-expressible).

## Layout

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` —
enforced by `[tool.uv.build-backend].module-root = ""`.
