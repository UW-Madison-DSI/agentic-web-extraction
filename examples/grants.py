"""Reference schema + runnable example for grant extraction.

Demonstrates the optional ``merge_extractions`` hook. The seed page and the
pages it links to each describe a grant, so a gather-all traversal produces
several matches; ``Opportunities.merge_extractions`` folds them into one
deduplicated result using the LLM (with a pure-Python fallback).

Run as a script (uses the defaults below):

    uv run python examples/grants.py

Run via the CLI (same schema, override seed/criteria as needed):

    uv run awe extract \\
        --schema examples/grants.py:Opportunities \\
        --criteria "Page describes one or more grant or funding opportunities an academic PI could apply for." \\
        --seed-url https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8 \\
        --max-fetches 5
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agentic_web_extraction.cache import KVCache
    from agentic_web_extraction.providers import Provider

DEFAULT_SEED_URL = "https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8"
DEFAULT_CRITERIA = (
    "Page describes one or more grant or funding opportunities an academic PI could apply for, "
    "with title, deadline, eligibility, and sponsor information."
)
DEFAULT_MAX_FETCHES = 5

_DEDUP_INSTRUCTIONS = (
    "TASK: The RECORDS below are grant/funding opportunities extracted from several "
    "pages of one crawl. Some records describe the SAME underlying opportunity seen "
    "on different pages (e.g. a Grants.gov listing and the sponsor's own announcement, "
    "or the same program across years). Return EXACTLY ONE canonical record per "
    "distinct opportunity. When collapsing duplicates, prefer non-null and more "
    "specific values, keep the sponsor's own page as `link` when available, and keep "
    "titles concise. Do NOT invent opportunities not present in the input."
)


class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str


class Opportunities(BaseModel):
    """Per-page extraction container: every opportunity found on one page.

    Passing this collection schema (not the singular ``Opportunity``) to the
    Extractor is what lets a single page yield many opportunities — the
    structured-output call fills one ``Opportunities`` object whose ``items``
    list holds them all. Across a gather-all traversal you then get one
    ``Opportunities`` per matching page, which ``merge_extractions`` fuses.
    """

    items: list[Opportunity] = Field(
        default_factory=list,
        description="All distinct grant/funding opportunities described on the page.",
    )

    @classmethod
    def merge_extractions(
        cls,
        matches: list[tuple[str, Opportunities]],
        *,
        provider: Provider | None = None,
        cache: KVCache | None = None,
    ) -> Opportunities:
        """Fold every matching page's opportunities into one deduped collection.

        LLM-based: flatten all per-page opportunities, then ask the extraction
        model to collapse records that describe the same underlying opportunity.
        The dedup call is tagged ``usage_tag="merge"`` so its tokens show up under
        a ``"merge"`` bucket in ``result.usage_by_function``.

        Falls back to a cheap deterministic dedup (by ``link``, backfilling null
        fields) when no provider is supplied, when there is nothing to reconcile,
        or when the LLM call fails.
        """
        flat = [opp for _url, extracted in matches for opp in extracted.items]
        if provider is None or len(flat) <= 1:
            return cls(items=_dedup_by_link(flat))

        payload = (
            f"{_DEDUP_INSTRUCTIONS}\n\nRECORDS ({len(flat)}):\n"
            + json.dumps([o.model_dump() for o in flat], indent=2, ensure_ascii=False)
        )
        try:
            merged = provider.extract(payload, cls, usage_tag="merge")
        except Exception as e:  # noqa: BLE001 - degrade to deterministic dedup
            print(f"  [merge] LLM dedup failed ({type(e).__name__}); using URL dedup", file=sys.stderr)
            return cls(items=_dedup_by_link(flat))
        if isinstance(merged, cls):
            return merged
        return cls(items=_dedup_by_link(flat))


def _dedup_by_link(opps: list[Opportunity]) -> list[Opportunity]:
    """Deterministic fallback: collapse by ``link``, backfilling null/empty fields."""
    by_link: dict[str, Opportunity] = {}
    for opp in opps:
        existing = by_link.get(opp.link)
        if existing is None:
            by_link[opp.link] = opp
            continue
        filled = existing.model_dump()
        for field, value in opp.model_dump().items():
            if filled.get(field) in (None, "") and value not in (None, ""):
                filled[field] = value
        by_link[opp.link] = Opportunity(**filled)
    return list(by_link.values())


def main() -> int:
    from agentic_web_extraction import Extractor

    extractor = Extractor(schema=Opportunities, criteria=DEFAULT_CRITERIA)
    result = extractor.extract(seed_url=DEFAULT_SEED_URL, max_fetches=DEFAULT_MAX_FETCHES)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.stopped_reason == "match" else 2


if __name__ == "__main__":
    sys.exit(main())
