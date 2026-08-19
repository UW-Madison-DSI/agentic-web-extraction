"""Offline test fixtures: a stub provider and a stub web.

Nothing here touches the network or an LLM. The traversal is exercised end to end
(real normalization, real frontier, real cache-key plumbing) with `fetch.fetch`
and the provider replaced.
"""

import threading

import pytest
from pydantic import BaseModel, Field

from agentic_web_extraction import fallback, fetch
from agentic_web_extraction.config import Settings
from agentic_web_extraction.extractor import Extractor
from agentic_web_extraction.fallback import Recovered
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


class Route:
    """A recovery route that records its calls and returns a fixed result."""

    def __init__(self, recovered: Recovered | None) -> None:
        self.recovered = recovered
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, user_agent: str = "") -> Recovered | None:
        self.calls.append((url, user_agent))
        return self.recovered


@pytest.fixture(autouse=True)
def clean_transport_memo():
    """Forget written-off hosts around every test.

    `fetch`'s memo is process-wide, like the http clients it sits beside, so a test
    that proves a host silent would otherwise decide how the next test's fetches are
    routed.
    """
    fetch.reset_transport_memo()
    yield
    fetch.reset_transport_memo()


class FakeImpersonateResponse:
    """The slice of a curl_cffi response the impersonate route reads."""

    def __init__(
        self,
        status_code: int = 200,
        body: bytes = b"<html><body>recovered</body></html>",
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.headers = {"content-type": content_type}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


class FakeImpersonateSession:
    """Stand-in for a curl_cffi Session, recording what went out on the wire."""

    def __init__(self, target: str) -> None:
        self.target = target
        # (url, headers, timeout) per request.
        self.calls: list[tuple[str, dict[str, str] | None, float | None]] = []
        self.response = FakeImpersonateResponse()

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, headers, timeout))
        return self.response


@pytest.fixture
def fake_impersonate(monkeypatch):
    """Replace the route's session factory; hand back the sessions it built.

    Covers the route without curl_cffi installed, and keeps the thread-local
    session cache from leaking sessions between tests.
    """
    built: list[FakeImpersonateSession] = []

    def factory(target: str) -> FakeImpersonateSession:
        session = FakeImpersonateSession(target)
        built.append(session)
        return session

    monkeypatch.setattr(fallback, "_sessions", threading.local())
    monkeypatch.setattr(fallback, "_new_session", factory)
    return built


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
