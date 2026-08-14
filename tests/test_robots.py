"""robots.txt policy: verdicts, overrides, and the deliberate fail-open."""

import httpx
import pytest

from agentic_web_extraction import robots
from agentic_web_extraction.robots import RobotsPolicy

from .conftest import StubWeb, page

UA = "awe-test/1.0 (+https://example.edu/crawler)"

ROBOTS = """
User-agent: *
Disallow: /private/

User-agent: awe-test
Disallow: /secret/
"""


def serving(body: str, status: int = 200):
    """A robots fetcher returning a fixed body for any origin."""

    def fetcher(url: str) -> tuple[int, str]:
        assert url.endswith("/robots.txt")
        return status, body

    return fetcher


def test_disallowed_and_allowed_paths():
    policy = RobotsPolicy(user_agent=UA, fetcher=serving(ROBOTS))

    assert policy.allows("https://site-test.org/public/x") is True
    # The named group wins over `*` for this agent, so /private/ is *not* applied.
    assert policy.allows("https://site-test.org/secret/x") is False


def test_wildcard_group_applies_to_an_unnamed_agent():
    policy = RobotsPolicy(user_agent="somebody-else/1.0", fetcher=serving(ROBOTS))

    assert policy.allows("https://site-test.org/private/x") is False
    assert policy.allows("https://site-test.org/public/x") is True


def test_blanket_disallow():
    policy = RobotsPolicy(user_agent=UA, fetcher=serving("User-agent: *\nDisallow: /"))

    assert policy.allows("https://site-test.org/") is False
    assert policy.allows("https://site-test.org/anything") is False


def test_override_domain_bypasses_the_check():
    policy = RobotsPolicy(
        user_agent=UA,
        overrides=["site-test.org"],
        fetcher=serving("User-agent: *\nDisallow: /"),
    )

    assert policy.allows("https://site-test.org/anything") is True
    assert policy.allows("https://www.site-test.org/anything") is True  # eTLD+1 key
    assert policy.allows("https://other-test.com/anything") is False


def test_fetch_failure_fails_open():
    def exploding(url: str) -> tuple[int, str]:
        raise httpx.ConnectError("no route to host")

    policy = RobotsPolicy(user_agent=UA, fetcher=exploding)

    assert policy.allows("https://site-test.org/anything") is True


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_non_2xx_robots_fails_open(status):
    policy = RobotsPolicy(
        user_agent=UA, fetcher=serving("User-agent: *\nDisallow: /", status=status)
    )

    assert policy.allows("https://site-test.org/anything") is True


def test_robots_is_fetched_once_per_origin():
    calls: list[str] = []

    def counting(url: str) -> tuple[int, str]:
        calls.append(url)
        return 200, ROBOTS

    policy = RobotsPolicy(user_agent=UA, fetcher=counting)
    for path in ("/a", "/b", "/private/c"):
        policy.allows(f"https://site-test.org{path}")
    policy.allows("https://other-test.com/a")

    assert calls == [
        "https://site-test.org/robots.txt",
        "https://other-test.com/robots.txt",
    ]


def test_non_http_url_is_allowed():
    policy = RobotsPolicy(user_agent=UA, fetcher=serving(ROBOTS))

    assert policy.allows("ftp://site-test.org/x") is True


# --- through the traversal --------------------------------------------------

SEED = "https://site-test.org/public/index"
OPEN_LINK = "https://site-test.org/public/more"
CLOSED_LINK = "https://site-test.org/secret/more"


def robots_web() -> StubWeb:
    return StubWeb(
        {SEED: page(OPEN_LINK, CLOSED_LINK), OPEN_LINK: page(), CLOSED_LINK: page()}
    )


def test_traversal_skips_a_disallowed_page(make_extractor, monkeypatch):
    monkeypatch.setattr(robots, "_http_get", serving(ROBOTS))
    web = robots_web()
    extractor = make_extractor(web, respect_robots=True)

    result = extractor.extract(SEED)

    assert OPEN_LINK in web.fetched
    # Never requested, and not reported as a page the crawl visited.
    assert CLOSED_LINK not in web.fetched
    assert CLOSED_LINK not in result.path


def test_traversal_override_ignores_robots(make_extractor, monkeypatch):
    monkeypatch.setattr(robots, "_http_get", serving(ROBOTS))
    web = robots_web()
    extractor = make_extractor(
        web, respect_robots=True, robots_overrides="site-test.org"
    )

    extractor.extract(SEED)

    assert CLOSED_LINK in web.fetched


def test_robots_off_by_default(make_extractor, monkeypatch):
    def unexpected(url: str) -> tuple[int, str]:
        raise AssertionError("robots.txt must not be fetched when the check is off")

    monkeypatch.setattr(robots, "_http_get", unexpected)
    web = robots_web()
    extractor = make_extractor(web)

    extractor.extract(SEED)

    assert CLOSED_LINK in web.fetched
