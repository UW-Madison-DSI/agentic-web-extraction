# Changelog

Write each change under `## Unreleased` as you make it.
[scripts/release.py](scripts/release.py) renames that heading to `## vX.Y.Z — <date>`
in the same commit as the version bump, then publishes the section as the GitHub
Release for the tag. An empty `## Unreleased` aborts the release.

## Unreleased

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
