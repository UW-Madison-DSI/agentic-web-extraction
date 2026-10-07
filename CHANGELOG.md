# Changelog

Write each change under `## Unreleased` as you make it.
[scripts/release.py](scripts/release.py) renames that heading to `## vX.Y.Z — <date>`
in the same commit as the version bump, then publishes the section as the GitHub
Release for the tag. An empty `## Unreleased` aborts the release.

## Unreleased

- **Output caps on the screen model's calls, on by default.** `score_links` is
  capped at `4000 + the links' URLs in tokens + 100 × links` output tokens and
  `screen` at `4000 + 1000`. Both calls can fall into the same whitespace loop that
  `AWE_MAX_OUTPUT_TOKENS` guards against on extraction. Uncapped, such a call ran to
  the endpoint limit, outlasted the 600 s read timeout, was re-sent by the SDK, and
  failed anyway as a `ValidationError` 13–22 minutes later. One crawl took 3 h 48 min
  instead of about 80 min ([#6](https://github.com/UW-Madison-DSI/agentic-web-extraction/issues/6)).
  Both outputs have a known size, so a capped runaway now fails in a minute or two
  with the same result: that page goes unscreened or its links unscored.
  - Settings: `AWE_SCORE_OUTPUT_TOKENS_PER_LINK` (default `100`) and
    `AWE_SCREEN_OUTPUT_TOKENS` (default `1000`), each `<= 0` = no cap on that call;
    `AWE_REASONING_OUTPUT_TOKENS` (default `4000`), the reasoning allowance in both.
  - The scorer's cap counts each URL's tokens, because the scorer echoes every URL
    exactly and a page of long URLs needs more room than a page of short ones.
  - `AWE_SCREEN_MODEL_MAX_OUTPUT_TOKENS` (default `0` = unknown) clamps both caps to
    the endpoint's limit. A 400 that refuses one of these caps (an over-limit
    `max_output_tokens`, or a vLLM-style "maximum context length") is retried once
    without a cap and logged, so a call that worked before the caps still works.
    An explicit `AWE_MAX_OUTPUT_TOKENS` on extraction is never dropped that way.
  - Tokens billed for a call the cap cut off mid-JSON are now counted in
    `usage_by_function` (the SDK's parse used to raise before usage was recorded;
    this applies to `extract` too). A cutoff with no parsed object raises an
    explicit error from all three structured calls instead of a bare `assert`.
- **Per-domain pacing, on by default.** `AWE_REQUEST_DELAY` (default `0.5`) is
  the minimum gap between the *starts* of two fetches to one registrable domain;
  `AWE_MAX_PER_DOMAIN` (default `4`) caps how many are in flight there at once —
  half the default `max_workers`, so it is a ceiling rather than a schedule and
  binds only when an origin is answering slowly enough that five requests
  overlap. The crawl boundary exists to keep a traversal on one site, so
  `max_workers` workers concentrate on a single origin and, before this, arrived
  as fast as httpx would go — the knob that makes the crawl fast was also what
  made it rude, with nothing in between. Set `AWE_REQUEST_DELAY=0` for the old
  behavior.
  - Enforced in [fetch.py](agentic_web_extraction/fetch.py) beside the transport
    memo, for the same reason: it is a fact about the *default transport*, keyed
    through `frontier.domain_of` like every other host comparison. Slots are
    reserved under the lock and slept for outside it, so N concurrent workers
    queue at t, t+delay, t+2·delay rather than all waking at the same instant —
    pacing that reads correctly in a log and changes nothing at the origin.
  - Gates the origin fetch only, never the recovery routes: `jina` and `wayback`
    talk to a third party whose rate limit has nothing to do with the crawled
    origin's, and pacing them under the origin's key would throttle the wrong host.
  - Wrapped around the whole retry sequence rather than each attempt, since
    tenacity already backs off ≥1s between attempts.
  - With `respect_robots` on, an origin's own `Crawl-delay` is honoured when it
    asks for more than `AWE_REQUEST_DELAY`. Read off the parser the allow-check
    already cached (no extra request) and passed down as `fetch(min_delay=...)`,
    so `fetch.py` never learns what robots.txt is. A `robots_overrides` domain is
    exempt from the delay too — honouring half of a policy the operator has been
    authorized to set aside is just a slower crawl.

- **DOM-level boilerplate removal before the Markdown conversion**
  (`AWE_MAIN_CONTENT_ONLY` / `--main-content-only`, default **on**). Drops
  `script`/`style`/`noscript`/`template`, and `header`/`footer`/`nav`/`aside`
  elements that are **not** inside a `main` or `article`. That exception is the
  point: an article's own `<header>` holds its title and date, which is exactly
  what a target schema asks for, so the naive "remove every header" rule loses
  data on precisely the pages worth extracting from.
  - On by default because site chrome is most of the DOM on a typical page and
    none of it answers the criterion, so leaving it in means every screen,
    summarize and extract call pays for the same masthead again. It is lossy —
    content a site keeps in an `<aside>` outside a `main`/`article` does not reach
    the extraction — so `--no-main-content-only` turns it off. Flipping it is safe
    with a warm cache: the page-cache key hashes the *filtered* markdown, so a
    change of the setting misses rather than replaying the other rendering.
  - Governs the *extraction input* only. Link discovery reads the unfiltered HTML,
    so filtering a navigation block out of the markdown never hides the links
    inside it from the scorer.
  - `beautifulsoup4` is now a declared dependency (markitdown already pulled it
    in; [normalize.py](agentic_web_extraction/normalize.py) now imports it
    directly). Pinned to the stdlib `html.parser`: which parser bs4 picks changes
    the markup it emits, which changes the content hash every cache key is built
    on, and a cache that misses because a wheel is present on one machine and not
    another is worse than a marginally slower parse.

- **Links no fetch could read are dropped before the scorer is billed for them.**
  `fetch._classify` admits HTML and PDF and nothing else, so a link to a `.zip`,
  `.jpg`, `.mp4` or `.css` was fetched, classified `skipped` and dropped — after
  the link scorer had already ranked it. `normalize.extract_links` now filters
  those out. No change in outcome, only in cost, which is why this one is on.
  - A deny list, not an allow list: `.php`, `.aspx`, `.do` and every extensionless
    URL are pages.
  - `.pdf` is absent from the list and deliberately **not** gated on `follow_pdf`.
    This filter runs in the worker, whose output the `PAGE` cache stores, so
    anything varying with configuration would be baked into an entry and replayed
    under a different configuration later — the same reasoning that keeps the
    crawl boundary at `frontier.push`. A pure function of the URL is the only kind
    of filter that is safe here.
  - `AWE_MAX_LINKS_PER_PAGE` (default `0`, off) caps links per scoring call, for
    the mega-navigation page. The one setting here left off, because no cap is
    right for the typical page: truncation is keyed on document order, so an
    ordinary site spends the allowance on its nav and loses the in-content links
    underneath it. Applied
    *after* links the crawl has already seen are removed — capping the raw list
    first hands the whole allowance to the site-wide navigation at the top of every
    page, leaves nothing new to score, and starves the frontier after the seed.
    It joins the `PAGE` cache key when set, since it decides the stored
    `link_scores`; unset, the key shape is unchanged.

- **A 200 that came back nearly empty can now trigger recovery**
  (`AWE_MIN_PAGE_TEXT_CHARS`, default `200`). A single-page app answers 200
  with a shell whose text arrives from JavaScript we do not run: it sailed past the
  status guard, normalized to almost nothing, was screened out as irrelevant, and
  left a log saying nothing was wrong — the one failure mode with no trigger and no
  trace. Failing to *obtain content* is what drives the chain, and this was the
  200-shaped version of it.
  - On at 200, matching `AWE_MIN_RECOVERED_TEXT_CHARS` so one number means "this
    is the page" on both sides of the chain. What it costs is requests, at a third
    party, on a page the origin already answered: a genuinely short page is
    indistinguishable from a shell by character count, so a "this document has
    moved" stub reaches jina/wayback too. Lower it rather than zeroing it if that
    trade is wrong for you; `0` accepts any 200 as content.
  - The recovered body must carry **more** visible text than the origin's to win.
    `fallback.recover` compares routes against each other, never against the page
    already in hand, so without this a thinner rendering could replace a real page.
    Turning the threshold on can only improve what comes back.

- **Sitemap seeding** (`AWE_USE_SITEMAP` / `--use-sitemap`, default **on**). Reads
  each seed origin's `robots.txt` `Sitemap:` lines, then `/sitemap.xml`, follows
  index documents, handles gzip, and offers what it finds to the link scorer.
  Best-first search's weakest spot is a page nothing links to prominently — page 12
  of a listing sits behind a paginator no scorer ranks highly — and a sitemap puts
  it in the frontier at the start.
  - Discovered URLs go **through** the frontier: scored by the same scorer, gated
    by the same crawl boundary, checked against the same robots policy. Pushing a
    few hundred unranked URLs at a fixed score would drown the relevance ordering
    that is the entire navigation policy.
  - Runs on the main thread (which owns the frontier) after `allowed` is built, so
    a site cannot nominate a domain the caller refused just by listing it.
  - Consumes no fetch budget — budget counts readable pages, and a sitemap is not
    one — and is skipped entirely under `seed_is_content`.
  - On by default because the URLs it finds are ranked, not privileged: the worst
    case is frontier candidates the scorer never pops. What it costs is bounded
    and spent at the origin being crawled, not a third party — at most
    `AWE_SITEMAP_MAX_DOCUMENTS` extra requests per seed origin, paced like every
    other fetch, before the traversal starts. It does change which pages a fixed
    budget reaches, which is the point; `--no-use-sitemap` restores a frontier
    containing only what the seed page itself links to.
  - Sitemap *documents* are restricted to the seed's own registrable domain. Both
    sources of locations — the `Sitemap:` lines in robots.txt and the `<loc>`s in a
    sitemap index — are written by the site being crawled, so without this an origin
    could name any address and have the client fetch it: another tenant's host, an
    internal service, a cloud instance's link-local metadata endpoint. The crawl
    boundary does not cover it, because that gates links entering the *frontier* and
    these are documents fetched before that.
  - Sitemap fetches go through the same pace gate as pages: they are the first
    requests a crawl makes.
  - The XML is written by the site being crawled, which is not a trusted party:
    `AWE_SITEMAP_MAX_BYTES` caps a body before parsing, a document declaring a
    DTD or an entity is refused unparsed (ElementTree does expand internal general
    entities, so billion-laughs works against it), and only `http`/`https`
    locations are returned. `AWE_SITEMAP_MAX_DOCUMENTS` and `AWE_SITEMAP_MAX_URLS`
    bound what one seed can cost.

- **`Extractor(on_event=...)`: structured progress without parsing stderr.** A
  subscriber receives a `logsink.Event(kind, message)` for every emitted line, for
  exactly the duration of `extract`. `kind` is the bracketed tag the lines already
  carry by convention (`fetch`, `blocked`, `robots`, `summarize`), so a caller can
  branch without parsing prose — best-effort, not a stable API, which is why the
  message is carried verbatim alongside it.
  - Hooks the sink rather than the Extractor's `_log`, so the transport, robots and
    fallback lines reach it too.
  - Subscribers are notified outside `logsink`'s lock: it is not reentrant, and a
    subscriber that logs — forwarding to another logger is half the point — would
    otherwise deadlock the crawl. A subscriber that raises is swallowed; a progress
    display must not cost a crawl that is otherwise working.

- **`awe schema` / `config.settings_schema()`** publish the JSON Schema of every
  setting: names, types, defaults, and an `env` key naming the variable that sets
  each one. A host codebase can now validate a configuration, or build a form for
  one, without importing the Extractor. It reads no values, so the output is safe
  to print, log or serve.

- **Tests for the half that costs money.** The suite covered fetch, boundary,
  robots and recovery, and nothing else — caching, summarization, consolidation and
  frontier ordering had no tests at all, which are the parts where a fault is
  expensive and silent. Added `test_cache_keys.py`, `test_summarize.py`,
  `test_frontier_order.py`, plus coverage for everything above.
  - A new `fake_tokens` fixture counts whitespace words instead of loading a real
    encoding. `tiktoken` downloads its table on first use, so the summarization
    path was reachable only with network — which also made
    `test_a_malformed_url_never_aborts_the_crawl` fail offline. It now passes.
  - `make_extractor` accepts a `cache=`; it hardcoded `cache=None`, which is why
    the cache had no tests.

- **`fetch()` now takes the caller's `Settings`.** Everything it reads — the pace
  gate, the thin-page threshold, `follow_pdf` — came from the process-wide
  `get_settings()`, so `Extractor(settings=...)` silently did not reach any of it,
  and the CLI's settings-only flags are built on exactly that. The Extractor passes
  its own settings per fetch; a direct caller still gets the process defaults. The
  transport memo threshold and the attempt budget stay global on purpose: the memo
  is shared across every Extractor in the process, and tenacity calls the attempt
  predicate from a retry hook with no access to the call's arguments.

- **Charset detection was broken under `main_content_only`.** The body was decoded
  to `str` before BeautifulSoup saw it, so the document's own `<meta charset>` was
  never consulted and a windows-1251 page that declares its encoding only in the
  markup arrived as replacement characters — silently, in the text the extraction
  model reads. bs4 now gets the undecoded bytes, with the HTTP header's charset as
  a hint it can override.

- **LICENSE (MIT) and CONTRIBUTING.md.** The package had no licence file at all,
  which left it legally unusable by default — including by the two org repos that
  already depend on it.

- **Docs** — caught the documentation up with v0.2.x. `.env.example` was missing
  `AWE_TRANSPORT_MEMO_FAILURES` and `AWE_MIN_RECOVERED_TEXT_CHARS` entirely, so both
  v0.2.3 knobs were undiscoverable from the file deployments actually copy. The
  README's roadmap stopped at v0.1 (no crawl boundary, robots.txt, attribution,
  recovery, impersonation, transport memo, thin-content check, or output cap), its
  project layout listed 4 of 7 test files, and its result-shape list named neither
  `protocol` nor `function_model` — the two fields cost is reconstructed from.
  `ExtractionResult.fallbacks_used`'s own docstring still described recovery as
  firing on a non-2xx response only and omitted the `impersonate:<target>` route.

## v0.2.3 — 2026-08-18

- **A host that has gone silent is written off, instead of re-proving it per page.**
  An origin that tarpits non-browser clients refuses every URL identically and does
  it by not answering, so each page spent its whole attempt budget (~35s) before
  reaching the recovery that could read it — one crawl spent ~10 minutes that way on
  a single site. After `AWE_TRANSPORT_MEMO_FAILURES` unanswered fetches (default
  `2`, `0` restores the old behavior) the registrable domain is written off for the
  rest of the process and later URLs there go straight to `AWE_FETCH_FALLBACKS`.
  - Deliberately hard to latch: only timeouts and network-level failures count (a
    malformed `Location` header says nothing about the host), **any** response clears
    it — 403 and 503 included, since a host refusing out loud is answering in one
    round-trip — and a domain that has answered even once is never written off,
    however many later fetches time out. The failure is attributed to the host that
    actually failed, which on a redirect is not the one asked for.
  - It cannot lose a page: when no route can read a URL on a written-off domain the
    origin is asked anyway, which is also the only way back (a fetch that succeeds
    clears the memo). Every `extract()` call starts by forgetting written-off hosts,
    so a crawl under a new User-Agent — or with `AWE_IMPERSONATE` newly enabled —
    re-tests them. Logged per URL as `[transport-memo]`.
- **Fixed — a client-rendered shell no longer ends the recovery chain.** A route that
  answered `200` with a few hundred bytes of empty containers won, because "a body
  arrived" was read as "the page was obtained": one homepage came back as 554 bytes
  through `impersonate` (raw HTML, no JS) where `jina` rendered the same URL to
  147KB, and `jina` was never asked. A body with less than
  `AWE_MIN_RECOVERED_TEXT_CHARS` of visible text (default `200`, `0` disables) is now
  treated as a decline and the next route is tried.
  - It reorders the chain; it cannot lose a page. If no route clears the bar, the
    fullest body obtained is returned anyway. PDFs are exempt — they carry their
    content as bytes, not as text.
  - **What this changes for you:** any page under the threshold falls through, not
    only a shell — a short stub the origin-only `impersonate` route served now reaches
    `jina`/`wayback` too, so those URLs are disclosed where an earlier route used to
    end the chain. Lower the threshold rather than zeroing it if that trade is wrong
    for you; real shells measure in the tens of characters.
- **Tests** — `tests/test_thin_content.py` for the chain fall-through, plus memo
  coverage in `tests/test_fetch_recovery.py` (latch, threshold, clear-on-any-response,
  answered-once immunity, silence-vs-dead-link, redirect attribution, the
  origin fall-back, and the per-crawl reset).

## v0.2.2 — 2026-08-18

Pages lost to *transport-level* blocking are now recoverable. Cut this as a
**minor** release: it changes what a deployment with recovery configured sends,
and how long a blocked domain takes to give up.

- **Behavior change — recovery now runs on transport failures too.** A fetch that
  produced no response at all (read timeout, dropped connection, malformed
  redirect header) went straight to `kind="error"` and never reached
  `AWE_FETCH_FALLBACKS`; only a non-2xx *response* did. That is backwards: an edge
  CDN that tarpits a non-browser client refuses less politely than one that
  answers 403, and was getting the better outcome. Both paths now recover.
  - **What this changes for you:** with a non-empty `AWE_FETCH_FALLBACKS`, more
    URLs are disclosed to `jina`/`wayback` and a blocked domain costs a recovery
    attempt on top of its retries. Set `AWE_FETCH_FALLBACKS=` empty for the old
    behavior (that also keeps the status guard, and makes no outbound call).
- **New recovery route: `impersonate`.** Re-requests the origin through
  [curl_cffi](https://github.com/lexiforest/curl_cffi), whose libcurl produces a
  browser's TLS/HTTP fingerprint, for origins that refuse on the shape of the
  handshake rather than on identity. Unlike `jina`/`wayback` it discloses nothing
  to a third party and returns live content, so put it first
  (`AWE_FETCH_FALLBACKS=impersonate,jina,wayback`) when it's on.
  - Off unless `AWE_IMPERSONATE` names a target (`chrome`, `safari`, …). Optional
    dependency: `pip install "agentic-web-extraction[impersonate]"`; without the
    wheel the route declines with a log line instead of failing the crawl.
  - `AWE_IMPERSONATE_BROWSER_UA` (default `false`) is a **separate** switch that
    drops attribution: a browser fingerprint under a verbatim browser UA is a full
    masquerade. Left off, the route sends your own `AWE_USER_AGENT`, which is
    enough for the large class of CDNs that key on fingerprint alone. Some sites
    reject that combination and are only reachable with it on — that is an
    institutional call about a clear refusal signal, so it isn't a default.
  - `AWE_IMPERSONATE_DOMAINS` scopes the escalation to named registrable domains
    (empty = every host). `AWE_IMPERSONATE_TIMEOUT` (default `30.0`) bounds it.
  - Recovered pages are still adjudicated by `allowed_domains` and robots.txt
    exactly as direct fetches are; the route is retrieval only. Provenance is
    `impersonate:<target>` in `FetchedPage.via` / `result.fallbacks_used`.
- **Fixed — a bot-sensor page is no longer read as robots.txt consent.** An origin
  that answers `/robots.txt` with `200` and an HTML interstitial parsed to *zero
  rules*, i.e. blanket permission, silently and at exactly the sites likeliest to
  have meant the opposite. A 200 that isn't plausibly a policy (non-text content
  type, or a body opening with markup) is now treated as *unavailable* — still
  failing open, but with a log line saying the rules were never obtained.
  - When `AWE_IMPERSONATE` covers a host, a robots.txt the default client can't
    obtain is retried over that transport, so a crawl doesn't read pages with a
    browser fingerprint while reading policy over the channel the site blocks.
    Never through `jina`/`wayback`: a policy must come from the origin.
- **`AWE_FETCH_ATTEMPTS`** (default `3`, the previous behavior) caps origin-fetch
  attempts. Read timeouts are capped at 2 regardless: a tarpit is deterministic,
  so attempts 2 and 3 spend the full read timeout each — ~35s to give up on a
  blocked URL instead of ~95s, which partly pays for the recovery attempt above.
- **Tests** — `tests/test_fetch_recovery.py` and `tests/test_impersonate.py`, plus
  robots.txt body-validation and escalation cases. Still fully offline; the
  `impersonate` route is exercised against a fake session, so the suite passes
  with the extra uninstalled.

## v0.2.1 — 2026-08-14

Crawl citizenship: the crawler can now be bounded, identified, and audited. Every
new knob defaults to v0.2.0 behavior, so upgrading the pin changes no crawl until a
caller opts in — with one exception, the recovery User-Agent, noted below.

- **Hard crawl boundary** — `Extractor(allowed_domains=[...])`, a default-deny
  allowlist of registrable domains (PSL/eTLD+1). Enforced where links are *queued*,
  not where requests go out, so redirects keep working and the page cache stays
  boundary-independent. Default `None` = unrestricted, as before.
  - Seed domains join the set automatically, so callers list only the extras and
    `[]` means "the seeds' own sites only".
  - Dropped links are logged `[blocked] <url>`, once per URL per crawl.
- **Opt-in redirect widening** — `allow_seed_redirect_domains` (default `False`)
  adds the domain a *seed* redirects to, so a rebrand doesn't dead-end a bounded
  crawl. Off by default: the seed's DNS owner would otherwise choose the extra
  domain. Requires the landing page to return readable content.
  - **This does not control whether redirects are followed.** `httpx` follows them
    inside a single fetch, as before; the flag decides only whether the landed
    domain joins the allowlist. With no `allowed_domains` set there is no
    allowlist, so the flag has no effect at all.
- **Attributable User-Agent** — `AWE_USER_AGENT` / `Extractor(user_agent=...)`,
  sent per request on origin fetches, both recovery routes, and the `robots.txt`
  fetch, so concurrent Extractors can't rename each other's traffic.
  - **Behavior change:** recovery requests (`jina`, `wayback`) now send this
    User-Agent rather than the separate `agentic-web-extraction/0.1 (fallback
    reader)` string, so all outbound traffic names one operator. Which route served
    a page is still recorded in `FetchedPage.via` / `result.fallbacks_used`. This
    applies whether or not you set `AWE_USER_AGENT`, and is the only default
    behavior that differs from v0.2.0.
- **robots.txt support** — `AWE_RESPECT_ROBOTS` (default off), one fetch per origin,
  evaluated against that User-Agent before the request. Re-checked on the resolved
  URL after a redirect (body discarded unread). Failure to obtain `robots.txt` fails
  open. `AWE_ROBOTS_OVERRIDES` exempts named domains.
- **CLI** — `--allowed-domain` (repeatable), `--allow-seed-redirect-domains`,
  `--user-agent`, `--respect-robots`, `--robots-override` (repeatable).
- **Fixed** — a single malformed `href` (a bracketed host such as
  `http://a[b]c.com/`) raised out of a worker thread and aborted the whole crawl,
  discarding every page already collected. Pre-existing; now degrades to losing
  that page's links.
- **Tests** — first test suite: 40 offline tests (`uv run pytest`), stub provider
  and stub web, no network or LLM calls.

## v0.2.0 — 2026-08-11

Baseline for this changelog; see the git history for earlier changes.
