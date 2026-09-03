"""Per-domain pacing.

The crawl boundary exists to keep a traversal on one site, so `max_workers`
workers land on a single origin -- and before this there was nothing between the
knob that makes the crawl fast and the behaviour a site operator experiences as
an attack. These pin down that the gate spaces requests, that concurrent workers
queue instead of colliding, and that an origin's own `Crawl-delay` wins when it
asks for more.

Timing is asserted on a monotonic clock the tests control, not on wall time: a
test that really slept would be slow *and* flaky on a loaded machine.
"""

import threading

import httpx
import pytest

from agentic_web_extraction import fallback, fetch
from agentic_web_extraction.config import Settings
from agentic_web_extraction.robots import RobotsPolicy

from .conftest import StubWeb, page

URL = "https://pace-test.org/a"
OTHER_PATH = "https://pace-test.org/b"
OTHER_DOMAIN = "https://elsewhere-test.org/a"


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock plus a `sleep` that advances it.

    Returns the recorded sleep durations, so a test asserts on *how long the gate
    asked to wait* rather than on how long the machine took.
    """
    now = [1000.0]
    slept: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(fetch.time, "monotonic", monotonic)
    monkeypatch.setattr(fetch.time, "sleep", sleep)
    return slept


def ok_client(sent: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>hi</body></html>",
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def paced_fetch(monkeypatch):
    """`fetch.fetch` against a always-200 client, with settings a test supplies."""

    def wire(**overrides):
        settings = Settings(
            llm_cache="",
            log_file="",
            fetch_fallbacks="",
            # These tests serve a two-character body, which the shipped
            # `min_page_text_chars` reads as a client-rendered shell. Pacing is
            # what is under test, so the thin-page check is off here -- and
            # `fallback` is pinned to the same settings, because it resolves its
            # routes from the process-wide ones: patching only `fetch` would let
            # a stub body reach the real jina/wayback endpoints.
            min_page_text_chars=0,
            **overrides,
        )
        monkeypatch.setattr(fetch, "get_settings", lambda: settings)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        monkeypatch.setattr(fetch, "get_client", lambda: ok_client())
        return settings

    return wire


def test_the_first_request_to_a_domain_never_waits(paced_fetch, clock):
    paced_fetch(request_delay=0.5)
    fetch.fetch(URL)
    assert clock == []


def test_a_second_request_to_the_same_domain_waits_the_delay(paced_fetch, clock):
    paced_fetch(request_delay=0.5)
    fetch.fetch(URL)
    fetch.fetch(OTHER_PATH)
    assert clock == [pytest.approx(0.5)]


def test_pacing_is_per_registrable_domain_not_per_url(paced_fetch, clock):
    """A different path on the same site is the same server; a different site is
    not. Keyed through `frontier.domain_of` like every other host comparison, so
    `www.` and a subdomain share a budget too."""
    paced_fetch(request_delay=0.5)
    fetch.fetch(URL)
    fetch.fetch(OTHER_DOMAIN)
    assert clock == []


def test_a_zero_delay_restores_the_unpaced_behavior(paced_fetch, clock):
    paced_fetch(request_delay=0.0)
    for _ in range(5):
        fetch.fetch(URL)
    assert clock == []


def test_concurrent_workers_queue_rather_than_all_waking_together(paced_fetch, clock):
    """The reservation is what makes this work. If each thread read "next allowed"
    and slept toward the same instant, eight workers would sleep 0.5s and then all
    fire at once -- pacing that looks right in a log and changes nothing at the
    origin."""
    paced_fetch(request_delay=0.5)

    waits = [fetch._reserve_slot("pace-test.org", 0.5) for _ in range(4)]

    assert waits == [pytest.approx(w) for w in [0.0, 0.5, 1.0, 1.5]]


def test_slots_are_reserved_without_holding_the_lock_across_the_sleep(paced_fetch):
    """Two domains must not queue behind each other. `_reserve_slot` releases the
    lock before the caller sleeps, so a slow domain never stalls a fast one."""
    paced_fetch(request_delay=0.5)
    assert fetch._reserve_slot("a-test.org", 5.0) == 0.0
    assert fetch._reserve_slot("b-test.org", 5.0) == 0.0


def test_reset_pacing_forgets_reserved_slots(paced_fetch, clock):
    paced_fetch(request_delay=0.5)
    fetch.fetch(URL)
    fetch.reset_pacing()
    fetch.fetch(OTHER_PATH)
    assert clock == []


def test_the_in_flight_cap_limits_concurrent_requests_to_one_domain(paced_fetch):
    """The gate is a semaphore per domain, so the (N+1)th worker blocks until one
    of the N in flight finishes."""
    paced_fetch(request_delay=0.0, max_per_domain=1)
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with fetch.paced(URL, 0.0, 1):
            entered.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(timeout=5)

    gate = fetch._gate_for("pace-test.org", 1)
    assert gate is not None
    assert gate.acquire(blocking=False) is False  # held by the other thread

    release.set()
    worker.join(timeout=5)
    assert gate.acquire(blocking=False) is True


def test_no_cap_means_no_gate(paced_fetch):
    paced_fetch(max_per_domain=0)
    assert fetch._gate_for("pace-test.org", 0) is None


# --- Crawl-delay -------------------------------------------------------------


def policy(body: str) -> RobotsPolicy:
    return RobotsPolicy(
        user_agent="awe-test/1.0",
        fetcher=lambda url, ua: (200, "text/plain", body),
        escalated_fetcher=lambda url, ua: None,
    )


def test_crawl_delay_is_read_for_the_configured_agent():
    rules = "User-agent: *\nCrawl-delay: 3\n"
    assert policy(rules).crawl_delay(URL) == 3.0


def test_no_crawl_delay_directive_is_zero_not_an_error():
    assert policy("User-agent: *\nDisallow: /private\n").crawl_delay(URL) == 0.0


def test_a_malformed_crawl_delay_is_zero():
    """A directive nobody can parse is not a policy. Returning 0 leaves
    `AWE_REQUEST_DELAY` in charge, so the crawl stays paced either way."""
    assert policy("User-agent: *\nCrawl-delay: soon\n").crawl_delay(URL) == 0.0


def test_an_unobtainable_robots_txt_is_zero():
    unreachable = RobotsPolicy(
        user_agent="awe-test/1.0",
        fetcher=lambda url, ua: (500, "text/plain", ""),
        escalated_fetcher=lambda url, ua: None,
    )
    assert unreachable.crawl_delay(URL) == 0.0


def test_an_override_exempts_a_domain_from_the_delay_too():
    """An override says the operator is authorized to set this site's policy
    aside. Honouring half of it -- ignoring the rules but obeying the delay -- is
    just a slower crawl for no gain in permission."""
    exempt = RobotsPolicy(
        user_agent="awe-test/1.0",
        overrides=["pace-test.org"],
        fetcher=lambda url, ua: (200, "text/plain", "User-agent: *\nCrawl-delay: 9\n"),
        escalated_fetcher=lambda url, ua: None,
    )
    assert exempt.crawl_delay(URL) == 0.0


def test_an_origins_crawl_delay_reaches_the_fetch(
    make_extractor, settings, fake_tokens
):
    """End to end: the worker reads the delay off the parser the allow-check
    already cached and passes it down, so `fetch.py` never learns what robots.txt
    is."""
    web = StubWeb({URL: page()})
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"request_delay": 0.25})
    )
    extractor.robots = policy("User-agent: *\nCrawl-delay: 7\n")

    extractor.extract(URL)

    assert web.delays == [7.0]


def test_the_larger_of_the_two_delays_wins(paced_fetch, clock):
    """`AWE_REQUEST_DELAY` is a floor the deployment sets; an origin asking for
    more gets more, and an origin asking for less does not get to speed us up."""
    paced_fetch(request_delay=2.0)
    fetch.fetch(URL, min_delay=0.1)
    fetch.fetch(OTHER_PATH, min_delay=0.1)
    assert clock == [pytest.approx(2.0)]

    fetch.reset_pacing()
    fetch.fetch(URL, min_delay=6.0)
    fetch.fetch(OTHER_PATH, min_delay=6.0)
    assert clock[-1] == pytest.approx(6.0)


def test_an_extractors_own_settings_reach_the_fetch(monkeypatch, clock):
    """`fetch` reads the pace gate, the thin-page threshold and `follow_pdf` from
    the settings it is *handed*, not from the process-wide `get_settings()`.
    Without that, `Extractor(settings=...)` -- which is what the CLI's
    settings-only flags are built on -- would configure nothing at all."""
    process = Settings(
        llm_cache="",
        log_file="",
        fetch_fallbacks="",
        request_delay=0.0,
        min_page_text_chars=0,
    )
    monkeypatch.setattr(fetch, "get_settings", lambda: process)
    monkeypatch.setattr(fallback, "get_settings", lambda: process)
    monkeypatch.setattr(fetch, "get_client", lambda: ok_client())
    caller = process.model_copy(update={"request_delay": 3.0})

    fetch.fetch(URL, settings=caller)
    fetch.fetch(OTHER_PATH, settings=caller)

    assert clock == [pytest.approx(3.0)]


def test_a_direct_caller_still_gets_the_process_settings(monkeypatch, clock):
    process = Settings(
        llm_cache="",
        log_file="",
        fetch_fallbacks="",
        request_delay=1.5,
        min_page_text_chars=0,
    )
    monkeypatch.setattr(fetch, "get_settings", lambda: process)
    monkeypatch.setattr(fallback, "get_settings", lambda: process)
    monkeypatch.setattr(fetch, "get_client", lambda: ok_client())

    fetch.fetch(URL)
    fetch.fetch(OTHER_PATH)

    assert clock == [pytest.approx(1.5)]
