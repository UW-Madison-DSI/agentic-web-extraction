"""The impersonate recovery route: scope, sessions, and the attribution gate.

Everything here runs against a fake session (see `fake_impersonate` in
conftest), so the suite stays offline and passes with curl_cffi absent — which is
also the default install. One test exercises the real import, skipped when the
optional extra isn't present.
"""

import threading
from dataclasses import replace

import pytest

from agentic_web_extraction import fallback
from agentic_web_extraction.config import Settings

from .conftest import StubWeb, page

URL = "https://blocked-test.org/corporate/index.html"
UA = "awe-test/1.0 (+https://example.edu/crawler)"


@pytest.fixture
def configure(monkeypatch):
    """Point the fallback module at one Settings."""

    def apply(**updates) -> Settings:
        settings = Settings(llm_cache="", log_file="", **updates)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        return settings

    return apply


def test_the_route_is_off_until_a_target_is_named(configure, fake_impersonate):
    configure(impersonate="")

    assert fallback.impersonate(URL, user_agent=UA) is None
    assert fake_impersonate == []  # no session built, nothing sent


def test_a_missing_curl_cffi_declines_rather_than_raising(configure, monkeypatch):
    configure(impersonate="chrome")

    def unimportable(target: str):
        raise ImportError("No module named 'curl_cffi'")

    monkeypatch.setattr(fallback, "_sessions", threading.local())
    monkeypatch.setattr(fallback, "_new_session", unimportable)

    assert fallback.impersonate(URL, user_agent=UA) is None


def test_recovered_body_and_provenance(configure, fake_impersonate):
    configure(impersonate="chrome124")

    recovered = fallback.impersonate(URL, user_agent=UA)

    assert recovered is not None
    assert recovered.text == "<html><body>recovered</body></html>"
    assert recovered.content_type == "text/html; charset=utf-8"
    # `via` names the target, so a result can be read back as "this page came from
    # an escalated transport", not merely "from the origin".
    assert recovered.via == "impersonate:chrome124"


def test_a_refusal_declines(configure, fake_impersonate):
    """The origin refuses this client too: nothing to hand back, and the chain
    moves on to the next configured route."""
    configure(impersonate="chrome")
    fallback._session_for_thread("chrome").response.status_code = 403

    assert fallback.impersonate(URL, user_agent=UA) is None


# --- the §2 policy gate: whose name is on the request -----------------------


def test_by_default_the_crawls_own_user_agent_is_sent(configure, fake_impersonate):
    """A browser fingerprint under an attributable string: the honest rung."""
    configure(impersonate="chrome")

    fallback.impersonate(URL, user_agent=UA)

    _url, headers, _timeout = fake_impersonate[0].calls[0]
    assert headers == {"User-Agent": UA}


def test_browser_ua_drops_our_identity(configure, fake_impersonate):
    """Opting in sends no override, so curl_cffi's impersonated browser UA goes
    out instead and nothing on the request names the operator. A deliberate,
    separately-typed choice — never a side effect of enabling `impersonate`."""
    configure(impersonate="chrome", impersonate_browser_ua=True)

    fallback.impersonate(URL, user_agent=UA)

    _url, headers, _timeout = fake_impersonate[0].calls[0]
    assert headers is None


def test_a_blank_user_agent_falls_back_to_the_module_default(
    configure, fake_impersonate
):
    configure(impersonate="chrome")

    fallback.impersonate(URL)

    _url, headers, _timeout = fake_impersonate[0].calls[0]
    assert headers == {"User-Agent": fallback._user_agent}


# --- scope ------------------------------------------------------------------


def test_domains_scope_the_escalation(configure, fake_impersonate):
    configure(impersonate="chrome", impersonate_domains="blocked-test.org")

    assert fallback.impersonate(URL, user_agent=UA) is not None
    # A subdomain keys to the same registrable domain, as everywhere else.
    assert fallback.impersonate("https://www.blocked-test.org/x") is not None
    assert fallback.impersonate("https://elsewhere-test.com/x") is None


def test_empty_domains_means_every_host(configure, fake_impersonate):
    configure(impersonate="chrome", impersonate_domains="")

    assert fallback.impersonate("https://anywhere-test.com/x", user_agent=UA)


def test_the_configured_timeout_is_sent(configure, fake_impersonate):
    configure(impersonate="chrome", impersonate_timeout=7.5)

    fallback.impersonate(URL, user_agent=UA)

    _url, _headers, timeout = fake_impersonate[0].calls[0]
    assert timeout == 7.5


# --- sessions ---------------------------------------------------------------


def test_one_session_per_thread(configure, fake_impersonate):
    """A curl_cffi session wraps a libcurl handle; a wave shares none of it."""
    configure(impersonate="chrome")
    seen: list[int] = []

    def crawl() -> None:
        fallback.impersonate(URL, user_agent=UA)
        fallback.impersonate(URL, user_agent=UA)
        seen.append(id(fallback._session_for_thread("chrome")))

    threads = [threading.Thread(target=crawl) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Two requests per thread, one session each — reused within the thread, never
    # across threads.
    assert len(fake_impersonate) == 3
    assert len(set(seen)) == 3


def test_one_session_per_target(configure, fake_impersonate):
    configure(impersonate="chrome")
    fallback.impersonate(URL, user_agent=UA)
    configure(impersonate="safari")
    fallback.impersonate(URL, user_agent=UA)

    assert [session.target for session in fake_impersonate] == ["chrome", "safari"]


def test_the_real_session_factory_builds_a_session():
    """The optional dependency, when it is actually installed. No network: the
    session is constructed and dropped."""
    pytest.importorskip("curl_cffi", reason="the 'impersonate' extra is not installed")

    session = fallback._new_session("chrome")

    assert session is not None


# --- the route is registered, and is not a way around the boundary ----------


def test_impersonate_is_a_known_route(configure):
    configure(fetch_fallbacks="impersonate,jina,wayback")

    assert fallback.configured_routes() == ["impersonate", "jina", "wayback"]


SEED = "https://blocked-test.org/"
ON_SITE = "https://blocked-test.org/about"
OFF_SITE = "https://elsewhere-test.com/x"


class ImpersonatedWeb(StubWeb):
    """A stub web whose pages all arrive through the escalated transport."""

    def fetch(self, url: str, *, user_agent: str = ""):
        return replace(
            super().fetch(url, user_agent=user_agent), via="impersonate:chrome"
        )


def test_a_recovered_page_still_obeys_the_crawl_boundary(make_extractor):
    """Recovery is retrieval; it decides nothing about where the crawl may go.

    The boundary is enforced at the one `frontier.push` call site, downstream of
    every route, so a page read through impersonation expands no further than one
    read directly.
    """
    web = ImpersonatedWeb({SEED: page(ON_SITE, OFF_SITE), ON_SITE: page()})
    extractor = make_extractor(web, allowed_domains=["blocked-test.org"])

    result = extractor.extract(SEED)

    assert OFF_SITE not in web.fetched
    assert ON_SITE in web.fetched
    assert result.fallbacks_used[SEED] == "impersonate:chrome"
