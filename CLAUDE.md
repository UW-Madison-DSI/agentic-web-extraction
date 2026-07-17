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

A **best-first web traversal**: a frontier of unvisited links where the LLM's
relevance scoring is the *only* navigation policy. The frontier loop is the spine —
pop the highest-scoring link → fetch → normalize (HTML→Markdown) → pre-screen → on a
match, extract; either way, score outgoing links and merge them back. It runs until
the frontier empties or the `max_fetches` budget is spent, gathering every match,
then fuses them via the schema's optional `merge_extractions` classmethod. Screen and
link-scorer share a cheap model; extraction uses a stronger one.

Key files: [extractor.py](agentic_web_extraction/extractor.py) (loop),
[frontier.py](agentic_web_extraction/frontier.py) (heap + visited set + PSL domain
compare), [normalize.py](agentic_web_extraction/normalize.py),
[providers/](agentic_web_extraction/providers/),
[result.py](agentic_web_extraction/result.py),
[config.py](agentic_web_extraction/config.py) (`AWE_*` settings).

## Conventions to respect

- **Schema-agnostic — no built-in domains.** The caller supplies the Pydantic schema,
  the NL criterion, and the seed URL. Don't add domain-specific defaults or classes.
  The schema must be a `type[BaseModel]`, so per-page multiplicity is a list field in a
  container schema (see [examples/grants.py](examples/grants.py)), not multiple objects
  per page.
- **Domain-agnostic normalization.** [normalize.py](agentic_web_extraction/normalize.py)
  ships **no** site-specific text munging. Cache-stability strippers are caller-supplied
  via `text_filters` (a `Sequence[Callable[[str], str]]`); the reference set lives in
  [examples/strippers.py](examples/strippers.py). Don't move site-specific filters into
  the library.
- **Single-knob traversal.** `max_fetches` (env `AWE_MAX_FETCHES`, default 10) is the
  primary lever. Don't add depth caps or per-link relevance thresholds without an
  explicit ask — LLM scoring is the policy. Deliberate exceptions, both single toggles
  off by default: `prefer_seed_domain` (soft off-domain disfavor expressed *to the LLM*,
  not a math penalty — nothing is excluded) and `stop_on_first_match` (`False` =
  gather-all-then-merge).
- **Uniform result shape.** `extract` always returns the same structure (`data`,
  `stopped_reason`, `pages_fetched`, `path`, `verdicts`, per-function token usage)
  whether it matched or exhausted budget. Plumbing this metadata is non-optional. See
  [result.py](agentic_web_extraction/result.py).
- **Logging: never a bare `print`.** All diagnostics go through `logsink.emit` → stderr
  (stdout is reserved for result JSON). A `log_file` path (env `AWE_LOG_FILE`, empty =
  off) also appends timestamped lines. See [logsink.py](agentic_web_extraction/logsink.py).
- **On-by-default LLM cache is generic.** Caching is on by default: the Extractor builds
  a `SqliteKVCache` at `AWE_LLM_CACHE` (`data/llm_cache.sqlite`) unless the caller passes
  their own `KVCache`, passes `cache=None` to disable, or the setting is empty. All of
  [cache.py](agentic_web_extraction/cache.py) stays domain-agnostic — values are opaque
  JSON round-tripped through the caller's schema. On an unchanged content hash it replays
  screen/extract/link-scores with zero LLM calls. **Merge is cached by the Extractor**
  (`_merge_cached`), not by `merge_extractions`: the merge key is derived from the
  contributing pages' page-cache keys, so the merge replays only when *every* source page
  hit the cache (unchanged). Don't push merge caching back into schemas' `merge_extractions`.
  `_merge` still offers `provider`/`cache` to `merge_extractions` by signature inspection —
  keep that mechanism, but the reference example no longer takes `cache`.
- **Don't fork CLI vs Python logic.** The CLI wires to the same `Extractor` the Python
  API exposes.

## Dependency gotchas

- `httpx` — plain client, **no HTTP-response cache** (no hishel). Fetching is cheap and
  the frontier never re-fetches a URL within a crawl, so an HTTP cache wasn't worth the
  memory/disk; the content-addressed LLM cache handles the expensive re-work instead.
- `tldextract` — PSL lookup for the domain comparison; constructed with
  `suffix_list_urls=()` to use the bundled offline snapshot (no runtime network fetch).
- `markitdown` (HTML→MD), `openai` (default provider, swappable via `AWE_PROVIDER`),
  `pydantic`/`pydantic-settings` (`AWE_*`, `OPENAI_*` env), `tenacity` (retries),
  `typer` (CLI; `--schema` = `import.path:ClassName`).

## CLI contract

```
awe extract --schema ./schemas.py:Opportunity --criteria "..." --seed-url https://... \
  [--max-fetches 10] [--stop-on-first-match | --gather-all-matches] \
  [--prefer-seed-domain | --no-prefer-seed-domain] [--log-file log.txt] [--no-cache]
```

`--criteria` accepts an inline string or `@path/to/file.txt`. `--schema` takes
`import.path:ClassName` or `path/file.py:ClassName`. `text_filters` are Python-API-only
(callables — not CLI-expressible).

## Layout

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` —
enforced by `[tool.uv.build-backend].module-root = ""`.
