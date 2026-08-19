"""A recovered body only wins if it is plausibly the page.

The incident: `impersonate` (raw HTML, no JS) answered `kohlercompany.com`'s
homepage with a 554-byte client-rendered shell. Being a non-None `Recovered`, it
ended the chain — so `jina`, which renders server-side and produced 147KB for the
same URL, was never asked. "A body arrived" is not "the page was obtained".

The threshold reorders the chain; it never loses a page. If no route clears it, the
fullest body obtained is still returned.
"""

import pytest

from agentic_web_extraction import fallback
from agentic_web_extraction.config import Settings
from agentic_web_extraction.fallback import Recovered

from .conftest import Route

URL = "https://spa-test.org/"

# What a framework serves before its JS runs: all markup and script, no prose.
SHELL = (
    "<html><head><title>Home</title>"
    "<script>window.__STATE__={a:1};/* many kilobytes of bundle */</script>"
    "<style>.root{display:flex}</style></head>"
    '<body><div id="root"></div><!-- app mounts here --></body></html>'
)
FULL = "<html><body><p>" + "Real content about the thing. " * 40 + "</p></body></html>"


def recovered(body: str, via: str, content_type: str = "text/html; charset=utf-8"):
    return Recovered(
        raw_bytes=body.encode("utf-8"), text=body, content_type=content_type, via=via
    )


@pytest.fixture
def chain(monkeypatch):
    """Wire `AWE_FETCH_FALLBACKS` to a set of recording routes, in order."""

    def configure(*routes: tuple[str, Recovered | None], **updates) -> list[Route]:
        settings = Settings(
            llm_cache="",
            log_file="",
            fetch_fallbacks=",".join(name for name, _ in routes),
        ).model_copy(update=updates)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        recorders = []
        for name, result in routes:
            recorder = Route(result)
            monkeypatch.setitem(fallback._ROUTES, name, recorder)
            recorders.append(recorder)
        return recorders

    return configure


def test_visible_text_ignores_markup_script_and_style():
    assert fallback.visible_text(SHELL) == "Home"
    assert "Real content" in fallback.visible_text(FULL)
    # Entities survive as the characters a reader sees.
    assert fallback.visible_text("<p>a &amp; b</p>") == "a & b"


def test_a_shell_falls_through_to_the_rendering_route(chain):
    """The regression: the second route is asked, and its body is what comes back."""
    shell_route, render_route = chain(
        ("impersonate", recovered(SHELL, "impersonate:chrome")),
        ("jina", recovered(FULL, "jina")),
    )

    result = fallback.recover(URL, user_agent="awe-test/1.0")

    assert shell_route.calls == [(URL, "awe-test/1.0")]
    assert render_route.calls == [(URL, "awe-test/1.0")]
    assert result is not None
    assert result.via == "jina"
    assert result.text == FULL


def test_a_full_body_still_wins_immediately(chain):
    """Nothing changes for the ordinary case: the first route that has the page
    ends the chain, and no third party is contacted."""
    first, second = chain(
        ("impersonate", recovered(FULL, "impersonate:chrome")),
        ("jina", recovered(FULL, "jina")),
    )

    result = fallback.recover(URL)

    assert result is not None and result.via == "impersonate:chrome"
    assert first.calls and second.calls == []


def test_when_no_route_clears_it_the_fullest_body_is_kept(chain):
    """A preference, not a new way to lose a page."""
    thinner = "<html><body><p>Hi</p></body></html>"
    chain(
        ("impersonate", recovered(thinner, "impersonate:chrome")),
        ("jina", recovered(SHELL + "<p>a little more text here</p>", "jina")),
    )

    result = fallback.recover(URL)

    assert result is not None
    assert result.via == "jina"


def test_a_pdf_body_is_exempt(chain):
    """A PDF carries its content as bytes; `text` is empty by contract, so
    measuring it would reject every document."""
    (route,) = chain(
        ("wayback", recovered("", "wayback:20240101", content_type="application/pdf"))
    )

    result = fallback.recover(URL)

    assert route.calls
    assert result is not None and result.via == "wayback:20240101"


def test_zero_disables_the_threshold(chain):
    shell_route, render_route = chain(
        ("impersonate", recovered(SHELL, "impersonate:chrome")),
        ("jina", recovered(FULL, "jina")),
        min_recovered_text_chars=0,
    )

    result = fallback.recover(URL)

    assert result is not None and result.via == "impersonate:chrome"
    assert render_route.calls == []
    assert shell_route.calls


def test_a_declining_route_is_not_confused_with_a_thin_one(chain):
    """None means "nothing obtained"; the chain must still end at None when every
    route declines, so the caller degrades the page to kind="error"."""
    chain(("impersonate", None), ("jina", None))

    assert fallback.recover(URL) is None
