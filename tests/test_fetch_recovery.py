"""Recovery is reached from a transport failure, not only from a bad status.

The incident: a crawl with `AWE_FETCH_FALLBACKS=jina` configured lost its only
seed to an edge CDN that tarpits non-browser clients. No response ever arrived,
so `fetch` short-circuited to kind="error" and the recovery route — which could
have read the page — was never called. A site that refuses by going silent had
been getting a *better* outcome than one that refuses with a 403.
"""

from dataclasses import replace

import httpx
import pytest

from agentic_web_extraction import fallback, fetch
from agentic_web_extraction.config import Settings
from agentic_web_extraction.fallback import Recovered

from .conftest import StubWeb, page

URL = "https://tarpit-test.org/corporate/index.html"
BODY = "<html><body><h1>The page itself</h1></body></html>"


def timing_out(exc: Exception | None = None):
    """A client whose every request dies in transport, with no response at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc or httpx.ReadTimeout("connection tarpitted", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


class Route:
    """A recovery route that records its calls."""

    def __init__(self, recovered: Recovered | None) -> None:
        self.recovered = recovered
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, user_agent: str = "") -> Recovered | None:
        self.calls.append((url, user_agent))
        return self.recovered


def html(body: str = BODY) -> Recovered:
    return Recovered(
        raw_bytes=body.encode("utf-8"),
        text=body,
        content_type="text/html; charset=utf-8",
        via="jina",
    )


@pytest.fixture
def wired(monkeypatch):
    """Point fetch + fallback at one Settings and a transport that never answers.

    `fetch_attempts=1` keeps the retry backoff out of the test's wall clock; the
    attempt policy itself is covered separately below.
    """

    def configure(
        route: Route | None = None, *, exc: Exception | None = None, **updates
    ) -> Route:
        settings = Settings(
            fetch_attempts=1, fetch_fallbacks="jina", llm_cache="", log_file=""
        ).model_copy(update=updates)
        monkeypatch.setattr(fetch, "get_settings", lambda: settings)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        monkeypatch.setattr(fetch, "get_client", lambda: timing_out(exc))
        route = route if route is not None else Route(html())
        monkeypatch.setitem(fallback._ROUTES, "jina", route)
        return route

    return configure


def test_transport_failure_is_handed_to_the_configured_route(wired):
    route = wired()

    result = fetch.fetch(URL, user_agent="awe-test/1.0")

    # The regression assertion: the route ran at all.
    assert route.calls == [(URL, "awe-test/1.0")]
    assert result.kind == "html"
    assert result.text == BODY
    assert result.via == "jina"
    # Returned under the requested URL — nothing resolved it, and a recovered page
    # is never reported under a proxy address.
    assert result.url == URL


def test_recovery_disabled_leaves_the_error_page(wired):
    route = wired(fetch_fallbacks="")

    result = fetch.fetch(URL)

    assert route.calls == []
    assert result.kind == "error"


def test_every_route_declining_preserves_the_error_contract(wired):
    route = wired(Route(None))

    result = fetch.fetch(URL)

    assert route.calls
    assert result.kind == "error"
    assert result.status == 0
    assert result.content_type == ""
    assert "ReadTimeout" in result.text


def test_a_pdf_url_spends_no_recovery_request_when_pdfs_are_off(wired):
    """`_recover`'s pre-check, now reachable from a second call site."""
    route = wired(follow_pdf=False)

    result = fetch.fetch("https://tarpit-test.org/report.pdf")

    assert route.calls == []
    assert result.kind == "error"


def test_a_non_httpx_failure_recovers_too(wired):
    """The handler catches bare Exception (a malformed header can raise anything),
    and recovery is about having no content, not about which library complained."""
    route = wired(exc=UnicodeEncodeError("utf-8", "☃", 0, 1, "bad Location header"))

    result = fetch.fetch(URL)

    assert route.calls
    assert result.kind == "html"


def test_read_timeouts_are_not_retried_three_times(monkeypatch):
    """A tarpit is deterministic: attempts 2 and 3 buy nothing and cost 30s each.

    Connect errors and 5xx keep the full budget — those genuinely are transient.
    """
    settings = Settings(fetch_attempts=3, llm_cache="", log_file="")
    monkeypatch.setattr(fetch, "get_settings", lambda: settings)

    assert fetch._attempts_for(httpx.ReadTimeout("tarpit")) == 2
    assert fetch._attempts_for(httpx.ConnectError("refused")) == 3
    assert fetch._attempts_for(None) == 3


def test_the_attempt_budget_is_configurable(monkeypatch):
    settings = Settings(fetch_attempts=5, llm_cache="", log_file="")
    monkeypatch.setattr(fetch, "get_settings", lambda: settings)

    assert fetch._attempts_for(httpx.ConnectError("refused")) == 5
    # Still capped: the setting raises the transient budget, not the tarpit one.
    assert fetch._attempts_for(httpx.ReadTimeout("tarpit")) == 2


# --- through the traversal --------------------------------------------------

SEED = "https://tarpit-test.org/"


class RecoveredWeb(StubWeb):
    """A stub web whose pages all arrive by way of a recovery route."""

    def fetch(self, url: str, *, user_agent: str = ""):
        return replace(super().fetch(url, user_agent=user_agent), via="jina")


def test_via_reaches_the_result(make_extractor):
    web = RecoveredWeb({SEED: page()})
    extractor = make_extractor(web)

    result = extractor.extract(SEED)

    assert result.fallbacks_used == {SEED: "jina"}
