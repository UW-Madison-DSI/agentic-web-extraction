"""Offline test fixtures: a stub provider and a stub web.

Nothing here touches the network or an LLM. The traversal is exercised end to end
(real normalization, real frontier, real cache-key plumbing) with `fetch.fetch`
and the provider replaced.
"""

import pytest
from pydantic import BaseModel, Field

from agentic_web_extraction.config import Settings
from agentic_web_extraction.extractor import Extractor
from agentic_web_extraction.fetch import FetchedPage
from agentic_web_extraction.result import ScreenVerdict, Usage


class Doc(BaseModel):
    """Trivial container schema -- the extraction target for these tests."""

    sources: list[str] = Field(default_factory=list)


class StubProvider:
    """Provider that screens everything in and scores every link equally.

    Deterministic and free, so a test asserts on *which pages the traversal chose
    to fetch* rather than on model behavior.
    """

    name = "stub"
    model_screen = "stub-screen"
    model_extract = "stub-extract"
    prompt_signature = "stub-v1"

    def __init__(self) -> None:
        self.usage_by_function: dict[str, Usage] = {}
        self.function_model: dict[str, str] = {}
        self.screened: list[str] = []

    def screen(self, page_md: str, criterion: str, **kwargs) -> ScreenVerdict:
        self.screened.append(kwargs.get("page_url", ""))
        return ScreenVerdict(match=True, reason="stub: everything matches")

    def score_links(
        self, links: list[tuple[str, str]], page_md: str, criterion: str, **kwargs
    ) -> list[tuple[str, float]]:
        return [(url, 0.9) for _text, url in links]

    def summarize(self, text: str, criterion: str, **kwargs) -> str:
        return text

    def extract(self, page_md: str, schema, *, usage_tag: str = "extract") -> BaseModel:
        return Doc(sources=["stub"])


class StubWeb:
    """A tiny fake web: url -> (html, resolved_url), plus a fetch log."""

    def __init__(self, pages: dict[str, str], redirects: dict[str, str] | None = None):
        self.pages = pages
        self.redirects = redirects or {}
        self.fetched: list[str] = []
        # (url, user_agent) per request, so a test can assert what went on the wire.
        self.requests: list[tuple[str, str]] = []

    def fetch(self, url: str, *, user_agent: str = "") -> FetchedPage:
        self.fetched.append(url)
        self.requests.append((url, user_agent))
        resolved = self.redirects.get(url, url)
        html = self.pages.get(resolved)
        if html is None:
            return FetchedPage(
                url=resolved,
                status=404,
                content_type="text/html",
                raw_bytes=b"",
                text="http error: status 404",
                kind="error",
            )
        return FetchedPage(
            url=resolved,
            status=200,
            content_type="text/html; charset=utf-8",
            raw_bytes=html.encode("utf-8"),
            text=html,
            kind="html",
        )


def page(*links: str) -> str:
    body = "".join(f'<a href="{href}">link to {href}</a>' for href in links)
    return f"<html><body><h1>Test page</h1><p>Body text.</p>{body}</body></html>"


@pytest.fixture
def settings() -> Settings:
    """Settings that never read the developer's environment for what matters here:
    sequential waves (deterministic fetch order), no cache file, no log file."""
    return Settings(
        max_workers=1,
        max_fetches=10,
        llm_cache="",
        log_file="",
        fetch_fallbacks="",
        user_agent="awe-test/1.0 (+https://example.edu/crawler)",
    )


@pytest.fixture
def make_extractor(settings, monkeypatch):
    """Factory: build an Extractor wired to a StubWeb and a StubProvider."""

    def factory(web: StubWeb, **kwargs) -> Extractor:
        monkeypatch.setattr("agentic_web_extraction.fetch.fetch", web.fetch)
        return Extractor(
            schema=Doc,
            criteria="anything",
            provider=StubProvider(),
            settings=kwargs.pop("settings", settings),
            cache=None,
            log_file="",
            **kwargs,
        )

    return factory
