from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

StoppedReason = Literal["match", "budget_exhausted"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    # Subset of input_tokens served from the provider's prompt cache (billed at
    # a discount). Uncached input = input_tokens - cached_input_tokens.
    cached_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass
class ScreenVerdict:
    match: bool
    reason: str


@dataclass
class PageVerdict:
    """A screen verdict tagged with the URL it was made on."""

    url: str
    match: bool
    reason: str


@dataclass
class ExtractionResult:
    data: BaseModel | None
    """The extracted result: a ``schema`` instance merged across every matching
    page (or the single match's data when ``stop_on_first_match``), or ``None``
    when no page matched (``stopped_reason == "budget_exhausted"``)."""

    stopped_reason: StoppedReason
    """Why the traversal ended: ``"match"`` if at least one page matched (even
    in gather-all mode, which continues past the first), else
    ``"budget_exhausted"`` when the frontier emptied or ``max_fetches`` ran out
    with no match."""

    pages_fetched: int
    """Count of *readable* pages that consumed a ``max_fetches`` budget slot —
    HTML/PDF bodies the agent did LLM work on. Fetches that errored or returned
    a non-HTML/PDF body are in ``path`` but excluded here."""

    path: list[str]
    """Resolved URLs visited in traversal order, including error/skipped fetches
    that did not consume budget. Deduped on the post-redirect URL."""

    verdicts: list[PageVerdict]
    """One ``PageVerdict`` (url, match, reason) per screened page — the full
    screening record, not just the matches."""

    protocol: str
    """Name of the provider adapter / wire protocol that ran the crawl (e.g.
    ``"openai"``). Names the SDK/billing surface, NOT the model vendor — an
    OpenAI-compatible endpoint may serve a non-OpenAI model. Pair with each
    bucket's model in ``usage_by_function`` to reconstruct cost."""

    usage_by_function: dict[str, Usage] = field(default_factory=dict)
    """Token usage split by call purpose (``screen`` / ``score_links`` /
    ``extract`` / ``merge`` / any tag a caller passes to ``extract``). Sum the
    values for a grand total."""

    function_model: dict[str, str] = field(default_factory=dict)
    """Which model each function bucket in ``usage_by_function`` ran on, so cost
    is reconstructable; aggregate over functions sharing a model for a per-model
    view."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.data.model_dump(mode="json")
            if self.data is not None
            else None,
            "stopped_reason": self.stopped_reason,
            "pages_fetched": self.pages_fetched,
            "path": list(self.path),
            "verdicts": [
                {"url": v.url, "match": v.match, "reason": v.reason}
                for v in self.verdicts
            ],
            "protocol": self.protocol,
            "usage_by_function": {
                func: {
                    "model": self.function_model.get(func),
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "calls": u.calls,
                    "cached_input_tokens": u.cached_input_tokens,
                }
                for func, u in self.usage_by_function.items()
            },
        }
