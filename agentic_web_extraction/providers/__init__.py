from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ..config import Settings
from ..result import ScreenVerdict, Usage


@runtime_checkable
class Provider(Protocol):
    name: str
    model_screen: str
    model_extract: str

    @property
    def usage(self) -> Usage: ...

    def screen(self, page_md: str, criterion: str) -> ScreenVerdict: ...

    def score_links(
        self,
        links: list[tuple[str, str]],
        page_md: str,
        criterion: str,
    ) -> list[tuple[str, float]]: ...

    def extract(self, page_md: str, schema: type[BaseModel]) -> BaseModel: ...


def get_provider(settings: Settings) -> Provider:
    name = settings.provider.lower()
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings=settings)
    raise ValueError(f"unknown provider: {settings.provider!r}")
