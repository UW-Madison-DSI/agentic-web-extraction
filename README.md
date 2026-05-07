# agentic-web-extraction

An agent that **traverses** the web to find and extract structured data. Given a seed URL, a target schema, and a relevance criterion, the agent reads each page, ranks outgoing links by how likely they lead to a match, and follows the best one — until it satisfies the schema or hits its fetch budget.

> **Status: v0.** Public API below is implemented end-to-end. Expect breaking changes between minor versions.

## What it is

You give it a *seed URL*, a Pydantic schema describing what you're looking for, and a natural-language relevance criterion. The agent then:

1. Fetches the seed page (HTML, optionally a linked PDF if part of the content).
2. Normalizes HTML → Markdown to cut token cost.
3. Pre-screens the page against your criterion. If it matches, runs structured extraction against your schema and returns.
4. If it doesn't match, scores every outgoing link by how likely it leads to a match, adds them to a frontier, and pops the highest-scoring unvisited link as the next page to fetch.
5. Repeats until a match is found or the fetch budget is exhausted.

The library is **schema-agnostic and goal-directed**. It does not ship with built-in domains like "grants" or "companies" — you bring the schema, you bring the criterion, and the agent navigates *to* the answer.

## How the agent decides

The agent maintains a **frontier** — every unvisited link it has seen so far, each annotated with an LLM-assigned relevance score against your criterion. On each step it pops the highest-scoring link, fetches and normalizes it, then pre-screens.

- If pre-screen says **match**, it runs structured extraction and returns immediately.
- If pre-screen says **not yet**, it scores the new page's outgoing links against the goal and merges them into the frontier.
- If the **fetch budget** is exhausted before any match, the agent stops and returns whatever it has, including the path it took.

This is best-first search, not breadth-first or depth-first. The LLM's relevance scoring is the only navigation policy; depth, domain scope, and per-link thresholds are deliberately **not** tunable in v0 — the budget is the single lever.

## What you provide

1. **Target schema** — a Pydantic model (or JSON schema) describing the fields you want extracted.
2. **Screening criterion** — a natural-language description of what makes a page "in scope". Used by the pre-screen *and* by the link-scorer to rank the frontier. Example: *"Page describes a grant or funding opportunity that an academic PI could apply for."*
3. **Seed URL** — a single starting point. The agent traverses outward from there.

Optional:

- **Fetch budget** — `max_fetches` (default `10`). The agent stops when it has fetched this many pages.
- **Provider / model** — defaults to OpenAI; swappable.
- **Normalization toggle** — HTML→Markdown is on by default for cost reduction.
- **Custom prompts** — override the default link-scoring, pre-screen, and extraction prompts.

## What you get back

A typed object conforming to your schema (or `None` if budget ran out before a match), plus traversal metadata:

- `data` — the extracted Pydantic instance, or `None`
- `stopped_reason` — `"match"` | `"budget_exhausted"`
- `pages_fetched` — total fetches the traversal used
- `path` — ordered list of URLs the agent visited, ending at the match (or wherever budget ran out)
- pre-screen verdict and reason for the matching page
- provider, model, and token usage across all calls

Whether the agent succeeded or gave up, the result is structured the same way — easy to audit.

## Pipeline

The four extraction stages run inside a frontier loop:

```
seed URL
   │
   ▼
   Fetch ──▶ Normalize ──▶ Pre-screen ───┐
   ▲                                     │
   │                            match? ──┴── not yet?
   │                              │            │
   │                              ▼            ▼
   │                          Extract    Score outgoing
   │                              │      links (LLM)
   │                              ▼            │
   │                           return          ▼
   │                                     add to frontier;
   │                                     pop highest-scoring
   │                                        unvisited
   │                                            │
   │                                     budget left?
   │                                       │      │
   └─────────── yes ──────────────────────┘      no
                                                  │
                                                  ▼
                                          return partial
                                          (best so far)
```

Each stage is independently swappable.

| Stage          | Notes                                                                                       |
|----------------|---------------------------------------------------------------------------------------------|
| Fetch          | Handles HTML; optionally follows linked PDFs that are part of the page                      |
| Normalize      | HTML → Markdown for token reduction; pluggable converter                                    |
| Pre-screen     | Cheap LLM call returning a binary yes/no against user-supplied criterion                    |
| Score links    | LLM scores every outgoing link's promise against the criterion; output feeds the frontier   |
| Extract        | Structured-output LLM call; provider-swappable; produces JSON conforming to user schema     |

By default the link-scorer reuses the pre-screen model — both are cheap, comparison-style calls.

## Example use cases

These are *illustrative* — the schemas and criteria belong to the caller.

**Grant opportunities.** Caller defines an `Opportunity` model (title, deadline, eligibility, sponsor, link), with criterion *"is this a grant a PI could apply for"*, and seeds the agent at a funding agency's landing page. The agent navigates the agency's site and stops on the first matching grant page within its fetch budget.

**University–industry engagement.** Caller defines a `Company` model (name, contact, engagement type), with criterion *"does this page describe company–university engagement"*, and seeds the agent at a company's homepage. The agent traverses partnership / news / about pages until it finds a matching engagement page.

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
)

result = extractor.extract(
    seed_url="https://example.gov/grants",
    max_fetches=10,            # optional; falls back to AWE_MAX_FETCHES
)
# result.data:           Opportunity | None
# result.stopped_reason: "match" | "budget_exhausted"
# result.pages_fetched:  int
# result.path:           list[str]
# result.verdict:        ScreenVerdict | None  (last screen; the matching one on success)
# result.usage:          Usage(input_tokens, output_tokens, calls)
```

Need to traverse several seed URLs and share the HTTP cache across them?

```python
results = extractor.extract_batch(
    seed_urls=["https://a.example/", "https://b.example/"],
    max_fetches=10,
)
```

### CLI

```bash
agentic-web-extraction extract \
  --schema examples/grants.py:Opportunity \
  --criteria "Page describes a grant a PI could apply for." \
  --seed-url https://example.gov/grants \
  --max-fetches 10
```

The `--schema` flag takes either a dotted import path (`my_pkg.schemas:Opportunity`) or a path to a Python file (`./schemas.py:Opportunity`) — in both cases followed by `:ClassName`. Criteria can be a quoted string or `@path/to/criteria.txt`. The CLI prints the result as JSON and exits `0` on match, `2` on budget exhaustion.

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

Settings are loaded from `.env` if present (see `.env.example`).

`AWE_MAX_FETCHES` is the only traversal knob in v0. Depth limits, per-domain scope, and link-relevance thresholds are intentionally **not** user-configurable — the budget is the single lever, and the LLM's link scoring is the navigation policy.

## Project layout

```
agentic_web_extraction/
    __init__.py          # re-exports + main() entry point
    cli.py               # Typer CLI: `extract` subcommand
    config.py            # AWE_* settings (pydantic-settings)
    extractor.py         # Extractor: frontier loop
    fetch.py             # httpx + hishel cache + tenacity retry
    frontier.py          # best-first heap + visited set
    normalize.py         # HTML→Markdown + raw-HTML link extraction
    result.py            # ExtractionResult, Usage, ScreenVerdict
    providers/
        __init__.py      # Provider protocol + factory
        openai_provider.py
examples/
    grants.py            # reference Opportunity model
pyproject.toml           # uv project, Python ≥3.13
```

## Development

```bash
uv sync
uv run agentic-web-extraction --help    # CLI help
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
- [x] `examples/` directory with reference schemas (`examples/grants.py`, kept out of the package)

Next:

- [ ] Async traversal (currently synchronous)
