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

from .conftest import Route, StubWeb, page

URL = "https://tarpit-test.org/corporate/index.html"
BODY = "<html><body><h1>The page itself</h1></body></html>"


def scripted(*outcomes, sent: list[str] | None = None):
    """A client that produces `outcomes` in order, repeating the last forever.

    Each outcome is an HTTP status to answer with, or an exception to raise. Used
    for the cases where a host has to *answer* before it goes silent.
    """
    remaining = list(outcomes)

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(str(request.url))
        outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(outcome, int):
            return httpx.Response(
                outcome, headers={"content-type": "text/html"}, text=BODY
            )
        raise outcome

    return httpx.Client(transport=httpx.MockTransport(handler))


def timing_out(exc: Exception | None = None, sent: list[str] | None = None):
    """A client whose every request dies in transport, with no response at all.

    `sent` (when given) records each URL that reached the wire, which is how the
    memo tests below prove a fetch never attempted the origin at all.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(str(request.url))
        raise exc or httpx.ReadTimeout("connection tarpitted", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


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
        route: Route | None = None,
        *,
        exc: Exception | None = None,
        sent: list[str] | None = None,
        outcomes: list | None = None,
        **updates,
    ) -> Route:
        settings = Settings(
            fetch_attempts=1, fetch_fallbacks="jina", llm_cache="", log_file=""
        ).model_copy(update=updates)
        monkeypatch.setattr(fetch, "get_settings", lambda: settings)
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        client = (
            scripted(*outcomes, sent=sent)
            if outcomes is not None
            else timing_out(exc, sent)
        )
        monkeypatch.setattr(fetch, "get_client", lambda: client)
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


# --- the per-host memo ------------------------------------------------------
#
# A host that tarpits refuses every URL identically, and refuses by going silent,
# so the second proof is the last one worth paying for. Before this, each page
# spent the whole attempt budget again — the incident was ~10 minutes of read
# timeouts on one company's site.


def test_a_silent_host_is_written_off_after_the_configured_proofs(wired):
    sent: list[str] = []
    route = wired(sent=sent)

    for path in ("a", "b", "c", "d"):
        fetch.fetch(f"https://tarpit-test.org/{path}")

    # Two URLs proved it; the rest never touched the origin.
    assert [url.rsplit("/", 1)[-1] for url in sent] == ["a", "b"]
    # Every page still went to recovery, which is the point: pages are not lost,
    # they are reached sooner.
    assert [url.rsplit("/", 1)[-1] for url, _ua in route.calls] == ["a", "b", "c", "d"]


def test_the_threshold_is_configurable(wired):
    sent: list[str] = []
    wired(sent=sent, transport_memo_failures=1)

    fetch.fetch("https://tarpit-test.org/a")
    fetch.fetch("https://tarpit-test.org/b")

    assert [url.rsplit("/", 1)[-1] for url in sent] == ["a"]


def test_the_memo_can_be_turned_off(wired):
    sent: list[str] = []
    wired(sent=sent, transport_memo_failures=0)

    for path in ("a", "b", "c"):
        fetch.fetch(f"https://tarpit-test.org/{path}")

    assert len(sent) == 3


def test_a_skip_that_recovers_nothing_falls_back_to_the_origin(wired):
    """The memo is an optimization, never the reason a page is lost.

    `configured_routes()` only knows the route *names*: `impersonate` declines
    outright when `AWE_IMPERSONATE` is unset, when the host is out of scope, or when
    curl_cffi is missing, and a deployment that names it alone would otherwise have
    every later URL on a written-off domain fail without the origin being asked.
    """
    sent: list[str] = []
    route = wired(Route(None), sent=sent)

    fetch.fetch("https://tarpit-test.org/a")
    fetch.fetch("https://tarpit-test.org/b")
    result = fetch.fetch(URL)

    # Recovery was tried first, then the origin anyway.
    assert route.calls[-1][0] == URL
    assert sent[-1] == URL
    assert result.kind == "error"


def test_a_host_that_answers_again_is_no_longer_written_off(wired):
    """The only way back once a domain is written off — and the reason the
    fall-through above matters beyond the one page it saves."""
    sent: list[str] = []
    wired(
        sent=sent,
        outcomes=[
            httpx.ReadTimeout("tarpitted"),
            httpx.ReadTimeout("tarpitted"),
            200,
        ],
        fetch_fallbacks="",  # nothing to skip to, so every fetch reaches the wire
    )

    fetch.fetch("https://tarpit-test.org/a")
    fetch.fetch("https://tarpit-test.org/b")
    assert fetch._written_off(URL)

    fetch.fetch("https://tarpit-test.org/c")  # answers 200

    assert not fetch._written_off(URL)


def test_recovery_disabled_never_skips_the_origin(wired):
    """With nothing to skip *to*, the memo must not turn a slow page into a lost
    one: the origin is still attempted, however hopeless it has proven."""
    sent: list[str] = []
    wired(sent=sent, fetch_fallbacks="")

    for path in ("a", "b", "c"):
        fetch.fetch(f"https://tarpit-test.org/{path}")

    assert len(sent) == 3


def test_a_host_that_has_ever_answered_is_never_written_off(wired):
    """A slow host is not a silent one.

    Eight workers interleave, so the count alone cannot tell "two failures in a row"
    from "two big pages timed out among six that were served". Having answered once
    settles it: whatever the count reaches, the origin keeps being asked.
    """
    sent: list[str] = []
    wired(sent=sent, outcomes=[200, httpx.ReadTimeout("slow body")])

    fetch.fetch("https://tarpit-test.org/small")  # served
    for path in ("big-a", "big-b", "big-c"):
        fetch.fetch(f"https://tarpit-test.org/{path}")  # read timeouts

    assert not fetch._written_off(URL)
    assert len(sent) == 4


def test_only_silence_counts_as_silence(wired):
    """A dead link is not evidence about the host.

    The fetch handler catches bare `Exception` on purpose — a malformed Location
    header raises UnicodeEncodeError, a bad href raises httpx.InvalidURL — and
    writing a domain off over two of those would route a healthy site through the
    recovery chain for the rest of the crawl.
    """
    assert fetch.counts_as_silence(httpx.ReadTimeout("tarpitted"))
    assert fetch.counts_as_silence(httpx.ConnectError("refused"))
    assert not fetch.counts_as_silence(
        UnicodeEncodeError("utf-8", "☃", 0, 1, "bad Location header")
    )
    assert not fetch.counts_as_silence(httpx.InvalidURL("nope"))

    sent: list[str] = []
    wired(sent=sent, exc=UnicodeEncodeError("utf-8", "☃", 0, 1, "bad header"))

    for path in ("a", "b", "c"):
        fetch.fetch(f"https://tarpit-test.org/{path}")

    assert len(sent) == 3  # never written off


def test_the_failure_is_attributed_to_the_host_that_failed(wired):
    """The client follows redirects inside one call, so the host that timed out is
    not always the one asked for. Blaming the first hop writes off a host that
    answers redirects fine while the silent landing domain never accumulates."""
    wired()
    landing = "https://landing-test.net/x"
    exc = httpx.ReadTimeout("tarpitted", request=httpx.Request("GET", landing))

    assert fetch._failed_host("https://redirector-test.org/x", exc) == landing

    for _ in range(2):
        fetch._note_no_response(
            fetch._failed_host("https://redirector-test.org/x", exc)
        )

    assert fetch._written_off("https://landing-test.net/y")
    assert not fetch._written_off("https://redirector-test.org/y")


def test_a_failure_with_no_request_falls_back_to_the_url_asked_for(wired):
    wired()

    assert fetch._failed_host(URL, ValueError("nothing to do with httpx")) == URL


def test_any_response_clears_the_memo(wired):
    """The memo tracks *silence*. A 403 is a host talking to us — one round-trip
    per page, which is nothing to skip — so it must not keep a host written off."""
    wired()

    fetch._note_no_response(URL)
    fetch._note_no_response(URL)
    assert fetch._written_off(URL)

    fetch._note_response("https://tarpit-test.org/somewhere-else")

    assert not fetch._written_off(URL)


def test_the_memo_is_keyed_on_the_registrable_domain(wired):
    """Same host matching as the crawl boundary and the robots overrides."""
    wired()

    fetch._note_no_response("https://tarpit-test.org/a")
    fetch._note_no_response("https://cdn.tarpit-test.org/b")

    assert fetch._written_off("https://www.tarpit-test.org/c")
    assert not fetch._written_off("https://elsewhere-test.com/c")


# --- through the traversal --------------------------------------------------

SEED = "https://tarpit-test.org/"


class RecoveredWeb(StubWeb):
    """A stub web whose pages all arrive by way of a recovery route."""

    def fetch(self, url: str, *, user_agent: str = ""):
        return replace(super().fetch(url, user_agent=user_agent), via="jina")


def test_each_crawl_re_tests_hosts_an_earlier_one_wrote_off(make_extractor):
    """The memo is evidence gathered during a crawl, not a standing fact about the
    network: this crawl may run under a different User-Agent or with impersonation
    newly enabled, and the host that tarpitted an hour ago may be answering now."""
    web = StubWeb({SEED: page()})
    extractor = make_extractor(web)
    fetch._note_no_response(SEED)
    fetch._note_no_response(SEED)
    assert fetch._written_off(SEED)

    extractor.extract(SEED)

    assert not fetch._written_off(SEED)


def test_via_reaches_the_result(make_extractor):
    web = RecoveredWeb({SEED: page()})
    extractor = make_extractor(web)

    result = extractor.extract(SEED)

    assert result.fallbacks_used == {SEED: "jina"}
