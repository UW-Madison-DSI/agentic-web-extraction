# CLAUDE.md

Guidance for Claude Code working in this repository.

## User preferences

- Keep the README up to date whenever you finish a feature.

## Commands

Managed with **uv** (Python ≥3.13, build backend `uv_build`):

```bash
uv sync            # install deps (incl. dev group)
uv run awe         # CLI entry point (`awe extract ...`, `awe schema`)
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
site doesn't belong here — including *indirectly*: `tiktoken` downloads its
encoding table on first use, so anything touching the fit-or-summarize path (or
`OpenAIProvider.score_links`, whose output cap counts URL tokens) takes
the `fake_tokens` fixture (whitespace words for tokens) rather than letting a real
encoding load.

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
*before* the fetch). Plus `user_agent`, so the traffic is attributable, and
per-domain pacing (`AWE_REQUEST_DELAY`, **on**), so it is bearable.

Key files: [extractor.py](agentic_web_extraction/extractor.py) (wave loop + consolidate),
[summarize.py](agentic_web_extraction/summarize.py) (fit-or-summarize),
[schema_outline.py](agentic_web_extraction/schema_outline.py) (schema → compact prompt outline),
[tokens.py](agentic_web_extraction/tokens.py) (tiktoken counting/splitting),
[frontier.py](agentic_web_extraction/frontier.py) (heap + visited set + snapshot + PSL
domain compare + `domain_of` allow keys), [fetch.py](agentic_web_extraction/fetch.py) (httpx + status
guard + transport-failure recovery + per-domain transport memo + UA),
[robots.py](agentic_web_extraction/robots.py) (opt-in robots.txt policy + `Crawl-delay`),
[sitemap.py](agentic_web_extraction/sitemap.py) (opt-in sitemap discovery, hardened XML),
[fallback.py](agentic_web_extraction/fallback.py) (impersonate/jina/wayback recovery),
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
  frontier rule still holds. A `200` is validated before it is parsed
  (`looks_like_policy`): a bot-sensor page served at `/robots.txt` parses to *zero
  rules*, i.e. blanket consent, at exactly the sites likeliest to have meant the
  opposite, so a non-text content type or a body opening with markup counts as
  *unavailable* (still fails open, but the log line says the rules were never obtained).
  A policy the default client can't obtain is retried over the escalated transport when
  `AWE_IMPERSONATE` covers the host — via `fallback.impersonate`, that route **only**,
  never `recover()`: rules must come from the origin, not from a reader's rendering or a
  years-old archive capture of somebody's policy. Otherwise a deployment reads a site's
  pages with a browser fingerprint and its policy over the channel the site blocks, then
  proceeds unrestricted every time. A skip returns `_PageOutcome(policy_skipped=True)`, which
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
  `max_output_tokens` (env `AWE_MAX_OUTPUT_TOKENS`, default `0` = no cap, the
  historical bytes-on-the-wire behavior) caps that one call's *output*. It exists
  because a JSON grammar permits arbitrary whitespace between tokens, so `\n  ` is
  always a legal next token and schema-guided decoding cannot break a repetition loop
  the way it would for a malformed key: uncapped, the endpoint's own output limit can
  outlast the client read timeout, so the call surfaces as a timeout, the SDK silently
  re-sends it, and one extraction burns minutes without a recoverable error reaching
  the caller. Capped, it becomes a `ValidationError` (truncated document) or the
  `AssertionError` in `extract()` (no parsed object) — catchable and cheap to re-roll.
  Note this is the Responses API, which does *not* raise `LengthFinishReasonError`.
  Deliberately not in any cache key and not a CLI flag: it is a per-deployment
  backstop, and a value below the largest legitimate extraction turns a working call
  into a failing one.
  The screen model's two structured calls have their own caps, **on** by default
  because their output size is known in advance, which the extraction's is not:
  `score_links` = `reasoning_output_tokens` (4000) + the links' URLs in tokens +
  `score_output_tokens_per_link` (100) × links — the URLs are counted because the
  scorer echoes each one exactly — and `screen` = `reasoning_output_tokens` +
  `screen_output_tokens` (1000); `<= 0` on either per-call knob sends no cap there.
  Because a cap that grows with the input can exceed an endpoint's own limit (and
  nothing knows that limit), `screen_model_max_output_tokens` (0 = unknown) clamps
  both, and a 400/422 refusing the cap is retried **once** uncapped and logged — so
  an endpoint limit below the cap can never break a call that worked without one.
  That retry is for these two computed caps only: `extract()`'s cap is the caller's
  deliberate backstop, so a refused one surfaces as an error. What the caps *can*
  break is a screen-model call that legitimately reasons past
  `reasoning_output_tokens`; a failed screen also skips that page's link scoring,
  so the answer to that is a larger allowance, not removing the cap.
  None of these is in any cache key. All three structured calls go through
  `_structured_call`, which keeps those rules in one place: it sends via
  `with_raw_response` so tokens billed for a mid-JSON cutoff (where the SDK's parse
  raises before a response object exists) still reach `usage_by_function`, and it
  raises on a response with no parsed object; the worker's existing `stage_error`
  path keeps such a page out of the `PAGE` cache.
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
  `stopped_reason`, `pages_fetched`, `path`, `verdicts`, `protocol`, `content_tokens`,
  `extraction_input_tokens`, `summarized`, `fallbacks_used`, plus per-function token
  usage in `usage_by_function`/`function_model`) whether it matched or exhausted
  budget. Plumbing this metadata is non-optional. `protocol` names the provider
  adapter / wire protocol, **not** the model vendor — an OpenAI-compatible endpoint
  may serve anything — so cost is only reconstructable by pairing it with
  `function_model`; keep the two together. See
  [result.py](agentic_web_extraction/result.py).
- **Frontier is single-threaded; workers are pure.** Only the main thread pops/pushes/
  marks the `Frontier`. `_process_page` runs on pool threads and returns a `_PageOutcome`;
  it reads a `frontier.snapshot()` (frozen set) to pre-filter links but never mutates
  shared state. Provider usage accumulation is lock-guarded for the same reason. Keep
  new per-page work inside the worker and new frontier work in the fold loop.
- **Nothing but a 2xx body is content; recovery is retrieval-only.** [fetch.py](agentic_web_extraction/fetch.py)
  classifies on Content-Type, so an edge-CDN "Access Denied" interstitial or a themed
  404 would otherwise be screened and extracted as if it were the page (guaranteed into
  the extraction under `seed_is_content`). The status guard drops anything outside 2xx;
  [fallback.py](agentic_web_extraction/fallback.py) then tries to turn the hole back into
  content over the routes named by `AWE_FETCH_FALLBACKS` (`impersonate` — re-request the
  origin through curl_cffi's browser TLS/HTTP fingerprint, see below; `jina` — `r.jina.ai`
  renders live and reads PDFs, requesting the full DOM by default so link extraction
  behaves as on a direct fetch; `wayback` — newest Archive capture, `id_`-unrewritten,
  staleness bounded by `AWE_WAYBACK_MAX_AGE_DAYS`). The chain is driven by **failure to
  obtain content**, not by response status: `fetch` calls `_recover` from the status
  guard *and* from its bare-`Exception` transport handler, because an origin that
  tarpits a non-browser client denies us the page exactly as completely as one that
  answers 403 — handling only the second meant the *less* polite refusal was the one
  that skipped recovery. Don't re-narrow that to a status check. Keep the module
  opinionated about *retrieval only* — content selection, normalization, and link policy
  stay where they live, nothing there may know about a particular site, and a recovered
  page is still adjudicated by the boundary and by robots exactly as a direct fetch is.
  Recovered bytes are returned under the **caller's** URL, never the proxy/archive
  address, so `path`, the `--- SOURCE:` markers, and caller citations stay canonical;
  the route lands in `FetchedPage.via` → `ExtractionResult.fallbacks_used`. `fallback.py`
  must not import `fetch.py` (fetch imports it, and classification/PDF policy belong to
  the fetch path). `jina`/`wayback` disclose the crawled URL to a third party;
  `impersonate` talks to the origin only — empty `AWE_FETCH_FALLBACKS` keeps the guard
  and disables recovery entirely. `AWE_FETCH_ATTEMPTS` (default 3) bounds origin retries,
  with read timeouts capped at 2 attempts however high it is set: a tarpit is
  deterministic, so the later attempts spend the read timeout again to be refused
  identically while holding a worker slot the wave waits on.
  The same reasoning across *pages* is the transport memo in `fetch.py`:
  `AWE_TRANSPORT_MEMO_FAILURES` (default 2, 0 = off) unanswered fetches write the
  registrable domain off for the process, and later URLs there skip `_send` for
  `_recover`. It lives in `fetch.py`, not `fallback.py` — it is a fact about the
  *default transport*, and `fallback.py` must stay ignorant of which host is being
  asked. Every rule around it exists because a wrong write-off routes a *healthy*
  site through the recovery chain, so keep all four: only `counts_as_silence` (httpx
  timeouts + network errors) latches, because the bare-`Exception` handler also
  catches per-URL failures (`UnicodeEncodeError` on a malformed `Location`,
  `InvalidURL`) that say nothing about the host; **any** response clears it
  (`_note_response`, after the try/except, which the `HTTPStatusError` branch reaches
  too) since refusing out loud is answering in one round-trip; a domain that has ever
  answered joins `_answered` and is never written off again, because eight interleaved
  workers make a count unable to distinguish "silent" from "slow on two big pages";
  and blame goes to `_failed_host` (the failing request's URL, not the first hop of a
  redirect the client followed inside one call). The skip is gated on
  `fallback.configured_routes()` being non-empty, but that only knows route *names* —
  `impersonate` declines on its own for an unset target, an out-of-scope host, or a
  missing curl_cffi — so when `_recover` returns nothing the fetch **falls through to
  `_send` anyway**: the memo is an optimization and must never be why a page is lost,
  and that fetch succeeding is the only way back. `Extractor.extract` calls
  `reset_transport_memo()` per crawl (the memo is evidence gathered during a crawl,
  not a standing fact — a later crawl may run under a different UA or with
  impersonation newly enabled), and `tests/conftest.py` resets it around every test.
  Keyed through `frontier.domain_of` like every other host comparison, lock-guarded
  like `_client`. Deliberately not wired into `robots.py`: that fetch happens once per
  origin, is cached, and must come from the origin, which is why it escalates to
  `fallback.impersonate` on its own instead.
  A route wins only if what it returned is plausibly the page: `recover()` measures
  `visible_text` (regex-stripped markup/script — not markitdown, which would buy
  parser fidelity to decide 554 vs 147,000) against
  `AWE_MIN_RECOVERED_TEXT_CHARS` (default 200, 0 = off) and treats a thinner body as
  a decline, so a client-rendered shell from a raw-HTML route falls through to a
  rendering one. Keep the check in `recover()`, route-agnostic — the routes stay dumb
  — and keep it a *preference*: the fullest sub-threshold body is returned when no
  route clears the bar, so the threshold can only change which body comes back, never
  whether one does. PDFs are exempt (their content is in `raw_bytes`; `text` is empty
  by contract). Its known cost is that a *genuinely* short page falls through too, so
  a stub the origin-only `impersonate` route served reaches `jina`/`wayback`; the
  answer is a lower threshold, not scoping the fall-through to non-disclosing routes
  (impersonate-shell → jina-render is the exact case it exists for). `_has_dom` is a
  different mechanism and stays: a within-route retry for jina's html mode, not
  cross-route fall-through.
- **Impersonation is two switches, both off, and neither is a transport swap.**
  `AWE_IMPERSONATE` (a curl_cffi target) buys a browser *fingerprint* while still
  sending the crawl's own attributable User-Agent — enough for CDNs that refuse on the
  shape of the handshake. `AWE_IMPERSONATE_BROWSER_UA` is separate because it drops
  attribution: sites that want fingerprint and identity to agree reject even a real
  Chrome UA with a contact URL appended, so reaching them means a full masquerade,
  usually at a site simultaneously refusing to serve its robots.txt. That is an
  institutional call, so it must stay opt-in, typed out, and scopeable
  (`AWE_IMPERSONATE_DOMAINS`, matched through `frontier.domain_of` like everything
  else). It is deliberately a *route in the existing chain*, not a pluggable primary
  transport: as a route it inherits `recover()`'s ordering, the `via` provenance, the
  `[fallback:*]` log convention and `configured_routes()`, escalates only after the
  honest path has actually been refused, and duplicates none of fetch's retry/status/
  classification logic. curl_cffi is an **optional** extra imported inside
  `_new_session`, so a base install declines the route with a log line instead of
  failing at import. Its sessions wrap a libcurl handle and are **not** thread-safe:
  one per (thread, target) in a `threading.local`, never the module-level `_client`
  singleton pattern the httpx clients use.
- **Politeness is transport state, and both of its knobs are ON.**
  `AWE_REQUEST_DELAY` (0.5s) spaces the *starts* of two fetches to one registrable
  domain; `AWE_MAX_PER_DOMAIN` (4, half the default `max_workers`, 0 = off) caps
  how many are in flight there — a ceiling rather than a schedule, binding only
  when an origin is slow enough that five requests overlap. Both
  live in [fetch.py](agentic_web_extraction/fetch.py) beside the transport memo
  for the same reason the memo does — they are facts about the *default
  transport* — and key through `frontier.domain_of` like every other host
  comparison. `fetch()` reads them from the `Settings` it is **handed**, not from
  `get_settings()`: without that, `Extractor(settings=...)` — which is what the
  CLI's settings-only flags are built on — reaches nothing inside `fetch`. The
  memo threshold and the attempt budget stay global, deliberately: the memo is
  shared across every Extractor in the process, and tenacity calls the attempt
  predicate from a retry hook that cannot see the call's arguments. The default is on because being impolite is a defect whose cost
  lands on somebody else, and because the crawl boundary *causes* the problem it
  fixes: keeping a traversal on one site means `max_workers` workers concentrate
  on a single origin, so the knob that makes the crawl fast was also what made it
  rude, with nothing in between. Three rules to keep. Slots are **reserved under
  the lock and slept for outside it**: a thread holding the lock while sleeping
  would serialize every domain behind one, and threads that each *read* "next
  allowed" and slept toward the same instant would all wake together — pacing
  that reads correctly in a log and changes nothing at the origin. The gate wraps
  the whole retry sequence, not each attempt, since tenacity already backs off
  ≥1s. And it gates the **origin fetch only**, never the recovery routes: `jina`
  and `wayback` talk to a third party whose rate limit has nothing to do with the
  crawled origin's, and pacing them under the origin's key would throttle the
  wrong host. An origin's `Crawl-delay` is read off the parser `robots.allows`
  already cached and passed down as `fetch(min_delay=...)` — larger of the two
  wins — so `fetch.py` never learns what robots.txt is. A `robots_overrides`
  domain is exempt from the delay too: honouring half of a policy the operator
  has been authorized to set aside is just a slower crawl.
- **A filter that runs in the worker must be a pure function of the URL.** The
  worker's output is what the `PAGE` cache stores, so anything varying with
  configuration gets baked into an entry and replayed under a different
  configuration later — the same argument that keeps the crawl boundary at
  `frontier.push`. That is why `normalize.extract_links` drops extensions
  `fetch._classify` could never accept as content (`.zip`, `.jpg`, `.css`,
  `.docx`) but **not** `.pdf`, and deliberately does not consult `follow_pdf`:
  the filter is invariant, so caching it is safe, and `fetch` skips an unwanted
  PDF cheaply anyway. It costs nothing in outcome — those links were fetched,
  classified `skipped` and dropped after the scorer had already been billed — so
  unlike `max_links_per_page` (the one knob here left off) it is always on. That
  cap,
  being configuration, *is* in the `PAGE` key (a `links=N` segment, added only
  when set) — and it is applied in the fold path **after** the `known` filter,
  never inside `extract_links`: capping the raw list hands the whole allowance to
  the site-wide nav at the top of every page, so `fresh` comes back empty and the
  frontier starves after the seed.
- **DOM filtering governs the extraction input, never link discovery.**
  `AWE_MAIN_CONTENT_ONLY` (**on** by default: site chrome is most of the DOM and
  none of it answers a criterion, so leaving it in means every screen, summarize
  and extract call pays for the same masthead again — it *is* lossy in the
  caller's results, which is what the off switch is for, and flipping it is
  cache-safe because the page-cache key hashes the filtered markdown) strips
  `script`/`style`/`noscript`/`template`, plus
  `header`/`footer`/`nav`/`aside` elements **not inside a `main` or `article`** —
  an article's own `<header>` holds its title and date, which is exactly what a
  schema asks for, so the naive "remove every header" rule loses data on the
  pages worth extracting from. This is *not* the site-specific munging this
  library bans from `normalize.py`: it names only standard sectioning elements.
  Link discovery reads the *unfiltered* `page.text`, so filtering a nav out of
  the markdown never hides the links in it from the scorer — keep that
  separation. bs4 is pinned to the stdlib `html.parser`, never lxml-if-available:
  which parser is used changes the emitted markup, which changes the content hash
  every cache key is built on, and a cache that misses because a wheel is present
  on one machine and not another is worse than a slower parse.
- **A thin 200 is a failure to obtain content.** `AWE_MIN_PAGE_TEXT_CHARS` (on
  at 200, matching `min_recovered_text_chars` so one number means "is this the
  page" on both sides of the chain) sends a fetched HTML page carrying too little
  visible text through the recovery chain — the single-page-app case, which was the one
  refusal with no trigger and no trace: past the status guard, normalized to
  nothing, screened out as irrelevant, logged as an ordinary page. Consistent
  with the doctrine that the chain is driven by failure to obtain content rather
  than by status. Two things keep it safe and must stay: it reuses
  `fallback.visible_text` (the same measure `recover` uses, so there is one
  definition of "is this the page"), and the recovered body must be **fuller than
  the origin's** to win — `recover` compares routes against each other, never
  against the page already in hand, so its "fullest sub-threshold body" can be
  worse than what the origin served. Its cost is requests, at a third party, on a
  page the origin already answered: a genuinely short page is indistinguishable
  from a shell by character count, so a stub falls through to jina/wayback too.
  Lower the threshold rather than zeroing it if that trade is wrong for a
  deployment.
- **Sitemap URLs go through the frontier, never around it.**
  [sitemap.py](agentic_web_extraction/sitemap.py) (`AWE_USE_SITEMAP`, on by
  default — the URLs are ranked, not privileged, so the worst case is candidates
  the scorer never pops, and the extra requests are bounded and go to the origin
  being crawled) is frontier *seeding*, not a second navigation policy: discovered
  URLs are handed to the same `score_links`, gated by the same `_queue_link`, and checked against
  the same robots policy. Pushing a few hundred unranked URLs at a fixed score
  would drown the relevance ordering that is the entire policy. It runs on the
  main thread (which owns the frontier) *after* `allowed` is built, so a site
  cannot nominate a domain the caller refused just by listing it; it consumes no
  fetch budget (budget counts readable pages); and it is skipped under
  `seed_is_content`, which asserts the seeds already are the content. The XML is
  written by the party being crawled: a body declaring a DTD or an entity is
  refused *unparsed* (ElementTree really does expand internal general entities),
  bodies are size-capped before parsing, only `http`/`https` locations are
  returned, and sitemap *documents* are restricted to the seed's own registrable
  domain — both sources of locations (robots.txt `Sitemap:` lines and an index's
  `<loc>`s) are attacker-controlled, and the crawl boundary does not cover them
  because it gates links entering the frontier while these are fetched before
  that. Its fetches go through the same pace gate as pages. Don't relax those into a parser configuration — a refusal of the
  construct cannot be reasoned around, a parser setting has to be re-verified on
  every upgrade.
- **A setting defaults ON when the typical crawl is better off and one flag
  reverts it; OFF when no single value is right for the typical crawl, or when a
  wrong value decides *which pages exist* rather than what they contain.** On:
  `request_delay`/`max_per_domain` (the cost is ours, the benefit is somebody
  else's), the extension filter (no outcome changes at all), `main_content_only`
  (chrome is most of the DOM and answers no criterion), `min_page_text_chars` (a
  client-rendered shell is otherwise a silent no-op), `use_sitemap` (its URLs are
  scored like any other link), `score_output_tokens_per_link`/
  `screen_output_tokens` (the output size is known, and uncapped a runaway call
  burns 13–22 minutes). Off:
  `max_links_per_page`, alone — truncation keeps document order, so a cap spends
  the allowance on the site-wide nav and drops the in-content links underneath
  it, which is not a thinner frontier but the wrong one. Four of the on defaults
  do cost the caller something (`main_content_only` is lossy;
  `min_page_text_chars` and `use_sitemap` spend requests;
  the screen-model caps fail a call that legitimately needs more than the cap):
  that is the trade, so keep it stated at the setting and
  keep the one flag that turns each off working.
- **Logging: never a bare `print`.** All diagnostics go through `logsink.emit` → stderr
  (stdout is reserved for result JSON). A `log_file` path (env `AWE_LOG_FILE`, empty =
  off) also appends timestamped lines, and `Extractor(on_event=...)` subscribes a
  callable to the same stream for the duration of `extract`. Lines carry a
  bracketed `[tag]` prefix by convention and `Event.kind` is derived from it, so
  keep emitting them that way — best-effort, not a stable API, which is why the
  raw message travels alongside. Subscribers are notified **outside** `logsink`'s
  lock (it is not reentrant, and a subscriber that logs — forwarding to another
  logger is half the point — would deadlock the crawl), while the file write
  stays inside it (concurrent emits would interleave half-lines). A subscriber
  that raises is swallowed: a progress display must not cost a crawl that is
  otherwise working. See [logsink.py](agentic_web_extraction/logsink.py).
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
- `curl_cffi` — **optional** (`[project.optional-dependencies] impersonate`), imported
  inside `fallback._new_session` so a base install never needs the wheel. Ships a
  bundled libcurl-impersonate binary, so a deployment image (glibc vs musl) needs its
  own wheel check. Not in the dev group either: the tests drive the route through a fake
  session and skip the one import-dependent case, so `uv run pytest` passes without it.
- `beautifulsoup4` — DOM filtering in `normalize.strip_boilerplate`. Already arrived
  transitively via markitdown; now declared, since depending on a transitive dependency
  is depending on somebody else's dependency choices. **Pinned to the stdlib
  `html.parser`** — see the DOM-filtering convention above on why the parser choice is
  a cache-stability question.
- `xml.etree.ElementTree` (stdlib) — sitemap parsing. It **does** expand internal
  general entities, so `sitemap.py` refuses a body declaring a DTD or an entity before
  parsing rather than relying on parser configuration.
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
awe schema     # JSON Schema of every AWE_* setting (names, types, defaults, env)

awe extract --schema ./schemas.py:Opportunities --criteria "..." \
  --seed-url https://... [--seed-url https://... ...] \
  [--max-fetches 10] [--max-context-tokens 128000] [--max-workers 8] \
  [--request-delay 0.5] [--max-per-domain 0] \
  [--main-content-only | --no-main-content-only] [--max-links-per-page 0] \
  [--min-page-text-chars 0] [--use-sitemap | --no-use-sitemap] \
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
