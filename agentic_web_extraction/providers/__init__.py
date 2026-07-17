from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ..config import Settings
from ..result import ScreenVerdict, Usage


@runtime_checkable
class Provider(Protocol):
    # Declared as read-only properties (not bare attributes) so implementations
    # backing them with @property — as OpenAIProvider does — satisfy the protocol.
    @property
    def name(self) -> str: ...

    @property
    def model_screen(self) -> str: ...

    @property
    def model_extract(self) -> str: ...

    # A stable fingerprint of every prompt template the provider sends (screen,
    # score, extract, and any conditional appendices). Mixed into the page-cache
    # key so editing a prompt auto-invalidates stale cached outcomes. Optional:
    # the Extractor treats a missing implementation as an empty signature.
    @property
    def prompt_signature(self) -> str: ...

    # Token usage bucketed by call-purpose tag, and the model each tag ran on.
    @property
    def usage_by_function(self) -> dict[str, Usage]: ...

    @property
    def function_model(self) -> dict[str, str]: ...

    def screen(
        self,
        page_md: str,
        criterion: str,
        *,
        page_url: str | None = None,
        seed_url: str | None = None,
        on_seed_domain: bool | None = None,
    ) -> ScreenVerdict: ...

    def score_links(
        self,
        links: list[tuple[str, str]],
        page_md: str,
        criterion: str,
        *,
        seed_url: str | None = None,
        on_seed_domain: dict[str, bool | None] | None = None,
    ) -> list[tuple[str, float]]: ...

    def extract(
        self, page_md: str, schema: type[BaseModel], *, usage_tag: str = "extract"
    ) -> BaseModel: ...


def get_provider(settings: Settings) -> Provider:
    name = settings.provider.lower()
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings=settings)
    raise ValueError(f"unknown provider: {settings.provider!r}")
