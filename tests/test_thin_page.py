"""The 200 that isn't a page.

A single-page app answers 200 with a shell whose text arrives later from
JavaScript we do not run. It sails past the status guard, normalizes to almost
nothing, gets screened out as irrelevant, and leaves a log saying nothing went
wrong -- which made it the one failure mode with no trigger and no trace. This is
that trigger, and the guard that stops it from making things worse.
"""

import httpx
import pytest

from agentic_web_extraction import fallback, fetch
from agentic_web_extraction.config import Settings
from agentic_web_extraction.fallback import Recovered

from .conftest import Route

URL = "https://shell-test.org/app"
SHELL = '<html><body><div id="root"></div><script>boot()</script></body></html>'
REAL = "<html><body><article>" + ("Real prose. " * 60) + "</article></body></html>"


def serving(body: str, content_type: str = "text/html; charset=utf-8") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def wired(monkeypatch):
    """Serve `body` from the origin and put `route` in the recovery chain."""

    def wire(
        body: str,
        route: Route | None = None,
        content_type: str = "text/html; charset=utf-8",
        **overrides,
    ):
        settings = Settings(
            llm_cache="",
            log_file="",
            request_delay=0.0,
            fetch_fallbacks="jina" if route is not None else "",
            **overrides,
        )
        monkeypatch.setattr(fetch, "get_settings", lambda: settings)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        monkeypatch.setattr(fetch, "get_client", lambda: serving(body, content_type))
        if route is not None:
            monkeypatch.setitem(fallback._ROUTES, "jina", route)
        return route

    return wire


def recovered(body: str, content_type: str = "text/html; charset=utf-8") -> Recovered:
    return Recovered(
        raw_bytes=body.encode("utf-8"),
        text=body,
        content_type=content_type,
        via="jina",
    )


def test_a_zero_threshold_returns_a_thin_page_as_it_is(wired):
    """The pre-0.3 behaviour, one setting away: the check costs requests at a
    third party, so a deployment that would rather keep whatever the origin
    served zeroes it."""
    route = wired(SHELL, Route(recovered(REAL)), min_page_text_chars=0)

    page = fetch.fetch(URL)

    assert route.calls == []
    assert page.text == SHELL
    assert page.via == ""


def test_a_thin_page_is_sent_through_recovery_by_default(wired):
    """200 characters is the shipped threshold, and it matches
    `min_recovered_text_chars` so one number means "this is the page" on both
    sides of the chain. Passed explicitly here, and in the tests below, so each
    one reads without the default in hand."""
    assert Settings.model_fields["min_page_text_chars"].default == 200

    wired(SHELL, Route(recovered(REAL)), min_page_text_chars=200)

    page = fetch.fetch(URL)

    assert "Real prose." in page.text
    assert page.via == "jina"


def test_a_page_with_enough_text_is_never_re_fetched(wired):
    route = wired(REAL, Route(recovered(REAL)), min_page_text_chars=200)

    page = fetch.fetch(URL)

    assert route.calls == []
    assert page.via == ""


SHORT_BUT_REAL = (
    "<html><body><p>Closed. Applications reopen in the fall.</p></body></html>"
)


def test_a_genuinely_short_page_is_not_replaced_by_a_thinner_rendering(wired):
    """The case the guard exists for. A short page is indistinguishable from a
    shell by character count, so this threshold will fire on real pages -- and
    `fallback.recover` compares routes against each other, never against the page
    we already hold, so its "fullest sub-threshold body" could be worse than what
    the origin served. Turning the threshold on must only ever be able to improve
    what comes back."""
    wired(
        SHORT_BUT_REAL,
        Route(recovered("<html><body>tiny</body></html>")),
        min_page_text_chars=200,
    )

    page = fetch.fetch(URL)

    assert page.text == SHORT_BUT_REAL
    assert page.via == ""


def test_a_fuller_rendering_wins_even_below_the_threshold(wired):
    """The comparison is against the origin, not against the threshold: a render
    that beats an empty shell without clearing 200 characters is still the better
    of the two bodies, and discarding it would throw away the improvement the
    setting was turned on to get."""
    better = "<html><body><p>" + ("word " * 20) + "</p></body></html>"
    wired(SHELL, Route(recovered(better)), min_page_text_chars=200)

    page = fetch.fetch(URL)

    assert "word" in page.text
    assert page.via == "jina"


def test_a_route_that_declines_leaves_the_thin_page_intact(wired):
    """Recovery is a chance to do better, never a reason to lose the page."""
    wired(SHELL, Route(None), min_page_text_chars=200)

    page = fetch.fetch(URL)

    assert page.text == SHELL
    assert page.kind == "html"


def test_a_route_answering_with_a_non_page_is_ignored(wired):
    """A PDF for a URL the origin served as HTML has not rendered the page."""
    wired(
        SHELL,
        Route(recovered("%PDF-1.4 ...", content_type="application/pdf")),
        min_page_text_chars=200,
    )

    page = fetch.fetch(URL)

    assert page.text == SHELL
    assert page.via == ""


def test_the_recovered_page_keeps_the_callers_url(wired):
    """Same contract as every other recovery: the body comes back under the URL
    the caller asked for, never the reader's address, so `path` and any citation
    derived from it stay canonical."""
    wired(SHELL, Route(recovered(REAL)), min_page_text_chars=200)

    assert fetch.fetch(URL).url == URL


def test_a_pdf_is_not_measured_for_visible_text(wired):
    """PDFs carry their content in `raw_bytes` and are empty in `text` by
    contract, so measuring them would send every PDF ever fetched through the
    recovery chain."""
    route = wired(
        "%PDF-1.4 fake",
        Route(recovered(REAL)),
        content_type="application/pdf",
        min_page_text_chars=200,
    )

    page = fetch.fetch(URL)

    assert page.kind == "pdf"
    assert route.calls == []
