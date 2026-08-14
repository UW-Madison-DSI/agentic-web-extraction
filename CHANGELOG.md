# Changelog

Write each change under `## Unreleased` as you make it.
[scripts/release.py](scripts/release.py) renames that heading to `## vX.Y.Z — <date>`
in the same commit as the version bump, then publishes the section as the GitHub
Release for the tag. An empty `## Unreleased` aborts the release.

## Unreleased

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
