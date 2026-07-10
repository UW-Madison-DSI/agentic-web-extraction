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
    stopped_reason: StoppedReason
    pages_fetched: int
    path: list[str]
    verdicts: list[PageVerdict]
    provider: str
    # Token usage split by call purpose (screen / score_links / extract / merge /
    # review). function_model records which model each function ran on so cost is
    # reconstructable; aggregate over functions sharing a model for a per-model view.
    usage_by_function: dict[str, Usage] = field(default_factory=dict)
    function_model: dict[str, str] = field(default_factory=dict)

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
            "provider": self.provider,
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
