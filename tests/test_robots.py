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


def serving(body: str, status: int = 200, content_type: str = "text/plain"):
    """A robots fetcher returning a fixed body for any origin."""

    def fetcher(url: str, user_agent: str = "") -> tuple[int, str, str]:
        assert url.endswith("/robots.txt")
        return status, content_type, body

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
    def exploding(url: str, user_agent: str = "") -> tuple[int, str, str]:
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
    calls: list[tuple[str, str]] = []

    def counting(url: str, user_agent: str = "") -> tuple[int, str, str]:
        calls.append((url, user_agent))
        return 200, "text/plain", ROBOTS

    policy = RobotsPolicy(user_agent=UA, fetcher=counting)
    for path in ("/a", "/b", "/private/c"):
        policy.allows(f"https://site-test.org{path}")
    policy.allows("https://other-test.com/a")

    assert calls == [
        ("https://site-test.org/robots.txt", UA),
        ("https://other-test.com/robots.txt", UA),
    ]


# --- a 200 is not automatically a policy ------------------------------------

SENSOR_PAGE = (
    '<html><body><h1>It works!</h1><noscript><img src="/akam/13/pixel_1"/>'
    "</noscript></body></html>"
)


def test_a_bot_sensor_page_is_not_read_as_consent():
    """The observed case: an origin answers /robots.txt with 200 and an HTML
    interstitial. Parsed, that is an empty ruleset — blanket permission from a
    site that said nothing of the kind. It must be treated as *unavailable*."""
    policy = RobotsPolicy(
        user_agent=UA, fetcher=serving(SENSOR_PAGE, content_type="text/html")
    )

    # Fails open, as every unobtainable robots.txt does...
    assert policy.allows("https://site-test.org/anything") is True
    # ...but with no parser cached, i.e. recorded as "no rules obtained" rather
    # than as a policy that permits everything.
    assert policy._parser_for("https://site-test.org/anything") is None


def test_markup_is_rejected_even_when_typed_text_plain():
    policy = RobotsPolicy(
        user_agent=UA, fetcher=serving(SENSOR_PAGE, content_type="text/plain")
    )

    assert policy._parser_for("https://site-test.org/x") is None


def test_a_real_policy_is_unchanged_by_the_check():
    policy = RobotsPolicy(user_agent=UA, fetcher=serving(ROBOTS))

    assert policy.allows("https://site-test.org/secret/x") is False
    assert policy._parser_for("https://site-test.org/x") is not None


@pytest.mark.parametrize(
    "content_type",
    ["", "text/plain", "text/plain; charset=utf-8", "text/x-robots", "TEXT/PLAIN"],
)
def test_plausible_policy_content_types_are_accepted(content_type):
    """Lenient on purpose: rejecting an oddly-typed real policy would discard
    rules the site meant us to follow."""
    policy = RobotsPolicy(
        user_agent=UA, fetcher=serving(ROBOTS, content_type=content_type)
    )

    assert policy.allows("https://site-test.org/secret/x") is False


@pytest.mark.parametrize(
    "content_type", ["text/html", "application/json", "image/png", "application/pdf"]
)
def test_document_content_types_are_rejected(content_type):
    policy = RobotsPolicy(
        user_agent=UA,
        fetcher=serving("User-agent: *\nDisallow: /", content_type=content_type),
    )

    assert policy._parser_for("https://site-test.org/x") is None


# --- escalated transport ----------------------------------------------------


def test_an_unobtainable_policy_is_retried_over_the_escalated_transport():
    """A deployment that reads a site's pages with a browser fingerprint must read
    its rules the same way, or it always proceeds on a policy it never got."""
    calls: list[str] = []

    def escalated(url: str, user_agent: str = "") -> tuple[int, str, str]:
        calls.append(url)
        return 200, "text/plain", ROBOTS

    policy = RobotsPolicy(
        user_agent=UA,
        fetcher=serving(SENSOR_PAGE, content_type="text/html"),
        escalated_fetcher=escalated,
    )

    # The rules the origin would only serve to a browser-shaped client apply.
    assert policy.allows("https://site-test.org/secret/x") is False
    assert calls == ["https://site-test.org/robots.txt"]


def test_the_escalated_transport_is_not_tried_when_the_first_one_worked():
    def unexpected(url: str, user_agent: str = "") -> tuple[int, str, str]:
        raise AssertionError("a usable robots.txt must not be fetched twice")

    policy = RobotsPolicy(
        user_agent=UA, fetcher=serving(ROBOTS), escalated_fetcher=unexpected
    )

    assert policy.allows("https://site-test.org/secret/x") is False


def test_a_declining_escalated_transport_still_fails_open():
    """The default: impersonation is unconfigured, so it returns None."""
    policy = RobotsPolicy(
        user_agent=UA,
        fetcher=serving("", status=503),
        escalated_fetcher=lambda url, ua="": None,
    )

    assert policy.allows("https://site-test.org/anything") is True


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


def test_redirect_to_a_disallowed_path_is_discarded(make_extractor, monkeypatch):
    """A redirector on an allowed path must not smuggle a disallowed page in.

    The request is unavoidable -- httpx follows the redirect inside one call -- but
    the body must not be read, screened, or pooled into the extraction.
    """
    monkeypatch.setattr(robots, "_http_get", serving(ROBOTS))
    hop = "https://site-test.org/public/go"
    web = StubWeb(
        pages={SEED: page(hop), CLOSED_LINK: page()},
        redirects={hop: CLOSED_LINK},
    )
    extractor = make_extractor(web, respect_robots=True)

    result = extractor.extract(SEED)

    assert hop in web.fetched  # the redirect itself could not be prevented
    assert CLOSED_LINK not in result.path
    assert [v.url for v in result.verdicts] == [SEED]


def test_redirect_within_allowed_paths_is_kept(make_extractor, monkeypatch):
    monkeypatch.setattr(robots, "_http_get", serving(ROBOTS))
    hop = "https://site-test.org/public/go"
    web = StubWeb(
        pages={SEED: page(hop), OPEN_LINK: page()},
        redirects={hop: OPEN_LINK},
    )
    extractor = make_extractor(web, respect_robots=True)

    result = extractor.extract(SEED)

    assert OPEN_LINK in result.path


def test_a_malformed_url_never_aborts_the_crawl(make_extractor):
    """`urlsplit` raises on a bracketed host, and every URL helper goes through it.

    Unguarded, one such href escapes the worker, surfaces out of `pool.map`, and
    discards every page already collected.
    """
    good = "https://site-test.org/public/more"
    web = StubWeb(
        {
            SEED: page("http://a[b]c.example.com/x", good),
            good: page(),
        }
    )
    extractor = make_extractor(web)

    result = extractor.extract(SEED)

    assert result.stopped_reason == "match"
    assert SEED in result.path


def test_robots_off_by_default(make_extractor, monkeypatch):
    def unexpected(url: str, user_agent: str = "") -> tuple[int, str]:
        raise AssertionError("robots.txt must not be fetched when the check is off")

    monkeypatch.setattr(robots, "_http_get", unexpected)
    web = robots_web()
    extractor = make_extractor(web)

    extractor.extract(SEED)

    assert CLOSED_LINK in web.fetched
