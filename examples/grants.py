"""Reference schema for the README's grants example.

Run from the repo root:

    uv run agentic-web-extraction extract \\
        --schema examples/grants.py:Opportunity \\
        --criteria "Page describes a grant or funding opportunity an academic PI could apply for." \\
        --seed-url https://www.nsf.gov/funding/ \\
        --max-fetches 10
"""

from __future__ import annotations

from pydantic import BaseModel


class Opportunity(BaseModel):
    title: str
    deadline: str | None = None
    eligibility: str | None = None
    sponsor: str | None = None
    link: str
