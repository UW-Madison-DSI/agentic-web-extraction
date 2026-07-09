"""Reference schema + runnable example for grant extraction.

Demonstrates the optional ``merge_extractions`` hook. The seed page and the
pages it links to each describe a grant, so a gather-all traversal produces
several matches; ``Opportunities.merge_extractions`` folds them into one
deduplicated result (pure Python — no extra LLM calls).

Run as a script (uses the defaults below):

    uv run python examples/grants.py

Run via the CLI (same schema, override seed/criteria as needed):

    uv run awe extract \\
        --schema examples/grants.py:Opportunities \\
        --criteria "Page describes one or more grant or funding opportunities an academic PI could apply for." \\
        --seed-url https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8 \\
        --max-fetches 5
"""

import json
import sys

from pydantic import BaseModel, Field

DEFAULT_SEED_URL = "https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8"
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
        matches: list[tuple[str, "Opportunities"]],
        *,
        provider: object | None = None,
        cache: object | None = None,
    ) -> "Opportunities":
        """Fold every matching page's opportunities into one deduped collection.

        Pure Python — ``provider`` and ``cache`` are accepted (the crawler always
        passes them) but unused here; a schema that wanted an LLM-reconciled merge
        would call ``provider.extract(..., usage_tag="merge")`` instead.

        Dedup is by ``link``: the first time a link is seen its record is kept,
        and later duplicates only backfill fields that were null/empty, so a
        sparse extraction on one page is completed by a richer one on another.
        """
        by_link: dict[str, Opportunity] = {}
        for _url, extracted in matches:
            for opp in extracted.items:
                existing = by_link.get(opp.link)
                if existing is None:
                    by_link[opp.link] = opp
                    continue
                filled = existing.model_dump()
                for field, value in opp.model_dump().items():
                    if filled.get(field) in (None, "") and value not in (None, ""):
                        filled[field] = value
                by_link[opp.link] = Opportunity(**filled)
        return cls(items=list(by_link.values()))


def main() -> int:
    from agentic_web_extraction import Extractor

    extractor = Extractor(schema=Opportunities, criteria=DEFAULT_CRITERIA)
    result = extractor.extract(seed_url=DEFAULT_SEED_URL, max_fetches=DEFAULT_MAX_FETCHES)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.stopped_reason == "match" else 2


if __name__ == "__main__":
    sys.exit(main())
