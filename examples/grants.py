"""Reference schema + runnable example for grant extraction.

Run as a script (uses the defaults below):

    uv run python examples/grants.py

Run via the CLI (same schema, override seed/criteria as needed):

    uv run awe extract \\
        --schema examples/grants.py:Opportunity \\
        --criteria "Page describes a grant or funding opportunity an academic PI could apply for." \\
        --seed-url https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8 \\
        --max-fetches 5
"""

import json
import sys

from pydantic import BaseModel

DEFAULT_SEED_URL = "https://simpler.grants.gov/opportunity/24a2e68b-9105-4fc8-8432-7ddff3e3afb8"
DEFAULT_CRITERIA = (
    "Page describes a single grant or funding opportunity an academic PI could apply for, "
    "with title, deadline, eligibility, and sponsor information."
)
DEFAULT_MAX_FETCHES = 5


class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str


def main() -> int:
    from agentic_web_extraction import Extractor

    extractor = Extractor(schema=Opportunity, criteria=DEFAULT_CRITERIA)
    result = extractor.extract(seed_url=DEFAULT_SEED_URL, max_fetches=DEFAULT_MAX_FETCHES)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.stopped_reason == "match" else 2


if __name__ == "__main__":
    sys.exit(main())
