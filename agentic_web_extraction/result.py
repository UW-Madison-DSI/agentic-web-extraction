from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

StoppedReason = Literal["match", "budget_exhausted"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
        )


@dataclass
class ScreenVerdict:
    match: bool
    reason: str


@dataclass
class ExtractionResult:
    data: BaseModel | None
    stopped_reason: StoppedReason
    pages_fetched: int
    path: list[str]
    verdict: ScreenVerdict | None
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.data.model_dump(mode="json") if self.data is not None else None,
            "stopped_reason": self.stopped_reason,
            "pages_fetched": self.pages_fetched,
            "path": list(self.path),
            "verdict": (
                {"match": self.verdict.match, "reason": self.verdict.reason}
                if self.verdict is not None
                else None
            ),
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "calls": self.usage.calls,
            },
        }
