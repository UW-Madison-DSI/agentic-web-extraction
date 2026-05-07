# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

v0 is implemented: traversal loop, OpenAI provider, CLI, frontier dedup, hishel cache, PDF support via markitdown. The README's "Usage" / "Configuration" sections describe the actual public API. Roadmap items checked off in README are done; unchecked items (async, more providers) are not.

Make sure README is up to date every time you finish a feature.

## Commands

Project is managed with **uv** (Python ≥3.13, build backend `uv_build`).

```bash
uv sync                        # install deps (incl. dev group)
uv run agentic-web-extraction  # run the CLI entry point
uv run ruff check              # lint
uv run ruff format             # format
uv run ty check                # type-check (Astral's ty, not mypy)
```

There is no test suite yet; if you add one, it will likely be `uv run pytest`.

## Architecture

The agent is a **best-first web traversal** over a frontier of unvisited links, where the LLM's relevance scoring is the *only* navigation policy. Per-stage notes:

- **Frontier loop** is the spine. Each iteration: pop highest-scoring unvisited link → fetch → normalize → pre-screen → branch (extract & return on match, else score outgoing links and merge into frontier). Stops on match or `max_fetches` budget exhaustion. Visited-set dedupe is required so the agent doesn't re-fetch within a traversal.
- **Four pluggable stages**: Fetch (HTML, optionally linked PDFs), Normalize (HTML→Markdown via `markitdown` for token reduction), Pre-screen (cheap binary LLM call against the user criterion), Score links (LLM ranks every outgoing link's promise), Extract (structured-output LLM call producing the user's Pydantic schema). Pre-screen and link-scorer share a model by default — both are cheap comparison calls; extraction uses a stronger model.
- **Schema-agnostic by design.** No built-in domains (no "grants" or "companies" classes). The caller supplies the Pydantic schema, the natural-language screening criterion, and the seed URL. Don't add domain-specific defaults.
- **Single-knob philosophy.** `max_fetches` (env: `AWE_MAX_FETCHES`, default 10) is the *only* traversal lever in v0. Depth caps, domain scoping, and per-link relevance thresholds are intentionally **not** user-configurable — the LLM's scoring is the policy. Don't add these knobs without an explicit ask.
- **Result shape is uniform across success and failure.** Whether the agent matched or ran out of budget, it returns the same structure: `data` (Pydantic instance or `None`), `stopped_reason` ("match" | "budget_exhausted"), `pages_fetched`, `path` (URLs visited in order), pre-screen verdict for the matching page, and provider/model/token usage. Plumbing this metadata is non-optional.

## Key dependencies and what they imply

- `httpx` + `hishel` — async HTTP with on-disk caching. Use the cache layer for fetches so re-runs during development are cheap.
- `markitdown` — the chosen HTML→Markdown converter. Keep it pluggable but it's the default.
- `openai` — default provider. The provider layer must be abstracted enough to swap (env: `AWE_PROVIDER`).
- `pydantic` + `pydantic-settings` — schemas come from callers; settings load from env (`AWE_*`, `OPENAI_*`).
- `tenacity` — for retrying flaky fetches and rate-limited LLM calls.
- `typer` — CLI. The `--schema` flag uses an `import.path:ClassName` form to load the caller's Pydantic model dynamically.

## CLI contract

```
agentic-web-extraction extract \
  --schema ./schemas.py:Opportunity \
  --criteria "..." \
  --seed-url https://... \
  --max-fetches 10
```

`--criteria` accepts either an inline string or `@path/to/file.txt`. The CLI must wire to the same `Extractor` class the Python API exposes — don't fork logic between the two entry points.

## Layout note

The package lives at the repo root (`agentic_web_extraction/`), not under `src/` — `[tool.uv.build-backend].module-root = ""` enforces this.
