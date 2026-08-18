"""Attribution: the crawl's User-Agent is configurable and actually sent."""

from agentic_web_extraction import fallback, fetch
from agentic_web_extraction.config import Settings

from .conftest import StubWeb, page

SEED = "https://site-test.org/"


def teardown_function() -> None:
    """Both clients are process-wide singletons, so leave them as found."""
    fetch.configure()
    fallback.configure()
    fetch.close_client()
    fallback.close_client()


def test_default_user_agent_is_the_library_constant():
    fetch.configure()

    assert fetch.user_agent() == fetch.USER_AGENT
    assert fetch.get_client().headers["User-Agent"] == fetch.USER_AGENT


def test_configure_sets_the_header_on_an_existing_client():
    # Build the client first, so this covers the reconfigure-in-place path rather
    # than only the lazy-init one.
    fetch.get_client()
    fetch.configure(user_agent="my-pipeline/1.0 (+https://example.edu/bot)")

    assert (
        fetch.get_client().headers["User-Agent"]
        == "my-pipeline/1.0 (+https://example.edu/bot)"
    )


def test_empty_configure_restores_the_default():
    fetch.configure(user_agent="my-pipeline/1.0")
    fetch.configure(user_agent="")

    assert fetch.user_agent() == fetch.USER_AGENT


def test_extractor_applies_the_configured_user_agent(make_extractor, settings):
    ua = "rabbit-test/9.9 (+https://example.edu/crawler; Some Team)"
    web = StubWeb({SEED: page()})
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"user_agent": ua})
    )

    assert extractor.user_agent == ua
    assert fetch.get_client().headers["User-Agent"] == ua
    # Recovery requests identify the same operator (the route is recorded in
    # FetchedPage.via, so nothing depends on a route-specific UA).
    assert fallback.get_client().headers["User-Agent"] == ua


def test_explicit_kwarg_beats_the_setting(make_extractor, settings):
    web = StubWeb({SEED: page()})
    extractor = make_extractor(
        web,
        user_agent="explicit/1.0",
        settings=settings.model_copy(update={"user_agent": "from-env/1.0"}),
    )

    assert extractor.user_agent == "explicit/1.0"
    assert fetch.user_agent() == "explicit/1.0"


def test_each_fetch_carries_its_own_extractors_user_agent(make_extractor, settings):
    """A second Extractor must not rename the first one's traffic.

    `configure` sets one string for the whole process, so without a per-request
    override, building any second Extractor without `user_agent=` reverts every
    crawl in flight to the generic library string — and leaves the agent sent
    diverging from the agent the robots rules are evaluated against.
    """
    ua = "first-pipeline/1.0 (+https://a.edu/)"
    first = make_extractor(StubWeb({SEED: page()}), user_agent=ua)

    # A second Extractor with no user_agent= resets the shared process default...
    web = StubWeb({SEED: page()})
    make_extractor(web, settings=settings)
    assert fetch.user_agent() != ua

    first.extract(SEED)

    # ...but the first crawl's own requests still name the first operator.
    assert web.requests == [(SEED, ua)]


def test_the_request_user_agent_reaches_the_wire(make_extractor):
    ua = "wire-test/2.0 (+https://example.edu/crawler)"
    web = StubWeb({SEED: page()})
    extractor = make_extractor(web, user_agent=ua)

    extractor.extract(SEED)

    assert web.requests == [(SEED, ua)]


def test_the_impersonate_route_carries_the_same_attribution(
    monkeypatch, fake_impersonate
):
    """A browser *fingerprint* is not a browser *identity*. By default the
    escalated route sends the crawl's own string, so the traffic stays traceable
    to its operator; dropping it is the separate AWE_IMPERSONATE_BROWSER_UA
    switch, and that is the only thing that drops it."""
    ua = "rabbit-test/9.9 (+https://example.edu/crawler; Some Team)"
    settings = Settings(impersonate="chrome", llm_cache="", log_file="")
    monkeypatch.setattr(fallback, "get_settings", lambda: settings)

    fallback.impersonate("https://blocked-test.org/x", user_agent=ua)

    _url, headers, _timeout = fake_impersonate[0].calls[0]
    assert headers == {"User-Agent": ua}


def test_blank_user_agent_falls_back_to_the_constant(make_extractor):
    web = StubWeb({SEED: page()})
    extractor = make_extractor(
        web,
        user_agent="",
        settings=Settings(max_workers=1, llm_cache="", log_file="", user_agent=""),
    )

    assert extractor.user_agent == fetch.USER_AGENT
