# agentic-web-extraction

An agent that **traverses** the web to find and consolidate structured data. Given one or more seed URLs, a target schema, and a relevance criterion, the agent reads each page, ranks outgoing links by how likely they lead to relevant content, follows the best ones — then pools every page that passed screening into a **single** structured extraction.

> **Status: v0.** Public API below is implemented end-to-end. Expect breaking changes between minor versions.

## Adopters

Repos in the `UW-Madison-DSI` org that declare a dependency on this package.
Refreshed weekly by [`.github/workflows/adopters.yml`](.github/workflows/adopters.yml) —
green = pinned to a concrete release, orange = floating (a git ref or no version
constraint at all).

<!-- adopters:start -->
[![UW-Madison-DSI/foundation-opportunity-extraction](https://img.shields.io/badge/foundation--opportunity--extraction-batch--extraction-orange)](https://github.com/UW-Madison-DSI/foundation-opportunity-extraction)
[![UW-Madison-DSI/rabbit-platform](https://img.shields.io/badge/rabbit--platform-8e8f674-orange)](https://github.com/UW-Madison-DSI/rabbit-platform)
<!-- adopters:end -->

Don't hand-edit the block between those markers — the next scan overwrites it.

<details><summary>How the scan works</summary>

GitHub Packages exposes no pull or download metric, and PyPI download counts
can't say *which* org repo installed us, so the signal has to come from the org's
own source.

It does **not** come from the Code Search API. Code search had only 23 of this
org's 55 Python repos indexed — repos whose `pyproject.toml` contains
`requires-python` returned zero hits for it, while the token had full private
visibility — so a search-driven count is a floor wearing the costume of a total.
It missed a real adopter. Instead,
[`scripts/adopters.py`](scripts/adopters.py) enumerates every repo from
`GET /orgs/{org}/repos` and reads each one's file tree directly
(`git/trees/{branch}?recursive=1`), which is complete by construction, sees
manifests in subdirectories, and doesn't care whether a repo is indexed. Then one
counting rule:

- **Counted** — a dependency manifest (`pyproject.toml`, `requirements*.txt`,
  `uv.lock`, …) declares the distribution on a non-comment line. Comments are
  stripped first, so a repo that mentions the package in a comment while running
  something else isn't reported as an adopter.
- **Reported, not counted** — a repo that only imports `agentic_web_extraction`
  or shells out to `awe extract`. That's necessary but not sufficient: a vendored
  copy, a sibling checkout, or a notebook that pip-installs from a branch all
  produce it, and none pin a version. This is the one signal still gathered by
  code search, so it inherits the index gap — but since it is only ever reported,
  never counted, that understates a gap already labelled as one.

Colour is about *what the repo tracks*, not whether a version string exists:
green only if every detected ref is a concrete release. A `uv.lock` entry with a
git source is deliberately labelled with its branch/tag/sha rather than the
`version` field next to it, because that field is just whatever this package's
own `pyproject.toml` said at that commit — reading it as a release pin would
paint a branch-tracking repo green.

The scan **hard-fails** rather than degrade, and reports what it examined:

- A missing token, rejected token, or any API error exits non-zero with the
  README untouched. `preflight` additionally proves private visibility *before*
  scanning, because an under-scoped token authenticates fine, answers HTTP 200 to
  everything, and returns empty results — rendering "none yet" over real
  adopters.
- A truncated file tree or an unexhausted page of repos is a hard error too, not
  a shrug: both mean the count is a floor being presented as a total.
- Every run prints `Examined N/M org repos`, so an incomplete scan is visible
  rather than implied.

Run it locally (dry run — prints the table and badge block, writes nothing):

```bash
GH_TOKEN=$(gh auth token) uv run --no-project scripts/adopters.py
```

The full org sweep is ~400 API calls and takes about 2½ minutes — all on the
5000/hour core bucket, so it fits a weekly job comfortably.

CI needs a secret `ADOPTER_SCAN_TOKEN`: a classic PAT with `repo` scope, or a
fine-grained PAT whose **resource owner is the org** (not a person) with all
repositories + Contents: read + Metadata: read. Only default branches are read,
so an adopter on an unmerged branch is invisible by design.

</details>

## What it is

You give it one or more *seed URLs*, a Pydantic schema describing what you're looking for, and a natural-language relevance criterion. The agent then:

1. Fetches each page (HTML, optionally a linked PDF if part of the content).
2. Normalizes HTML → Markdown to cut token cost.
3. Pre-screens the page against your criterion. Pages that match are kept.
4. Either way, scores every outgoing link by how likely it leads to relevant content, adds them to a frontier, and pops the highest-scoring unvisited links as the next pages to fetch.
5. Repeats until the fetch budget is exhausted (or the frontier empties), then **concatenates the markdown of every screened-in page and runs one structured extraction over the whole thing**.

The library is **schema-agnostic and goal-directed**. It does not ship with built-in domains like "grants" or "companies" — you bring the schema, you bring the criterion, and the agent navigates *to* the answer.

## How the agent decides

The agent maintains a **frontier** — every unvisited link it has seen so far, each annotated with an LLM-assigned relevance score against your criterion. It processes the frontier in **parallel waves**: it pops the top-scoring links (up to `max_workers` at once), fetches/normalizes/screens/scores them concurrently in a thread pool, and folds the results back.

- If pre-screen says **match**, the page's normalized markdown is collected for extraction.
- Whether or not the page matched, its outgoing links are scored against the goal and merged into the frontier.
- The loop keeps going until the **fetch budget** is exhausted (or the frontier empties), collecting every screened-in page along the way.
- At the end, all collected pages are concatenated and fed to **one** extraction call (summarized down first if they exceed the context budget — see below). `stopped_reason` is `"match"` if at least one page passed screening and extraction produced data, else `"budget_exhausted"`.

**Multiple seeds pool into one result.** Pass a list of seed URLs and they all share one frontier; the fetch budget is `max_fetches` *per seed* (so `max_fetches × len(seeds)` total). Every seed is guaranteed to be fetched, a URL reachable from several seeds is screened/scored only once, and everything that passes screening across all seeds feeds the same single extraction. More seeds simply means more content in the one result.

This is best-first search, not breadth-first or depth-first. The LLM's relevance scoring is the primary navigation policy; depth and per-link thresholds are deliberately **not** tunable — the budget is the main lever. `max_workers` trades a little best-first strictness (best-first *within* a wave) for parallelism, even on a single seed; set it to `1` for strictly sequential best-first. The one opt-in navigation exception is a *soft* same-domain preference (off by default; see below): rather than re-weighting scores in code, it hands the LLM the seed/page URL and a computed on-domain signal and asks it to disfavor off-domain content — a nudge the model applies with its own judgment, never a hard exclusion.

**The one hard limit: the crawl boundary.** `allowed_domains` is the exception to all of the above — a default-deny list of registrable domains a link may be *queued* from. Off (`None`) by default, so out of the box the scorer may send the crawl anywhere. Set it and a link pointing outside the set is dropped before it ever enters the frontier, logged as `[blocked]`, and therefore never fetched. It is deliberately *not* the same thing as `prefer_seed_domain`, which excludes nothing. See [Crawl boundary, attribution, robots.txt](#crawl-boundary-attribution-and-robotstxt).

**Fit-or-summarize.** The concatenated content of all screened-in pages is measured (via tiktoken) against `max_context_tokens`. If it fits, it's extracted as-is. If not, it's compressed first with a criteria- **and schema-aware** map-reduce on the cheap screen model — each page is summarized (keeping everything relevant to your criterion), the summaries are concatenated, and if still over budget the combined text is summarized again until it fits. The concatenated and post-summarization sizes are logged.

`always_summarize` (default `False`) turns the overflow check off as a *gate*: the map pass runs on every run, whether or not the content fits. Use it when you want the compression for its own sake — boilerplate, navigation chrome, and off-criterion prose reduced to a retention list before the strong model reads it, and a cheaper extraction call — rather than only as a last resort before overflow. The reduce passes are unchanged: they still only run while the text is over budget, so a corpus that already fits costs exactly one summarize call per page. Since summarization is lossy, leave it off unless you've checked that what your schema needs survives it.

Summarization is the pipeline's only lossy step — the extraction model never sees the original text — so the summarizer is given **your target schema** alongside the criterion, rendered as a compact field outline. The criterion says what's *topically* relevant; the schema says which concrete values are actually going to be asked for. Without it a summarizer optimizing for topical relevance will happily drop the dates, amounts, identifiers, citations, and URLs that a schema field requires. The prompt frames the schema as a retention list, not a task change: it instructs the model to copy literal values verbatim and keep list-record boundaries intact, while still emitting condensed prose (never JSON) — turning the cheap model into an extractor would lock in its mistakes and discard the context the strong model uses to disambiguate.

## What you provide

1. **Target schema** — a Pydantic model describing the fields you want extracted. It must be a `BaseModel` subclass (not a bare `list`), so to capture *many* records, use a container schema with a list field (e.g. `class Opportunities(BaseModel): items: list[Opportunity]`) — the single extraction fills its list from the pooled content of every screened-in page. See [examples/grants.py](examples/grants.py).
2. **Screening criterion** — a natural-language description of what makes a page "in scope". Used by the pre-screen *and* by the link-scorer to rank the frontier. Example: *"Page describes a grant or funding opportunity that an academic PI could apply for."*
3. **Seed URL(s)** — one starting point or a list. The agent traverses outward from all of them and pools the results into one extraction.

Optional:

- **Fetch budget** — `max_fetches` (default `10`), applied *per seed* (total budget is `max_fetches × number of seeds`).
- **Context budget** — `max_context_tokens` (default `128000`). The input-token budget for the single extraction; if the concatenated pages exceed it, they're summarized down first.
- **Always summarize** — `always_summarize` (default `False`). Run the map-reduce summarization even when the concatenated pages already fit the context budget, to compress boilerplate into a criteria/schema-aware retention list and shrink the extraction call. Costs one summarize call per page and makes the pipeline's lossy step unconditional.
- **Output cap** — `max_output_tokens` (default `0`, meaning no cap: the endpoint's own limit applies). A backstop for degenerate generation on the extract model. A JSON grammar permits arbitrary whitespace between tokens, so schema-guided decoding can't break a repetition loop the way it would for a malformed key — a model that falls into one emits blank indentation until something stops it. Uncapped, that's the endpoint's limit, which can outlast the client read timeout; the call then surfaces as a timeout and is silently re-sent by the SDK's own retries, so a single extraction can burn many minutes without a recoverable error ever reaching you. Setting a cap converts that into a prompt failure you can catch and re-roll. Size it above the largest legitimate extraction for your schema — a cap below that truncates good output.
- **Wave concurrency** — `max_workers` (default `8`). How many top-scored links are fetched/screened/scored at once. `1` = strictly sequential best-first.
- **Direct extraction** — `seed_is_content` (default `False`). When `True`, every seed URL is taken to *be* the content: the pre-screen is skipped (seeds are treated as guaranteed matches) and link-scoring is skipped (no links are queued), so the agent fetches just the seeds, consolidates them, extracts once, and stops. Use it when every seed is already a known target page and you only want the structured extraction — it skips the discovery machinery and the screen/score LLM calls entirely.
- **Same-domain preference** — `prefer_seed_domain` (default `False`). When `True`, the pre-screen and link-scorer calls are told the seed URL(s), the page/link URL, and a Python-computed `on_seed_domain` signal (on *any* seed's domain, for multi-seed runs), with an instruction to *disfavor* off-domain pages and links. The LLM applies it as a soft preference, not a filter — a clearly on-target off-domain page still matches / scores high, and nothing is excluded. Comparison is at the registrable-domain (eTLD+1) level via the Public Suffix List, so all of `*.wisc.edu` count as one domain.
- **Crawl boundary** — `allowed_domains` (default `None`, unrestricted). A default-deny list of registrable domains a scored link may be queued from; every seed's own domain is added automatically, so `[]` means "the seeds' sites and nowhere else". The only *hard* navigation limit in the library. Paired with `allow_seed_redirect_domains` (default `False`), which opts into widening the boundary to wherever a seed redirects, so a site that rebrands or moves host doesn't dead-end the crawl — off by default because it is the one way a party other than the caller can grow the set.
- **User-Agent** — `user_agent` (default `AWE_USER_AGENT`, itself defaulting to a generic library string). What every fetch sends, and the agent name robots.txt is matched against. Set it to something that names you and a real contact URL.
- **robots.txt** — `respect_robots` (default `False`) checks each origin's robots.txt for that user agent *before* fetching; `robots_overrides` exempts named domains. A robots.txt that can't be obtained fails open.
- **Text filters** — `text_filters`, a list of `str -> str` transforms applied to the normalized markdown. This is where *you* strip volatile per-response tokens (rotating anti-bot tokens, per-render timestamps, shuffled recommendation strips) so a page's content hash stays stable and the page cache can hit. The library ships none — it's site-agnostic; ready-made examples live in [examples/strippers.py](examples/strippers.py).
- **Provider / model** — defaults to OpenAI; swappable.
- **Normalization toggle** — HTML→Markdown is on by default for cost reduction.
- **Custom prompts** — override the default link-scoring, pre-screen, extraction, and summarize prompts.

## What you get back

A typed object conforming to your schema (or `None` if nothing screened in), plus traversal metadata:

- `data` — the single extracted Pydantic instance (pooled across every screened-in page), or `None`
- `stopped_reason` — `"match"` | `"budget_exhausted"`
- `pages_fetched` — total readable pages the traversal did LLM work on (across all seeds)
- `path` — ordered list of URLs the agent visited
- `verdicts` — one pre-screen verdict (`url`, `match`, `reason`) per screened page
- `content_tokens` — estimated token size of the raw concatenation of all screened-in pages
- `extraction_input_tokens` — token size of what actually fed the extraction (smaller than `content_tokens` when `summarized`)
- `summarized` — `True` when the concatenation was summarized down first (because it exceeded `max_context_tokens`, or because `always_summarize` is on)
- `fallbacks_used` — URL → route for any page the origin refused that was recovered elsewhere (`"jina"` / `"wayback:<timestamp>"`); empty on a clean crawl. Non-empty means some content did not come from the site itself — see [Blocked-page recovery](#blocked-page-recovery-jina--wayback)
- provider and token usage across all calls, split by call purpose (each with the model it ran on)

Whether the agent succeeded or gave up, the result is structured the same way — easy to audit.

## Pipeline

The traversal stages run inside a parallel-wave frontier loop, then one extraction consolidates everything:

```
seed URL(s)
   │
   ▼
   Fetch ──▶ Normalize ──▶ Pre-screen ──▶ match? ──▶ collect page markdown
   ▲                            │
   │                            ▼
   │                    Score outgoing
   │                    links (LLM);
   │                    add to frontier;
   │                    pop highest-scoring
   │                    unvisited (wave of N)
   │                            │
   │                     budget left?
   │                       │      │
   └───────── yes ─────────┘      no
                                  │
                                  ▼
                        concatenate all collected pages
                        ──▶ fits max_context_tokens?
                            (always_summarize forces "no")
                              │              │
                             yes             no ──▶ summarize (map-reduce,
                              │                        screen model)
                              └──────┬───────────────────┘
                                     ▼
                            one Extract call
                            ──▶ return result
                          (data=None if nothing screened in)
```

Each stage is independently swappable.

| Stage          | Notes                                                                                       |
|----------------|---------------------------------------------------------------------------------------------|
| Fetch          | Handles HTML; optionally follows linked PDFs that are part of the page. A non-2xx response is never content — recover it via a fallback route, else drop the page. Sends the configured User-Agent, and (opt-in) skips URLs robots.txt disallows before requesting them |
| Normalize      | HTML → Markdown for token reduction; pluggable converter; caller-supplied `text_filters` run here |
| Pre-screen     | Cheap LLM call returning a binary yes/no against user-supplied criterion                    |
| Score links    | LLM scores every outgoing link's promise against the criterion; output feeds the frontier, where the crawl boundary (if set) drops off-domain links before they are queued |
| Summarize      | When the concatenation overflows `max_context_tokens` (or on every run, with `always_summarize`); criteria- and schema-aware map-reduce on the screen model |
| Extract        | One structured-output LLM call over the concatenated (possibly summarized) content; produces JSON conforming to user schema |

By default the link-scorer and summarizer reuse the pre-screen model — all cheap, comparison/compression-style calls.

**The schema outline the summarizer is shown.** Rather than the raw `model_json_schema()` — mostly `title`/`type` scaffolding, `anyOf` pairs for nullables, and `$ref` indirection the cheap model has to resolve itself — the schema is rendered as an outline, with each definition emitted once and field descriptions carried through:

```
Opportunities:
  items: list[Opportunity] -- All distinct grant/funding opportunities described in the content.

Opportunity:
  title: string (required)
  deadline: string? -- ISO date if stated
  eligibility: string?
  sponsor: string?
  link: string (required)
```

That's 124 tokens against 405 for the equivalent JSON Schema; on a deeper schema the gap widens (a 7-field container over 3 nested models: 202 vs 1,042). Emitting each definition once — rather than flattening to dotted paths — also means nesting survives, shared sub-models aren't duplicated per referencing field, and a self-referential schema renders in finite space. It costs nothing per chunk regardless: the outline lives in the stable instruction prefix that's byte-identical across every chunk and reduce level, so the provider's prompt cache serves it. See [schema_outline.py](agentic_web_extraction/schema_outline.py).

**How much of the page each LLM call sees.** The cheap traversal calls read a truncated prefix of the normalized markdown; extraction reads the full concatenated content:

| Call        | Text sent                                                 |
|-------------|-----------------------------------------------------------|
| Pre-screen  | First 16,000 characters of the page                       |
| Score links | First 4,000 characters (links themselves are never truncated — every link is sent with its full URL and anchor text) |
| Summarize   | Full chunk (input is pre-chunked to fit the model window) |
| Extract     | Full concatenated content of all screened-in pages, untruncated (summarized first if over `max_context_tokens`, or always under `always_summarize`) |

Consequence: a page whose relevant content sits entirely beyond the first 16k characters of markdown will fail the pre-screen and never reach extraction, and the link scorer judges links with context from only the top of the page. Caller-supplied `text_filters` interact with this — stripping boilerplate moves real content earlier in the document, effectively widening what the screen and scoring calls see.

## Example use cases

These are *illustrative* — the schemas and criteria belong to the caller.

**Grant opportunities.** Caller defines an `Opportunity` model (title, deadline, eligibility, sponsor, link) inside an `Opportunities` list container, with criterion *"is this a grant a PI could apply for"*, and seeds the agent at a funding agency's landing page. The agent navigates the agency's site, collects every matching grant page within its budget, and extracts them all in one pass.

**University–industry engagement.** Caller defines a `Company` model (name, contact, engagement type) in a list container, with criterion *"does this page describe company–university engagement"*, and seeds the agent at a company's homepage (or several companies' homepages at once). The agent traverses partnership / news / about pages and consolidates the matching ones into a single result.

## Installation

Install directly from GitHub with the `git+` URL syntax — no PyPI release needed.

**uv** (add it to another project):

```bash
uv add "git+https://github.com/UW-Madison-DSI/agentic-web-extraction.git"
```

**pip** (into any environment):

```bash
pip install "git+https://github.com/UW-Madison-DSI/agentic-web-extraction.git"
```

**In `pyproject.toml`** (declare it as a project dependency):

```toml
[project]
dependencies = [
    "agentic-web-extraction @ git+https://github.com/UW-Madison-DSI/agentic-web-extraction.git",
]
```

Pin to a specific tag, branch, or commit by appending `@<ref>` to the URL:

```bash
uv add "git+https://github.com/UW-Madison-DSI/agentic-web-extraction.git@v0.1.0"   # tag
pip install "git+https://github.com/UW-Madison-DSI/agentic-web-extraction.git@main" # branch
```

Once installed, both the Python API and the `awe` CLI are available:

```python
from agentic_web_extraction import Extractor
```

```bash
awe extract --help
```

Requires Python ≥3.13. If the repository is private, use an SSH URL instead
(`git+ssh://git@github.com/UW-Madison-DSI/agentic-web-extraction.git`) and make sure
your Git credentials are configured.

## Usage

### Python

```python
from pydantic import BaseModel, Field
from agentic_web_extraction import Extractor

class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str

class Opportunities(BaseModel):          # list container: the one extraction fills it
    items: list[Opportunity] = Field(default_factory=list)

extractor = Extractor(
    schema=Opportunities,
    criteria="Page describes a grant or funding opportunity an academic PI could apply for.",
    # provider/model defaults come from AWE_* env vars (see Configuration).
    # Pass `provider=MyProvider(...)` to inject a custom Provider instance.
    prefer_seed_domain=False,  # optional; falls back to AWE_PREFER_SEED_DOMAIN.
                               # True = feed the LLM the seed/page URL + on-domain signal and
                               # ask it to disfavor off-domain content (a nudge, never excludes).
    allowed_domains=None,      # optional HARD boundary; None = unrestricted (the default).
                               # A list makes it default-deny: a link off it is never queued.
                               # Seed domains are added for you, so [] = "the seeds only".
    allow_seed_redirect_domains=False, # opt in to let a seed that redirects off-domain
                                       # widen the boundary to where it landed (rebrands,
                                       # moved hosts). Off by default: the seed's DNS
                                       # owner would otherwise pick the extra domain.
    user_agent=None,           # optional; falls back to AWE_USER_AGENT. Name yourself and
                               # a real contact URL — this is how a site owner reaches you.
    respect_robots=None,       # optional; falls back to AWE_RESPECT_ROBOTS (off).
    robots_overrides=None,     # optional; comma-separated domains exempt from that check.
    text_filters=None,         # optional; list of str->str transforms applied to
                               # the normalized markdown (cache-stability strippers,
                               # etc.). The library ships none — see examples/strippers.py.
)

result = extractor.extract(
    "https://example.gov/grants",   # a single URL, or a list of seed URLs
    max_fetches=10,            # optional; falls back to AWE_MAX_FETCHES (PER seed)
    seed_is_content=False,     # optional; falls back to AWE_SEED_IS_CONTENT.
                               # True = the seeds ARE the content: skip pre-screen +
                               # link-scoring, consolidate the seeds, extract, and stop.
)
# result.data:           Opportunities | None  (one extraction over all screened-in pages)
# result.stopped_reason: "match" | "budget_exhausted"
# result.pages_fetched:  int
# result.path:           list[str]
# result.verdicts:       list[PageVerdict]  (one per screened page: url, match, reason)
# result.content_tokens: int  -- raw concatenation size (tokens)
# result.extraction_input_tokens: int  -- what fed extraction (< content_tokens if summarized)
# result.summarized:     bool -- whether the concatenation was summarized down to fit
# result.fallbacks_used: dict[str, str]  -- url -> "jina" | "wayback:<timestamp>" for
#   pages the origin refused that were recovered elsewhere; empty on a clean crawl.
# result.protocol:       str  -- provider adapter / wire protocol that ran the
#   crawl (e.g. "openai"); names the SDK/billing surface, not the model vendor.
# result.usage_by_function: dict[str, Usage]  -- token usage by call purpose
#   (screen / score_links / summarize / extract, plus any tag a caller passes to extract()).
#   Usage = (input_tokens, output_tokens, calls, cached_input_tokens); the cached
#   count is the prompt-cache subset of input_tokens, populated when the provider
#   reports it (OpenAI's usage.input_tokens_details.cached_tokens).
# result.function_model: dict[str, str]  -- which model each function ran on, so
#   cost is reconstructable; aggregate functions sharing a model for a per-model view.
```

`provider.extract(..., usage_tag="...")` lets a caller bucket a structured-
output call under its own purpose; the screen, link-score, summarize, and extract
calls are tagged automatically. Sum `usage_by_function.values()` for a grand total.

Need to pool several seed URLs into one extraction? Pass a list — the budget is
`max_fetches` per seed, and a URL reachable from more than one seed is screened once:

```python
result = extractor.extract(
    ["https://a.example/", "https://b.example/"],
    max_fetches=10,   # 10 per seed → 20 total budget for the shared frontier
)
```

There's no HTTP-response cache — fetching is cheap and the frontier never re-fetches a URL within a crawl, so the crawler uses a plain client and spends nothing on memory or disk for HTTP. The expensive LLM work is what's cached (on by default; see below), and that cache persists across seeds and runs.

#### Text filters (cache-stability hacks live in *your* code)

The library is site-agnostic and does no site-specific text munging. But real
pages embed *volatile per-response fragments* — Cloudflare's rotating
email-obfuscation tokens, per-render timestamps, randomized form honeypot
labels, shuffled "related content" carousels — that change the normalized
markdown on every fetch and so defeat the content-addressed page cache (the hash
never repeats). `Extractor(..., text_filters=[...])` takes a list of pure
`str -> str` transforms applied in order to the normalized markdown, which is
where you strip those fragments so the hash stabilizes:

```python
from examples.strippers import CACHE_STABILITY_FILTERS
from agentic_web_extraction import Extractor

extractor = Extractor(schema=..., criteria=..., text_filters=CACHE_STABILITY_FILTERS)
```

[examples/strippers.py](examples/strippers.py) ships a ready-made set keyed to
specific real-world sites (Cloudflare, Foundant, Gravity Forms, EREF,
CyberGrants) — copy the ones you need or write your own. They live in `examples/`,
not the library, precisely so the core stays domain-agnostic; each filter is
built to remove only content-free/invisible markup, never text an LLM would use.

#### Crawl boundary, attribution, and robots.txt

Left to itself the crawler follows whatever the link scorer likes best. That is the
point of the design — and it means a crawl seeded at one site can end up fetching
pages on domains nobody vetted, reachable in a couple of hops from any "related
links" block. A URL-categorization appliance watching the egress sees a host
requesting flagged domains under a User-Agent that names a library rather than an
operator, and the crawl becomes somebody's security ticket.

Three knobs, all off/generic by default so upgrading changes nothing until you set
them:

```python
extractor = Extractor(
    schema=Opportunities,
    criteria="...",
    allowed_domains=["glpf.org", "smapply.io"],   # hard boundary (default-deny)
    user_agent="my-pipeline/1.0 (+https://example.edu/crawler; Some Team)",
    respect_robots=True,
    log_file="data/crawl_audit/crawl.log",        # durable record of both
)
```

**The boundary is enforced where links are queued, not where requests go out.** A
link whose registrable domain isn't in the set is dropped from the frontier and
logged; nothing about the HTTP layer changes. Three things follow from that choice:

- **Redirects keep working.** `httpx` follows redirects inside a single fetch, so a
  seed that 301s to a rebranded domain still resolves and still yields content. Only
  *queuing* is filtered. Set `allow_seed_redirect_domains=True` to also add the domain
  a seed landed on to the boundary, so its links stay crawlable — off by default,
  because whoever controls the seed's DNS then chooses that domain (a lapsed name now
  parked by someone else would nominate its own host). Even opted in, widening
  requires the landing page to return readable content, and a *non-seed* link that
  redirects off-boundary is read but expands no further.
- **It's per-Extractor state, not process-global.** Several crawls with different
  boundaries can run concurrently in one process, including inside a thread pool. The
  User-Agent is sent per request from the same place, so a second `Extractor` built
  meanwhile can't rename the first one's traffic mid-crawl.
- **Matching is at the registrable domain (eTLD+1) via the Public Suffix List**, so
  `glpf.org` covers `www.` and `grants.glpf.org`, and multi-label suffixes
  (`example.co.uk`) work without a hand-maintained list. Hosts with no registrable
  domain (`localhost`, an IP literal) key on the bare host, so they can still be
  named explicitly.

Every seed's own domain is added to the set automatically — a caller that passes a
seed URL is asking for that domain by definition — so `allowed_domains=[]` means
"the seeds' own sites and nowhere else", and you only ever list the *extras*.

**Why an allowlist and not a blocklist.** The domains that trigger this are usually
ordinary, legitimate sites that some categorization feed has flagged; they appear on
no malware list you could subscribe to. Default-deny excludes every domain a
blocklist would name plus the millions it wouldn't, needs no feed kept fresh, and
adds no network dependency to the fetch path.

**robots.txt** (`respect_robots`, off by default) is checked per origin *before* the
fetch, against the configured User-Agent, using the stdlib parser; one request per
origin for the life of the Extractor. A disallowed URL costs no request, no budget
slot and no LLM call, and is logged rather than reported as a visited page. A URL
that *redirects* is checked twice — once as requested, once where it landed, since a
redirector on an allowed path would otherwise be a hole straight through the check
(including across origins). The second request is already spent, because httpx
follows redirects inside a single call and refusing to follow them would break the
rebrand case the boundary depends on; what the second check buys is that the body is
discarded unread rather than screened and pooled into the extraction.
Failures — 404, 401/403, 5xx, timeouts — **fail open**: a missing robots.txt has
never meant "stay out", and treating an origin's brief 500 as a site-wide `Disallow`
would silently empty an authorized crawl. `robots_overrides` exempts named domains
for hosts that blanket-disallow automated clients but whose content you're
authorized to read.

The boundary and robots are complementary, not redundant: the boundary decides
*where* the crawl may go (a hard guarantee you can state to a security team), robots
decides *what* it may read once there (the site's own wishes). Neither replaces
[`--log-file`](#logging), which is what makes any of it auditable after the fact —
`[page]`, `[blocked]` and `[robots]` lines with timestamps, on disk, surviving the
container.

#### Blocked-page recovery (jina → wayback)

Fetch classifies on Content-Type, so an HTTP error page served as `text/html` —
an edge-CDN "Access Denied" interstitial, a themed 404 — used to look exactly like
the page. It would be normalized, screened, pooled into the concatenation, and
extracted from. Under `seed_is_content` that is worse than losing the page:
screening is skipped, so the error text is *guaranteed* into the extraction and the
URL becomes a citable source.

So **a non-2xx response is never content**. Before dropping it, the fetcher tries
the routes in `AWE_FETCH_FALLBACKS`, in order:

| Route | What it does | Trade-off |
|-------|--------------|-----------|
| `jina` | `r.jina.ai` renders the URL server-side and returns it — also reads PDFs, so a blocked document comes back as text | Live content, but a third party sees the URL; anonymous requests are rate-limited (set `JINA_API_KEY`) |
| `wayback` | The Internet Archive's newest successful capture, served with the `id_` modifier so bytes come back unrewritten | Free and stable, but as stale as the capture — bound it with `AWE_WAYBACK_MAX_AGE_DAYS` |

The first route that returns content wins; if all decline, the page becomes
`kind="error"` and the traversal skips it at no LLM cost and no budget slot.

Recovered content is always returned **under the original URL**, never the
reader/archive address — `page.url` is what lands in `path`, what the extraction
prompt sees in its `--- SOURCE:` markers, and what any citation a caller builds
derives from. The route is recorded per page (`FetchedPage.via`) and surfaced as
`result.fallbacks_used`, so a caller can disclose which findings didn't come from
the site itself.

By default Jina is asked for the full DOM (`AWE_JINA_RETURN_FORMAT=html`), so
normalization, link extraction and the frontier behave exactly as on a direct
fetch. Set it empty for Jina's readability pass instead — markdown with the nav
chrome stripped, roughly 3× fewer tokens, but only the links its extraction kept.
That markdown is escaped and its links promoted to real anchors before it enters
the pipeline; handing raw markdown to the HTML normalizer would *silently delete*
angle-bracketed text (`a <threshold> of 5` → `a  of 5`) and surface no links at all.

Both routes disclose the crawled URL to a third party. Set `AWE_FETCH_FALLBACKS=`
empty to keep only the status guard and never make an outbound call.

#### LLM-response cache (on by default)

The crawler caches its LLM work, keyed by content, at three levels:

- **Per page** — each page is content-addressed by the hash of its normalized markdown (mixed with a version stamp over the criterion, schema, provider prompt templates, and models — so changing any of those, or requesting a different schema for the same URL, misses instead of serving a stale result). If a page's content is unchanged from a prior run, the crawler **replays** that page's screen verdict and link scores with **zero LLM calls** — the page is still fetched, so `pages_fetched` and the budget are unaffected.
- **The consolidated extraction** — keyed on the *set* of contributing pages (a hash of all their per-page cache keys, plus the context/encoding settings that govern the summarize-or-not decision). It replays only when **the exact same set of pages with the same content** recurs — change, add, or drop any screened-in page and the extraction re-runs. The stored value carries the context-size metadata too, so a hit replays the full result (including `content_tokens` / `summarized`).
- **Per summary chunk** — when summarization runs, each chunk's summary is cached by its content hash, so an unchanged page replays its summary for free. The same version stamp is mixed in, so changing the schema — which changes what the summarizer was told to preserve — invalidates stored summaries too.

Extracted data round-trips through your Pydantic model, so the cache is schema-agnostic.

It's **on by default** — a SQLite store at `AWE_LLM_CACHE` (`data/llm_cache.sqlite`).
Set `AWE_LLM_CACHE=` empty (or pass `Extractor(..., cache=None)`) to disable it. The
store is [`SqliteKVCache`](agentic_web_extraction/cache.py), a single
`kv(namespace, key, value)` table; to use your own backend (Redis, a shared DB),
pass any object implementing the `KVCache` protocol (`get(namespace, key)` /
`put(namespace, key, value)`) as `Extractor(..., cache=...)`.

#### Flex service tier (off by default)

Set `AWE_USE_FLEX=true` to send every LLM call on OpenAI's
[flex service tier](https://developers.openai.com/api/docs/guides/flex-processing),
which bills at Batch-API rates — **50% off input and output** — and whose discount
still stacks with prompt caching. Unlike the Batch API it is *synchronous*, so
nothing about the traversal changes: same wave loop, same best-first ordering, same
result shape.

What you trade for the halved bill:

- **Latency.** Flex calls can be far slower, so the client's read timeout is raised
  from 600s to 1800s when it's on. This is a knob for bulk/offline runs, not
  interactive ones.
- **Capacity.** Flex can refuse a request outright with an uncharged
  `429 resource_unavailable`. The provider retries that *single call* on the standard
  tier (`service_tier="auto"`) and logs `[llm] flex capacity unavailable`, so a run
  never fails purely for want of flex capacity — it just pays standard price for the
  calls that couldn't get flex. An ordinary rate-limit 429 is **not** escalated this
  way; it propagates as before.

The tier affects price and latency, never response content, so it's deliberately
**not** part of any cache key — a run with flex on will happily reuse entries a
standard run wrote, and vice versa. Availability is per-model (both default models
support it as of writing); check the
[pricing page](https://developers.openai.com/api/docs/pricing?latest-pricing=flex)
for the current list.

Most of a run's cost sits in the single consolidated extraction — one call on the
stronger model with up to `AWE_MAX_CONTEXT_TOKENS` of input, against many small calls
on the cheap screen model — so the discount lands mostly there.

### CLI

```bash
uv run awe extract \
  --schema examples/grants.py:Opportunities \
  --criteria "Page describes a grant a PI could apply for." \
  --seed-url https://example.gov/grants \
  --max-fetches 10
```

The `--schema` flag takes either a dotted import path (`my_pkg.schemas:Opportunities`) or a path to a Python file (`./schemas.py:Opportunities`) — in both cases followed by `:ClassName`. Criteria can be a quoted string or `@path/to/criteria.txt`. Repeat `--seed-url` to pool several seeds into one extraction (`--seed-url URL1 --seed-url URL2`); the fetch budget applies per seed. Add `--max-context-tokens N` to change the extraction input budget (over it, pages are summarized down; defaults to `AWE_MAX_CONTEXT_TOKENS`), `--always-summarize` to summarize even when the pages already fit that budget (`--no-always-summarize` forces it off, the default; omit to use `AWE_ALWAYS_SUMMARIZE`), and `--max-workers N` to change wave concurrency (defaults to `AWE_MAX_WORKERS`). Add `--seed-is-content` to treat the seeds as the content directly — skip the pre-screen and link-scoring, consolidate the seeds, and extract (`--no-seed-is-content` forces it off, the default; omit to use `AWE_SEED_IS_CONTENT`). Add `--prefer-seed-domain` to softly disfavor off-domain pages/links (the LLM is told the seed/page URL and an on-domain signal; `--no-prefer-seed-domain` forces it off, the default; omit to use `AWE_PREFER_SEED_DOMAIN`). Add `--allowed-domain glpf.org` (repeatable) to impose the hard crawl boundary — links off the listed domains are never queued; seed domains are included automatically, and `--allow-seed-redirect-domains` opts into letting a redirecting seed widen it (off by default). Add `--user-agent "my-pipeline/1.0 (+https://example.edu/crawler)"` to identify the crawler (defaults to `AWE_USER_AGENT`), `--respect-robots` to honor robots.txt before each fetch (`--no-respect-robots` forces it off, the default; omit to use `AWE_RESPECT_ROBOTS`), and `--robots-override site.org` (repeatable) to exempt a domain from that check. Add `--log-file run.log` to also write a timestamped log file (off by default — no path, no file; see [Logging](#logging)). Add `--no-cache` to disable the on-by-default LLM-response cache (equivalently `AWE_LLM_CACHE=`). `text_filters` are Python-API-only (they're callables, not expressible on the command line), so a CLI crawl runs with no filters — use the Python API if you need them. The CLI prints the result as JSON and exits `0` on match, `2` on budget exhaustion.

### Runnable example

`examples/grants.py` is a runnable end-to-end demo. It defines a singular `Opportunity` plus an `Opportunities` **collection** schema, and extracts with the collection so the one extraction can return many opportunities. It seeds against a real Grants.gov page, collects several linked NIH announcement pages that pass screening, and extracts them all in a single pass over the concatenated content. It also wires in the cache-stability `text_filters` from [examples/strippers.py](examples/strippers.py) to show how a caller supplies them. LLM caching is on by default, so nothing cache-related is wired in the example — run it twice and the second run's `usage_by_function` shows `0` calls across the board (screen/score replay per unchanged page, and the consolidated extraction replays because every contributing page hit the cache). Delete `data/llm_cache.sqlite`, or pass `Extractor(..., cache=None)`, to force a cold crawl.

```bash
uv run python examples/grants.py
```

Seed: `https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8`. Sample output (truncated) — every screened-in page is pooled into one extraction:

```json
{
  "data": {
    "items": [
      {
        "title": "Development and Application of PET and SPECT Imaging Ligands ...",
        "deadline": "February 05, 2025",
        "sponsor": "National Institutes of Health (NIH)",
        "link": "https://grants.nih.gov/grants/guide/pa-files/PAR-23-164.html"
      }
    ]
  },
  "stopped_reason": "match",
  "pages_fetched": 5,
  "path": ["https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8", "..."],
  "verdicts": [
    {"url": "https://simpler.grants.gov/opportunity/24a2e68b-...", "match": true, "reason": "..."}
  ],
  "protocol": "openai",
  "content_tokens": 14820,
  "extraction_input_tokens": 14820,
  "summarized": false,
  "usage_by_function": {
    "screen":      {"model": "gemma-4-26b-a4b-it", "input_tokens": 8637,  "output_tokens": 247,  "calls": 5, "cached_input_tokens": 6816},
    "score_links": {"model": "gemma-4-26b-a4b-it", "input_tokens": 10581, "output_tokens": 5994, "calls": 4, "cached_input_tokens": 8480},
    "extract":     {"model": "gemma-4-26b-a4b-it", "input_tokens": 14820, "output_tokens": 512,  "calls": 1, "cached_input_tokens": 0}
  }
}
```

Requires `OPENAI_API_KEY` and a reachable OpenAI-compatible endpoint (or your provider's equivalent) — see Configuration. The example's models default to `AWE_MODEL_EXTRACT` / `AWE_MODEL_SCREEN`; point these at models your key can actually access.

## Configuration

| Setting              | Env var               | Default                |
|----------------------|-----------------------|------------------------|
| OpenAI API key       | `OPENAI_API_KEY`      | required for default   |
| OpenAI base URL      | `OPENAI_BASE_URL`     | OpenAI's default       |
| Provider             | `AWE_PROVIDER`        | `openai`               |
| Extraction model     | `AWE_MODEL_EXTRACT`   | `gpt-5.5`              |
| Pre-screen model     | `AWE_MODEL_SCREEN`    | `gpt-5.4-mini`         |
| Flex service tier    | `AWE_USE_FLEX`        | `false` (true = 50% off at Batch-API rates, slower; auto-fallback to standard) |
| HTML→MD normalize    | `AWE_NORMALIZE`       | `true`                 |
| Follow linked PDFs   | `AWE_FOLLOW_PDF`      | `true`                 |
| Blocked-page recovery | `AWE_FETCH_FALLBACKS` | `jina,wayback` (ordered; empty = drop blocked pages, no outbound call) |
| Jina API key         | `JINA_API_KEY`        | unset (anonymous, rate-limited) |
| Jina return format   | `AWE_JINA_RETURN_FORMAT` | `html` (empty = readability markdown) |
| Archive staleness cap | `AWE_WAYBACK_MAX_AGE_DAYS` | `0` (any age) |
| Max page fetches     | `AWE_MAX_FETCHES`     | `10` (per seed)        |
| Extraction context budget | `AWE_MAX_CONTEXT_TOKENS` | `128000` (over it → summarize to fit) |
| Always summarize     | `AWE_ALWAYS_SUMMARIZE` | `false` (true = summarize even when the content already fits) |
| Extraction output cap | `AWE_MAX_OUTPUT_TOKENS` | `0` (no cap; set it to bound degenerate generation on the extract model) |
| Wave concurrency / beam width | `AWE_MAX_WORKERS` | `8` (1 = sequential best-first) |
| Token-count encoding | `AWE_TIKTOKEN_ENCODING` | `o200k_base` (fallback for models tiktoken doesn't know) |
| Seed is content      | `AWE_SEED_IS_CONTENT` | `false` (true = skip screen + link-scoring, extract the seeds directly) |
| Prefer seed domain   | `AWE_PREFER_SEED_DOMAIN` | `false` (true = LLM disfavors off-domain pages/links) |
| Crawl User-Agent     | `AWE_USER_AGENT`      | `agentic-web-extraction/0.1 (+https://github.com/)` — replace with an attributable string |
| Respect robots.txt   | `AWE_RESPECT_ROBOTS`  | `false` (true = check per origin before fetching; failures fail open) |
| robots.txt overrides | `AWE_ROBOTS_OVERRIDES` | empty (comma-separated domains exempt from the check) |
| LLM-response cache   | `AWE_LLM_CACHE`       | `data/llm_cache.sqlite` (on; empty = disable) |
| Log file path        | `AWE_LOG_FILE`        | empty (off; set a path to enable) |

Settings are loaded from `.env` if present (see `.env.example`).

The **crawl boundary** is deliberately not an env var: which domains are in scope
depends on the seeds of a given crawl, not on the deployment. Pass it per crawl —
`Extractor(allowed_domains=[...])` or `awe extract --allowed-domain ...`.

### Logging

Progress and diagnostic lines (per-page fetch/score status, per-LLM-call timing and token counts) always go to **stderr** — never stdout, which carries the result JSON, so piping the CLI's output stays clean. On top of that, giving a **log file path** also appends every line, prefixed with a `YYYY-MM-DD HH:MM:SS` timestamp, to that file — a durable, timestamped record for a host codebase that wants one. It's a single knob: **no path means no file** (the default).

Enable it via env (`AWE_LOG_FILE=run.log`), the CLI (`--log-file run.log`), or the Python API:

```python
Extractor(schema=..., criteria=..., log_file="run.log")  # "" or omit = no file
```

`AWE_MAX_FETCHES` (per seed) is the main traversal knob. Depth limits and link-relevance thresholds are intentionally **not** user-configurable — the budget is the main lever and the LLM's link scoring is the navigation policy. `AWE_MAX_WORKERS` is a concurrency knob (wave/beam width), not a relevance policy: best-first ordering holds within each wave. The opt-in soft same-domain preference (`AWE_PREFER_SEED_DOMAIN`, a single on/off knob) feeds the LLM an on-domain signal and asks it to disfavor off-domain content but never excludes a link. The single *hard* limit is the crawl boundary (`allowed_domains`), which drops off-boundary links at the queue point — see [Crawl boundary, attribution, robots.txt](#crawl-boundary-attribution-and-robotstxt).

The log is the audit trail for both: `[page] <url>` per fetch, `[blocked] <url>` per link the boundary dropped, `[robots] …` per page robots.txt kept us off. Point `AWE_LOG_FILE` at a path on a volume that outlives the container if you ever expect to have to reconstruct what a crawl did.

## Project layout

```
agentic_web_extraction/
    __init__.py          # re-exports + main() entry point
    cli.py               # Typer CLI: `extract` subcommand
    config.py            # AWE_* settings (pydantic-settings)
    cache.py             # KVCache protocol + SqliteKVCache + content-hash helpers (on-by-default LLM cache)
    extractor.py         # Extractor: parallel-wave frontier loop + consolidated extraction
    summarize.py         # criteria/schema-aware map-reduce summarization (fit to context budget)
    schema_outline.py    # renders a caller schema as a compact field outline for the summarize prompt
    tokens.py            # tiktoken-backed token counting + token-aware splitting
    fallback.py          # blocked-page recovery routes (jina, wayback)
    fetch.py             # httpx (plain, no HTTP cache) + tenacity retry + status guard + UA
    robots.py            # opt-in robots.txt policy (per-origin cache; fails open)
    logsink.py           # shared stderr + optional timestamped log-file sink
    frontier.py          # best-first heap + visited set + PSL registrable-domain (tldextract)
    normalize.py         # HTML→Markdown + raw-HTML link extraction + caller text_filters hook
    result.py            # ExtractionResult, Usage, ScreenVerdict, PageVerdict
    providers/
        __init__.py      # Provider protocol + factory
        openai_provider.py
examples/
    grants.py            # reference Opportunity + Opportunities list-container schema
    strippers.py         # example cache-stability text_filters (site-specific; kept out of the package)
tests/
    conftest.py          # offline stub provider + stub web (no network, no LLM)
    test_crawl_boundary.py  # allowed_domains: default-deny, seed redirects, domain keys
    test_robots.py       # robots.txt verdicts, overrides, fail-open
    test_user_agent.py   # User-Agent configuration reaches both http clients
pyproject.toml           # uv project, Python ≥3.13
scripts/
    adopters.py          # weekly org adoption scan (stdlib-only PEP 723)
    release.py           # version bump + tag + atomic push (PEP 723; typer/rich)
.github/workflows/
    adopters.yml         # cron: refresh the adopter badges at the top of this file
```

## Development

```bash
uv sync
uv run awe --help                       # CLI help
uv run pytest                           # tests (offline: stub provider + stub web)
uv run ruff check                       # lint
uv run ruff format                      # format
uv run ty check                         # type-check
```

Python ≥3.13. Build backend: `uv_build`. The package lives at the repo root (`agentic_web_extraction/`), not under `src/` — `[tool.uv.build-backend].module-root = ""` enforces this.

### Releasing

Per-release notes live in [CHANGELOG.md](CHANGELOG.md).

```bash
uv run scripts/release.py [major|minor|patch]   # default: patch
```

Takes the version in `pyproject.toml` as the base, bumps the chosen component,
then commits `chore: release vX.Y.Z` and pushes the branch and a matching
`vX.Y.Z` tag **atomically** — the pyproject version and the git tag can never
drift apart. `uv.lock` is gitignored here, so it stays out of the release; if
it's ever tracked, the script refreshes it in the same commit (uv records the
project version there too). Consumers pin a release with:

```bash
uv add "agentic-web-extraction @ git+https://github.com/UW-Madison-DSI/agentic-web-extraction@v0.1.1"
```

The script refuses to do anything unless the release is safe: it must run from
`main`, the working tree must be clean, and the branch must be exactly level
with its remote (neither ahead nor behind). If any step after the bump fails,
the version, the commit and the tag are all rolled back, so a retry starts from
a clean slate and releases the number that failed rather than skipping it. It's
a PEP 723 script — `typer`/`rich` come from its own header, not the project deps.

## Roadmap

v0 done:

- [x] HTML→MD converter (`markitdown`)
- [x] Structured-output extractor (OpenAI Responses API + Pydantic `text_format`)
- [x] Provider abstraction (`Provider` protocol + factory; OpenAI is the v0 impl)
- [x] PDF fetcher and text extraction (markitdown handles PDF; toggle via `AWE_FOLLOW_PDF`)
- [x] LLM link-scorer + frontier data structure (best-first heap)
- [x] Visited-set / dedupe (URL canonicalization; dedup on push and pop)
- [x] Budget accounting + `stopped_reason` plumbing
- [x] Path recording in result metadata
- [x] Multi-seed pooling (pass a list of seeds into one shared frontier; budget is per seed; a URL reachable from several seeds is screened once; the LLM cache persists across seeds and runs)
- [x] Consolidate-then-extract (concatenate every screened-in page and run one structured extraction over the whole thing — no per-page extraction, no merge/dedup)
- [x] Fit-or-summarize (`max_context_tokens`; over budget, a criteria-aware map-reduce on the screen model compresses the content to fit, via tiktoken chunking) — plus an opt-in `always_summarize` that runs the map pass even when the content fits
- [x] Schema-aware summarization (the target schema is rendered as a compact field outline and handed to the summarizer as a retention list, so compression can't drop the concrete values extraction will demand)
- [x] Parallel-wave traversal (`max_workers`; pop the top-N frontier links and fetch/screen/score them concurrently, even on a single seed)
- [x] On-by-default content-addressed LLM cache (`SqliteKVCache` at `AWE_LLM_CACHE`, swappable via the `KVCache` protocol; replays per-page screen/score, per-chunk summaries, and the consolidated extraction — when every contributing page is unchanged — with no LLM calls)
- [x] Caller-supplied `text_filters` (site-specific cache-stability strippers live in `examples/`, not the library)
- [x] Opt-in soft same-domain preference (single `prefer_seed_domain` knob, off by default; LLM is fed an on-domain signal and asked to disfavor off-domain content; PSL-based registrable domain via `tldextract`)
- [x] Opt-in direct extraction (single `seed_is_content` knob, off by default; treats the seed pages as the content — skips the pre-screen and link-scoring, no discovery)
- [x] Opt-in flex service tier (single `AWE_USE_FLEX` knob, off by default; Batch-API rates on synchronous calls, per-call fallback to standard when flex has no capacity)
- [x] `examples/` directory with reference schemas and filters (`examples/grants.py`, `examples/strippers.py`, kept out of the package)
- [x] Weekly org adoption scan (`scripts/adopters.py` + `.github/workflows/adopters.yml`; index-independent repo/tree sweep → shields badges in the [Adopters](#adopters) block, hard-fails and reports coverage rather than committing a silent undercount)
- [x] Guarded release script (`scripts/release.py`; main-only, clean-and-synced preconditions, pyproject/`uv.lock`/git-tag kept in sync, atomic branch+tag push with rollback — see [Releasing](#releasing))
