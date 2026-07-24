"""Reference schema + runnable example for grant extraction.

The crawler screens pages during a best-first traversal, then concatenates the
markdown of *every* screened-in page (across all seeds) and runs a **single**
structured extraction over the whole thing — so `Opportunities` is just a list
container the one extraction fills. No per-page extraction, no merge/dedup step.

LLM-call caching is on by default (SQLite at ``AWE_LLM_CACHE``), so a second run
over unchanged pages replays every screen/score result and the consolidated
extraction with no LLM calls -- nothing to wire up here; see ``main``.

Run as a script (uses the defaults below):

    uv run python examples/grants.py

Run via the CLI (same schema, override seed/criteria as needed; repeat
``--seed-url`` to pool several seeds into one extraction):

    uv run awe extract \\
        --schema examples/grants.py:Opportunities \\
        --criteria "Page describes one or more grant or funding opportunities an academic PI could apply for." \\
        --seed-url https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8 \\
        --max-fetches 5
"""

from __future__ import annotations

import json
import sys

from pydantic import BaseModel, Field

DEFAULT_SEED_URL = (
    "https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8"
)
DEFAULT_CRITERIA = (
    "Page describes one or more grant or funding opportunities an academic PI could apply for, "
    "with title, deadline, eligibility, and sponsor information."
)
DEFAULT_MAX_FETCHES = 5


class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str


class Opportunities(BaseModel):
    """Extraction container: every opportunity found across the crawl.

    Passing this collection schema (not the singular ``Opportunity``) to the
    Extractor is what lets the single consolidated extraction return many
    opportunities — the structured-output call fills one ``Opportunities`` object
    whose ``items`` list holds every opportunity it found in the concatenated
    content of all screened-in pages.
    """

    items: list[Opportunity] = Field(
        default_factory=list,
        description="All distinct grant/funding opportunities described in the content.",
    )


def main() -> int:
    from agentic_web_extraction import Extractor

    try:
        # When run as a script (`uv run python examples/grants.py`), the examples
        # dir is on sys.path, so `strippers` is importable top-level.
        from strippers import CACHE_STABILITY_FILTERS
    except ImportError:
        # When imported as part of the `examples` package (repo root on path).
        from examples.strippers import CACHE_STABILITY_FILTERS

    # `text_filters` is where a caller injects the site-specific cache-stability
    # strippers the (agnostic) library no longer ships. They're harmless on
    # sites they don't match, so passing the whole bundle is fine.
    #
    # LLM-call caching is ON by default -- a SQLite store at AWE_LLM_CACHE
    # (data/llm_cache.sqlite). The first run populates it; re-running this script
    # replays screen/link-score outputs for unchanged pages, and the consolidated
    # extraction too when every contributing page hit the cache, with no LLM calls
    # (watch `usage_by_function` drop to zero `calls` on the second run). Delete
    # data/llm_cache.sqlite to force a cold crawl, or pass `cache=None` to disable.
    extractor = Extractor(
        schema=Opportunities,
        criteria=DEFAULT_CRITERIA,
        text_filters=CACHE_STABILITY_FILTERS,
    )
    # `extract` also accepts a list of seed URLs to pool more content into the one
    # extraction, e.g. extractor.extract([url_a, url_b], max_fetches=5).
    result = extractor.extract(DEFAULT_SEED_URL, max_fetches=DEFAULT_MAX_FETCHES)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.stopped_reason == "match" else 2


if __name__ == "__main__":
    sys.exit(main())
