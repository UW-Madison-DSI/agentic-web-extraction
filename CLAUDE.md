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

uv run pytest      # tests (offline: stub provider + stub web, no network/LLM)

uv run scripts/release.py [major|minor|patch]   # cut a release (default: patch)
```

Tests live in [tests/](tests/) and are deliberately network-free: [tests/conftest.py](tests/conftest.py)
supplies a `StubProvider` (screens everything in, scores every link 0.9) and a
`StubWeb` (url → html, plus a redirect map and a fetch log), so a test asserts on
*which pages the traversal chose to fetch*. Anything needing a real LLM or a real
site doesn't belong here.

## Architecture

A **best-first web traversal that ends in one consolidated extraction**: a frontier
of unvisited links where the LLM's relevance scoring is the *only* navigation policy.
One or more seed URLs are pushed into a single shared frontier at a sentinel score
(`float("inf")`) so every seed is fetched first; the budget is `max_fetches` *per
seed*. The loop processes the frontier in **parallel waves** — pop the top-N links
(`max_workers`), then concurrently fetch (with non-2xx recovery via
[fallback.py](agentic_web_extraction/fallback.py)) → normalize (HTML→Markdown) →
pre-screen → score outgoing links per page — folding results back on the main thread
(which owns the frontier; workers never mutate it). Pages that pass screening have
their markdown **collected**, not extracted per-page. When the frontier empties or the
budget is spent, the collected pages are concatenated and — if over
`max_context_tokens`, or always under `always_summarize` — summarized down (criteria-aware map-reduce on the screen model), then a **single**
extraction runs over the whole thing. No `merge_extractions`, no dedup. Screen, link-
scorer, and summarizer share a cheap model; extraction uses a stronger one.

Two hard controls sit on top of that policy, both off/generic by default:
`allowed_domains` (a default-deny set of registrable domains, enforced at the
`frontier.push` call site — off-boundary links are dropped and logged `[blocked]`,
never fetched) and `respect_robots` (per-origin robots.txt, checked in the worker
*before* the fetch). Plus `user_agent`, so the traffic is attributable.

Key files: [extractor.py](agentic_web_extraction/extractor.py) (wave loop + consolidate),
[summarize.py](agentic_web_extraction/summarize.py) (fit-or-summarize),
[schema_outline.py](agentic_web_extraction/schema_outline.py) (schema → compact prompt outline),
[tokens.py](agentic_web_extraction/tokens.py) (tiktoken counting/splitting),
[frontier.py](agentic_web_extraction/frontier.py) (heap + visited set + snapshot + PSL
domain compare + `domain_of` allow keys), [fetch.py](agentic_web_extraction/fetch.py) (httpx + status guard + UA),
[robots.py](agentic_web_extraction/robots.py) (opt-in robots.txt policy),
[fallback.py](agentic_web_extraction/fallback.py) (jina/wayback recovery),
[normalize.py](agentic_web_extraction/normalize.py),
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
- **The crawl boundary is filtered at the frontier, never at the transport.** The
  hard limit is `Extractor(allowed_domains=...)` (default `None` = unrestricted, so
  upgrading changes nothing), enforced at the one `frontier.push` call site in the
  fold loop and nowhere else. Do **not** move it into `_process_page`: worker-side
  filtering would bake the current allowed set into the `PAGE` cache's stored
  `link_scores`, so a later run with a different boundary would replay the old one.
  That is *also* the answer to "off-boundary links are re-scored on every page, which
  costs tokens" — true, and the fix is not free: skipping them means filtering the
  scorer's input, which is what gets cached. Buying it back properly needs the
  boundary in the `PAGE` key (the `seeddom=` segment is the precedent), which costs
  cross-crawl sharing precisely where it pays most — the same portal page reached from
  55 different seeds would no longer share one entry. Left as-is deliberately; only
  the *log* is deduped (see below). Do **not** move it into `fetch.py` either: httpx
  follows redirects inside a single fetch, and filtering requests would break every
  site that has rebranded or moved host — that is what `allow_seed_redirect_domains`
  is for, and it is **off** by default: whoever controls a seed's DNS decides where it
  lands, so it is the one path by which someone other than the caller can widen the
  set. Opted in, it fires only for a *seed* (a non-seed link that redirects
  off-boundary is read but expands no further) and only when the landing page returned
  readable content, so a parked domain's error page can't nominate itself. Widening
  runs as a pre-pass over the whole wave (`_widen_for_seed_redirects`) before any link
  is gated: all seeds share `SEED_SCORE` and arrive together, so folding them one at a
  time made the boundary depend on which worker finished first. Matching goes through
  `frontier.domain_of` (PSL via tldextract, bare host as the fallback key for
  `localhost`/IPs) — don't write new host matching, and don't add a blocklist or
  threat-intel feed: default-deny already covers everything a feed would name, with no
  feed to keep fresh and no network dependency. Every dropped link gets a `[blocked]`
  line — once per crawl per URL, since a site-wide footer link is re-offered by every
  page and one line per page buries the log without adding a fact; the dedup set is
  never consulted as policy, so a mid-crawl widening still takes effect. Seed domains
  join the set automatically (a caller passing a seed is asking for that domain), so
  `[]` means "the seeds' sites only" and callers list only the extras. Deliberately
  **not** an `AWE_*` setting: the in-scope domains depend on a given crawl's seeds,
  not on the environment.
- **robots.txt is opt-in, per-origin, and fails open.**
  [robots.py](agentic_web_extraction/robots.py) is checked inside the worker before
  the fetch (so a disallowed URL costs no request, no budget slot, no LLM call) and
  again on the *resolved* URL when the fetch redirected — httpx follows redirects
  inside one call, so a redirector on an allowed path is otherwise a hole straight
  through the check, cross-origin included; the second request is already spent, but
  the body is discarded unread rather than screened and pooled. Running in the worker
  is safe because the policy owns its cache + lock and touches no traversal state — the
  frontier rule still holds. A skip returns `_PageOutcome(policy_skipped=True)`, which
  the fold loop keeps out of `path` (nothing was retrieved); the log line is the record,
  so it must name the URL — eight workers interleave their output, and a line that
  identifies only the agent is unattributable. Failure to *obtain* robots.txt — 404, 401/403, 5xx, timeout — is treated as
  unrestricted, the opposite of RFC 9309's suggestion, because an origin's brief 500
  (or an edge rule that blocks the crawler's robots.txt too) would otherwise empty an
  authorized crawl behind a line that reads like the site's own policy. Keep that
  documented wherever it moves. `AWE_ROBOTS_OVERRIDES` exempts domains; it is *not* a
  boundary — the two compose (boundary = where, robots = what).
- **Attribution: a process default, overridden per request.** `AWE_USER_AGENT` /
  `Extractor(user_agent=...)` feeds `fetch.configure()` and `fallback.configure()`
  from `Extractor.__init__`, following the `logsink.configure` precedent — but that
  sets only the *default*, because both http clients are process-wide singletons and
  `settings.user_agent` has a non-empty default: a second Extractor built without
  `user_agent=` would otherwise revert the first one's in-flight traffic to the generic
  library string, and leave the agent sent diverging from the agent its robots rules
  are evaluated against. So every request also carries the initiating Extractor's own
  string — `fetch(url, user_agent=...)` → `_send`, `fallback.recover(url,
  user_agent=...)` → both routes, and `RobotsPolicy`'s own `robots.txt` fetch. Keep new
  outbound calls on that path; a request that falls back to the client default is one
  whose attribution depends on construction order. Recovery requests carry the *same*
  string as origin ones (which route served a page is already in `FetchedPage.via`).
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
  `extraction_input_tokens`, `summarized`, `fallbacks_used`, per-function token usage)
  whether it matched or exhausted budget. Plumbing this metadata is non-optional. See
  [result.py](agentic_web_extraction/result.py).
- **Frontier is single-threaded; workers are pure.** Only the main thread pops/pushes/
  marks the `Frontier`. `_process_page` runs on pool threads and returns a `_PageOutcome`;
  it reads a `frontier.snapshot()` (frozen set) to pre-filter links but never mutates
  shared state. Provider usage accumulation is lock-guarded for the same reason. Keep
  new per-page work inside the worker and new frontier work in the fold loop.
- **Non-2xx is never content; recovery is retrieval-only.** [fetch.py](agentic_web_extraction/fetch.py)
  classifies on Content-Type, so an edge-CDN "Access Denied" interstitial or a themed
  404 would otherwise be screened and extracted as if it were the page (guaranteed into
  the extraction under `seed_is_content`). The status guard drops anything outside 2xx;
  [fallback.py](agentic_web_extraction/fallback.py) then tries to turn the hole back into
  content over the routes named by `AWE_FETCH_FALLBACKS` (`jina` — `r.jina.ai` renders
  live and reads PDFs, requesting the full DOM by default so link extraction behaves as
  on a direct fetch; `wayback` — newest Archive capture, `id_`-unrewritten, staleness
  bounded by `AWE_WAYBACK_MAX_AGE_DAYS`). Keep that module opinionated about *retrieval
  only* — content selection, normalization, and link policy stay where they live, and
  nothing there may know about a particular site; the chain is driven by response status
  alone. Recovered bytes are returned under the **caller's** URL, never the proxy/archive
  address, so `path`, the `--- SOURCE:` markers, and caller citations stay canonical;
  the route lands in `FetchedPage.via` → `ExtractionResult.fallbacks_used`. `fallback.py`
  must not import `fetch.py` (fetch imports it, and classification/PDF policy belong to
  the fetch path). Both routes disclose the crawled URL to a third party — empty
  `AWE_FETCH_FALLBACKS` keeps the guard and disables recovery.
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
- **The git tag is the release; nothing else sets the version.**
  [scripts/release.py](scripts/release.py) is the only thing that writes
  `version` in `pyproject.toml` — never hand-edit it, because the value has to
  match the `vX.Y.Z` tag. The script is precondition-heavy on purpose: `main`
  only, clean tree, exactly level with the remote, **and a non-empty
  `## Unreleased` section in [CHANGELOG.md](CHANGELOG.md)** — the point is that a release
  can't be cut from a state you can't reconstruct from the tag. Branch and tag go
  up with `git push --atomic` so a half-push can't leave a tag pointing at an
  unpushed commit, and **any** failure after the bump rolls back the version, the
  changelog, the commit and the tag together — a bumped `pyproject.toml` left on
  disk would silently become the base of the next run, permanently skipping that
  number.
  Release notes are part of the release commit, not an afterthought: `## Unreleased`
  is renamed to `## vX.Y.Z — <date>` and committed *with* the bump, so the tag
  carries its own notes and a consumer who installs `@vX.Y.Z` has them on disk. That
  is why the changelog is a file first and a GitHub Release second — publishing to
  the Releases page (`gh release create`, notes piped on stdin) happens *after* the
  atomic push and is the one step **outside** the rollback: the tag is public by
  then, so it can't be un-released, and re-running the script would cut a new
  version rather than retry the step. So that failure prints the manual `gh` command
  and exits non-zero without touching git. A missing/unauthenticated `gh` is a
  *skip*, not a failure — the tag is the release. Don't add a second source of
  release-note truth (hand-written Release bodies, generated notes); one text, two
  places.
  Lockfile handling is conditional on `git ls-files`, not assumed: `uv.lock` is
  gitignored here, and `git add` on an ignored path is a hard error, so the
  refresh is skipped unless the lockfile is actually tracked (where it must be
  refreshed in the same commit, since uv records the project version in it too). It's PEP 723 like
  [scripts/adopters.py](scripts/adopters.py), but *not* stdlib-only — `typer`/`rich`
  come from its own header, so it stays out of the project's dependency surface.
  No tag-triggered workflow exists yet; the tag is the pin consumers install from
  (`git+https://...@vX.Y.Z`), and the GitHub Release is published by the script
  itself, not by CI. If a workflow is ever added, it hangs off
  `push: tags: ["v*"]` — and it must not also create the Release, or the two will
  race on the same tag.
- **The README `<!-- adopters:start -->` block is generated.** Never hand-edit it;
  [scripts/adopters.py](scripts/adopters.py) (weekly, via
  [.github/workflows/adopters.yml](.github/workflows/adopters.yml)) overwrites it. That
  script is deliberately stdlib-only PEP 723 and must hard-fail — a missing/under-scoped
  token or any API error exits non-zero with the README untouched, because a silent zero
  is indistinguishable from real disadoption. No public-only fallback, and don't go
  looking for a download metric to "improve" it: GitHub Packages exposes none, and PyPI
  counts can't attribute to a repo. Only a *dependency manifest* declaration counts;
  imports and `awe extract` invocations are reported in the job summary, never counted.
  **Counting must not go back through the Code Search API** — code search had only 23 of
  the org's 55 Python repos indexed and missed a real adopter, so discovery enumerates
  `GET /orgs/{org}/repos` and walks each repo's `git/trees/{branch}?recursive=1`.
  Code search is retained *only* for the never-counted weak signal. Anything meaning the
  count is a floor (truncated tree, unexhausted repo pages) is a hard error, and every
  run prints `Examined N/M org repos` so an incomplete sweep is visible.

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
  [--prefer-seed-domain | --no-prefer-seed-domain] \
  [--allowed-domain example.org ...] [--allow-seed-redirect-domains] \
  [--user-agent "name/1.0 (+contact-url)"] \
  [--respect-robots | --no-respect-robots] [--robots-override example.org ...] \
  [--log-file log.txt] [--no-cache]
```

`--criteria` accepts an inline string or `@path/to/file.txt`. `--schema` takes
`import.path:ClassName` or `path/file.py:ClassName`. `--seed-url` is repeatable (pools
seeds into one extraction; budget is per seed). `--allowed-domain` and
`--robots-override` are repeatable too; **no** `--allowed-domain` means no boundary,
so the CLI normalizes Click's empty tuple to `None` (an empty *set* would mean the
opposite thing — seeds only). `text_filters` are Python-API-only (callables — not
CLI-expressible).

## Layout

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` —
enforced by `[tool.uv.build-backend].module-root = ""`. `tests/` is a package
(`__init__.py`) so test modules can `from .conftest import ...`; it sits outside the
built wheel.
