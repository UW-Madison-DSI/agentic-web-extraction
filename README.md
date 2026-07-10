# agentic-web-extraction

An agent that **traverses** the web to find and extract structured data. Given a seed URL, a target schema, and a relevance criterion, the agent reads each page, ranks outgoing links by how likely they lead to a match, and follows the best one — until it satisfies the schema or hits its fetch budget.

> **Status: v0.** Public API below is implemented end-to-end. Expect breaking changes between minor versions.

## What it is

You give it a *seed URL*, a Pydantic schema describing what you're looking for, and a natural-language relevance criterion. The agent then:

1. Fetches the seed page (HTML, optionally a linked PDF if part of the content).
2. Normalizes HTML → Markdown to cut token cost.
3. Pre-screens the page against your criterion. If it matches, runs structured extraction against your schema and records the result.
4. Either way, scores every outgoing link by how likely it leads to a match, adds them to a frontier, and pops the highest-scoring unvisited link as the next page to fetch.
5. Repeats until the fetch budget is exhausted (or the frontier empties), accumulating every matching page, then merges them into a single result via the schema's optional `merge_extractions` hook (or returns the first match if the schema doesn't define one).

The library is **schema-agnostic and goal-directed**. It does not ship with built-in domains like "grants" or "companies" — you bring the schema, you bring the criterion, and the agent navigates *to* the answer.

## How the agent decides

The agent maintains a **frontier** — every unvisited link it has seen so far, each annotated with an LLM-assigned relevance score against your criterion. On each step it pops the highest-scoring link, fetches and normalizes it, then pre-screens.

- If pre-screen says **match**, it runs structured extraction and records the page as a match.
- Whether or not the page matched, it scores the new page's outgoing links against the goal and merges them into the frontier.
- The loop keeps going until the **fetch budget** is exhausted (or the frontier empties), collecting every matching page along the way.
- At the end, all matches are combined via the schema's optional `merge_extractions` classmethod (falling back to the first match). `stopped_reason` is `"match"` if at least one page matched, else `"budget_exhausted"`.

This is best-first search, not breadth-first or depth-first. The LLM's relevance scoring is the primary navigation policy; depth and per-link thresholds are deliberately **not** tunable in v0 — the budget is the main lever. The one opt-in exception is a *soft* same-domain preference (off by default; see below), which only re-weights scores and never excludes a link.

## What you provide

1. **Target schema** — a Pydantic model describing the fields you want extracted. It must be a `BaseModel` subclass (not a bare `list`), so to capture *many* records per page, use a container schema with a list field (e.g. `class Opportunities(BaseModel): items: list[Opportunity]`) and optionally define `merge_extractions` on it to fuse the per-page results — see [examples/grants.py](examples/grants.py).
2. **Screening criterion** — a natural-language description of what makes a page "in scope". Used by the pre-screen *and* by the link-scorer to rank the frontier. Example: *"Page describes a grant or funding opportunity that an academic PI could apply for."*
3. **Seed URL** — a single starting point. The agent traverses outward from there.

Optional:

- **Fetch budget** — `max_fetches` (default `10`). The agent stops when it has fetched this many pages.
- **Match mode** — `stop_on_first_match` (default `False`). `False` spends the budget gathering every matching page and merges them; `True` returns as soon as the first page matches.
- **Same-domain preference** — `off_domain_weight` (default `1.0`). Links off the seed's registrable domain get their score multiplied by this weight: `1.0` = full weight / no preference (the default), `< 1.0` = a soft nudge toward same-domain, `0.0` = strongest. It's a nudge, not a filter — off-domain links are never excluded. Comparison is at the registrable-domain (eTLD+1) level, so all of `*.wisc.edu` count as one domain.
- **Text filters** — `text_filters`, a list of `str -> str` transforms applied to the normalized markdown. This is where *you* strip volatile per-response tokens (rotating anti-bot tokens, per-render timestamps, shuffled recommendation strips) so a page's content hash stays stable and the page cache can hit. The library ships none — it's site-agnostic; ready-made examples live in [examples/strippers.py](examples/strippers.py).
- **Provider / model** — defaults to OpenAI; swappable.
- **Normalization toggle** — HTML→Markdown is on by default for cost reduction.
- **Custom prompts** — override the default link-scoring, pre-screen, and extraction prompts.

## What you get back

A typed object conforming to your schema (or `None` if budget ran out before a match), plus traversal metadata:

- `data` — the extracted Pydantic instance, or `None`
- `stopped_reason` — `"match"` | `"budget_exhausted"`
- `pages_fetched` — total fetches the traversal used
- `path` — ordered list of URLs the agent visited
- `verdicts` — one pre-screen verdict (`url`, `match`, `reason`) per screened page, in visit order
- provider and token usage across all calls, split by call purpose (each with the model it ran on)

Whether the agent succeeded or gave up, the result is structured the same way — easy to audit.

## Pipeline

The four extraction stages run inside a frontier loop:

```
seed URL
   │
   ▼
   Fetch ──▶ Normalize ──▶ Pre-screen
   ▲                            │
   │                     match? ─┴─ not yet
   │                       │          │
   │                       ▼          │
   │                   Extract        │
   │                (record match)    │
   │                       │          │
   │                       └────┬─────┘
   │                            ▼
   │                    Score outgoing
   │                    links (LLM);
   │                    add to frontier;
   │                    pop highest-scoring
   │                       unvisited
   │                            │
   │                     budget left?
   │                       │      │
   └───────── yes ─────────┘      no
                                  │
                                  ▼
                          merge all matches
                          ──▶ return result
                        (data=None if no match)
```

Each stage is independently swappable.

| Stage          | Notes                                                                                       |
|----------------|---------------------------------------------------------------------------------------------|
| Fetch          | Handles HTML; optionally follows linked PDFs that are part of the page                      |
| Normalize      | HTML → Markdown for token reduction; pluggable converter; caller-supplied `text_filters` run here |
| Pre-screen     | Cheap LLM call returning a binary yes/no against user-supplied criterion                    |
| Score links    | LLM scores every outgoing link's promise against the criterion; output feeds the frontier   |
| Extract        | Structured-output LLM call; provider-swappable; produces JSON conforming to user schema     |

By default the link-scorer reuses the pre-screen model — both are cheap, comparison-style calls.

## Example use cases

These are *illustrative* — the schemas and criteria belong to the caller.

**Grant opportunities.** Caller defines an `Opportunity` model (title, deadline, eligibility, sponsor, link), with criterion *"is this a grant a PI could apply for"*, and seeds the agent at a funding agency's landing page. The agent navigates the agency's site and stops on the first matching grant page within its fetch budget.

**University–industry engagement.** Caller defines a `Company` model (name, contact, engagement type), with criterion *"does this page describe company–university engagement"*, and seeds the agent at a company's homepage. The agent traverses partnership / news / about pages until it finds a matching engagement page.

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
from pydantic import BaseModel
from agentic_web_extraction import Extractor

class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str

extractor = Extractor(
    schema=Opportunity,
    criteria="Page describes a grant or funding opportunity an academic PI could apply for.",
    # provider/model defaults come from AWE_* env vars (see Configuration).
    # Pass `provider=MyProvider(...)` to inject a custom Provider instance.
    off_domain_weight=1.0,     # optional; falls back to AWE_OFF_DOMAIN_WEIGHT.
                               # 1.0 = full weight / no preference; < 1.0 softly favors
                               # the seed's registrable domain; 0.0 = strongest (never excludes).
    text_filters=None,         # optional; list of str->str transforms applied to
                               # the normalized markdown (cache-stability strippers,
                               # etc.). The library ships none — see examples/strippers.py.
)

result = extractor.extract(
    seed_url="https://example.gov/grants",
    max_fetches=10,            # optional; falls back to AWE_MAX_FETCHES
    stop_on_first_match=False, # optional; falls back to AWE_STOP_ON_FIRST_MATCH.
                               # True = return on the first match; False (default)
                               # = spend the budget gathering every match, then merge.
)
# result.data:           Opportunity | None  (merged across all matching pages)
# result.stopped_reason: "match" | "budget_exhausted"
# result.pages_fetched:  int
# result.path:           list[str]
# result.verdicts:       list[PageVerdict]  (one per screened page: url, match, reason)
# result.protocol:       str  -- provider adapter / wire protocol that ran the
#   crawl (e.g. "openai"); names the SDK/billing surface, not the model vendor.
# result.usage_by_function: dict[str, Usage]  -- token usage by call purpose
#   (screen / score_links / extract, plus any tag a caller passes to extract()).
#   Usage = (input_tokens, output_tokens, calls, cached_input_tokens); the cached
#   count is the prompt-cache subset of input_tokens, populated when the provider
#   reports it (OpenAI's usage.input_tokens_details.cached_tokens).
# result.function_model: dict[str, str]  -- which model each function ran on, so
#   cost is reconstructable; aggregate functions sharing a model for a per-model view.
```

`provider.extract(..., usage_tag="merge")` lets a caller bucket a structured-
output call under its own purpose; the screen and link-score calls are tagged
automatically. Sum `usage_by_function.values()` for a grand total.

Need to traverse several seed URLs and share the HTTP cache across them?

```python
results = extractor.extract_batch(
    seed_urls=["https://a.example/", "https://b.example/"],
    max_fetches=10,
)
```

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

#### Optional page cache

`Extractor(..., cache=)` accepts any object implementing the generic `KVCache`
protocol (`get(namespace, key)` / `put(namespace, key, value)`, see
[cache.py](agentic_web_extraction/cache.py)). When supplied, the crawler
content-addresses each page by the hash of its normalized markdown (mixed with a
version stamp over the criterion, schema, and models). If a page's content is
unchanged from a prior run, the crawler **replays** that page's screen verdict,
extracted data, and link scores with **zero LLM calls** — the page is still
fetched, so `pages_fetched` and the `max_fetches` budget are unaffected. It's
schema-agnostic (extracted data round-trips through your Pydantic model) and
opt-in; with no `cache` the behavior is exactly as before. The same cache is
forwarded to the schema's optional `merge_extractions(..., cache=)` so callers
can cache the merge too.

### CLI

```bash
uv run awe extract \
  --schema examples/grants.py:Opportunity \
  --criteria "Page describes a grant a PI could apply for." \
  --seed-url https://example.gov/grants \
  --max-fetches 10
```

The `--schema` flag takes either a dotted import path (`my_pkg.schemas:Opportunity`) or a path to a Python file (`./schemas.py:Opportunity`) — in both cases followed by `:ClassName`. Criteria can be a quoted string or `@path/to/criteria.txt`. Add `--stop-on-first-match` to return on the first matching page (or `--gather-all-matches` to force the gather-and-merge default); omit both to use `AWE_STOP_ON_FIRST_MATCH`. Add `--off-domain-weight 0.5` to softly down-weight off-domain links (`1.0` = full weight / no preference, the default; omit to use `AWE_OFF_DOMAIN_WEIGHT`). `text_filters` are Python-API-only (they're callables, not expressible on the command line), so a CLI crawl runs with no filters — use the Python API if you need them. The CLI prints the result as JSON and exits `0` on match, `2` on budget exhaustion.

### Runnable example

`examples/grants.py` is a runnable end-to-end demo and the reference for the `merge_extractions` hook. It defines a singular `Opportunity` plus an `Opportunities` **collection** schema, and extracts with the collection so a page can yield many opportunities. It seeds against a real Grants.gov page and, in gather-all mode, matches several linked NIH announcements; `Opportunities.merge_extractions` then folds them into one result using an **LLM dedup call** (`provider.extract(..., usage_tag="merge")`), which collapses records describing the same underlying opportunity and surfaces as a `"merge"` bucket in `usage_by_function`. It falls back to a deterministic link-dedup when no provider is available or the call fails. It also wires in the cache-stability `text_filters` from [examples/strippers.py](examples/strippers.py) to show how a caller supplies them.

```bash
uv run python examples/grants.py
```

Seed: `https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8`. Sample output (truncated) — the several matched pages collapse to one canonical opportunity:

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
  "usage_by_function": {
    "screen":      {"model": "gemma-4-26b-a4b-it", "input_tokens": 8637,  "output_tokens": 247,  "calls": 5, "cached_input_tokens": 6816},
    "score_links": {"model": "gemma-4-26b-a4b-it", "input_tokens": 10581, "output_tokens": 5994, "calls": 4, "cached_input_tokens": 8480},
    "extract":     {"model": "gemma-4-26b-a4b-it", "input_tokens": 16783, "output_tokens": 410,  "calls": 2, "cached_input_tokens": 16736},
    "merge":       {"model": "gemma-4-26b-a4b-it", "input_tokens": 593,   "output_tokens": 289,  "calls": 1, "cached_input_tokens": 224}
  }
}
```

(For a pure-Python merge instead, drop the `provider.extract` call and reconcile the records in code — the `"merge"` bucket then won't appear. The foundation-extraction reference shows a richer LLM merge that also uses the `cache` argument to memoize the dedup.)

Requires `OPENAI_API_KEY` and a reachable OpenAI-compatible endpoint (or your provider's equivalent) — see Configuration. The example's models default to `AWE_MODEL_EXTRACT` / `AWE_MODEL_SCREEN`; point these at models your key can actually access.

## Configuration

| Setting              | Env var               | Default                |
|----------------------|-----------------------|------------------------|
| OpenAI API key       | `OPENAI_API_KEY`      | required for default   |
| OpenAI base URL      | `OPENAI_BASE_URL`     | OpenAI's default       |
| Provider             | `AWE_PROVIDER`        | `openai`               |
| Extraction model     | `AWE_MODEL_EXTRACT`   | `gpt-5.5`              |
| Pre-screen model     | `AWE_MODEL_SCREEN`    | `gpt-5.4-mini`         |
| HTML→MD normalize    | `AWE_NORMALIZE`       | `true`                 |
| Follow linked PDFs   | `AWE_FOLLOW_PDF`      | `true`                 |
| Max page fetches     | `AWE_MAX_FETCHES`     | `10`                   |
| Stop at first match  | `AWE_STOP_ON_FIRST_MATCH` | `false` (gather all + merge) |
| Off-domain weight    | `AWE_OFF_DOMAIN_WEIGHT` | `1.0` (1.0 = full weight / no preference; < 1.0 = soft same-domain nudge) |
| HTTP response cache  | `AWE_HTTP_CACHE`      | `data/http_cache.sqlite` (empty = in-memory) |

Settings are loaded from `.env` if present (see `.env.example`).

`AWE_MAX_FETCHES` is the main traversal knob in v0. Depth limits and link-relevance thresholds are intentionally **not** user-configurable — the budget is the main lever and the LLM's link scoring is the navigation policy. The only exception is the opt-in soft same-domain preference (`AWE_OFF_DOMAIN_WEIGHT`, a single knob — `1.0` disables it), which re-weights scores but never excludes a link.

## Project layout

```
agentic_web_extraction/
    __init__.py          # re-exports + main() entry point
    cli.py               # Typer CLI: `extract` subcommand
    config.py            # AWE_* settings (pydantic-settings)
    cache.py             # KVCache protocol + content-hash helpers (opt-in page cache)
    extractor.py         # Extractor: frontier loop
    fetch.py             # httpx + hishel cache + tenacity retry
    frontier.py          # best-first heap + visited set + PSL registrable-domain (tldextract)
    normalize.py         # HTML→Markdown + raw-HTML link extraction + caller text_filters hook
    result.py            # ExtractionResult, Usage, ScreenVerdict, PageVerdict
    providers/
        __init__.py      # Provider protocol + factory
        openai_provider.py
examples/
    grants.py            # reference Opportunity + Opportunities schema (merge_extractions demo)
    strippers.py         # example cache-stability text_filters (site-specific; kept out of the package)
pyproject.toml           # uv project, Python ≥3.13
```

## Development

```bash
uv sync
uv run awe --help                       # CLI help
uv run ruff check                       # lint
uv run ruff format                      # format
uv run ty check                         # type-check
```

Python ≥3.13. Build backend: `uv_build`. The package lives at the repo root (`agentic_web_extraction/`), not under `src/` — `[tool.uv.build-backend].module-root = ""` enforces this.

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
- [x] Batch mode with caching (`Extractor.extract_batch`; in-memory hishel cache spans seeds)
- [x] Opt-in content-addressed page cache (`Extractor(..., cache=)` over a generic `KVCache`; replays screen/extract/score with no LLM calls when page content is unchanged)
- [x] Multi-match gather + `merge_extractions` hook (accumulate every matching page within budget, then merge)
- [x] Caller-supplied `text_filters` (site-specific cache-stability strippers live in `examples/`, not the library)
- [x] Opt-in soft same-domain preference (single `off_domain_weight` knob, `1.0` = off; PSL-based registrable domain via `tldextract`)
- [x] `examples/` directory with reference schemas and filters (`examples/grants.py`, `examples/strippers.py`, kept out of the package)
