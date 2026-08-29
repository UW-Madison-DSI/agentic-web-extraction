# Contributing

Thanks for looking. This is a small library with strong opinions about a few
things; most of this document is about which those are, so a good change doesn't
get held up over something nobody wrote down.

## Getting set up

Python ≥3.13, managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync            # install deps, including the dev group
uv run pytest      # tests (offline: stub provider + stub web, no network, no LLM)
uv run ruff check  # lint
uv run ruff format # format
uv run ty check    # type-check (Astral's ty, not mypy)
```

`uv run awe extract --help` shows the CLI; `uv run awe schema` prints the JSON
Schema of every `AWE_*` setting.

Run all four before opening a pull request. There is no CI gate that will catch
a formatting slip for you.

## Tests are offline, and must stay that way

[`tests/conftest.py`](tests/conftest.py) supplies a `StubProvider` (screens
everything in, scores every link 0.9) and a `StubWeb` (a `url -> html` dict, a
redirect map, and a fetch log). A test therefore asserts on **which pages the
traversal chose to fetch**, which is the interesting thing about this library and
the thing a mocked HTTP client alone would not tell you.

Nothing in `tests/` may reach the network or a real model. That includes
indirectly: `tiktoken` downloads its encoding table on first use, so a test that
exercises the summarization path should stub `count_tokens` / `split_by_tokens`
(the `fake_tokens` fixture does this) rather than let a real encoding load.

If a change can only be verified against a live site or a live model, it belongs
in a script you run by hand, not in the suite.

## Conventions worth knowing before you write the patch

These are the places where the obvious change is the wrong one. Each is
explained at length in [CLAUDE.md](CLAUDE.md); the short version:

- **The library is schema-agnostic.** The caller brings the Pydantic schema, the
  natural-language criterion, and the seed URLs. No built-in domains, no
  "grants" or "companies" classes, no site-specific text munging in
  `normalize.py` — caller-supplied `text_filters` are where that goes.
- **LLM relevance scoring is the navigation policy.** Please don't add depth
  caps or per-link score thresholds without discussing it first; the fetch budget
  is the intended lever. `max_workers` is concurrency, not relevance.
- **The crawl boundary is enforced at `frontier.push`, and nowhere else.**
  Not in the worker (that would bake the current allowed set into the cached link
  scores and replay it under a different boundary later) and not in `fetch.py`
  (httpx follows redirects inside one call, so filtering requests would break
  every site that has moved host).
- **Nothing but a 2xx body is content.** An error page served as `text/html` is
  not the page. Recovery (`fallback.py`) is retrieval-only: it must not learn
  about content selection, normalization, or any particular site.
- **robots.txt fails open, deliberately** — the opposite of RFC 9309's
  suggestion for the 5xx case, because an origin's brief 500 would otherwise
  empty an authorized crawl behind a line that reads like the site's own policy.
  If you move that code, move the reasoning with it.
- **Never a bare `print`.** Diagnostics go through `logsink.emit`; stdout is
  reserved for result JSON. Lines carry a bracketed `[tag]` prefix, which is what
  the `on_event` subscriber uses to classify them.
- **Comments carry the reasoning, not the mechanics.** The code says what it
  does. A comment should say why the obvious alternative was rejected — most of
  this file's rules exist because somebody tried the obvious thing first.

## Defaults

A new setting defaults **on** only when it can neither lose the caller data nor
spend extra requests at a third party. Everything else ships off, documented, and
opt-in. `request_delay` is on because being impolite is a defect whose cost lands
on somebody else; `main_content_only` is off because it is lossy in the caller's
own results.

## Releases

Don't hand-edit `version` in `pyproject.toml`. The git tag *is* the release, and
[`scripts/release.py`](scripts/release.py) is the only thing that writes the
version — it requires `main`, a clean tree, exact parity with the remote, and a
non-empty `## Unreleased` section in [CHANGELOG.md](CHANGELOG.md). Add your entry
under `## Unreleased` as part of the change.

Don't hand-edit the `<!-- adopters:start -->` block in the README either; it is
regenerated weekly by [`scripts/adopters.py`](scripts/adopters.py).

## Pull requests

Say what breaks if the change is wrong. A patch that changes a default, a cache
key, or where a policy is enforced should say so in its own first paragraph —
those are the three things that cause quiet damage here, and the review is much
faster when you name it yourself.
