"""Sitemap seeding.

Two things are being defended. First, that discovered URLs go *through* the
frontier -- scored, boundary-gated, robots-checked -- rather than around it: a
few hundred URLs entering unranked would drown the relevance ordering that is the
whole navigation policy. Second, that a third party's XML cannot hurt us: these
documents are written by the site being crawled, which is not a trusted party.
"""

import gzip

import httpx
import pytest

from agentic_web_extraction import fetch, sitemap
from agentic_web_extraction.sitemap import discover, parse

from .conftest import StubProvider, StubWeb

ORIGIN = "https://map-test.org"
SEED = f"{ORIGIN}/"

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://map-test.org/a</loc></url>
  <url><loc>https://map-test.org/b</loc></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://map-test.org/one.xml</loc></sitemap>
</sitemapindex>"""


# --- parsing -----------------------------------------------------------------


def test_a_urlset_yields_pages():
    pages, children = parse("u", URLSET)
    assert pages == ["https://map-test.org/a", "https://map-test.org/b"]
    assert children == []


def test_an_index_yields_child_documents_not_pages():
    pages, children = parse("u", INDEX)
    assert pages == []
    assert children == ["https://map-test.org/one.xml"]


def test_namespaces_are_matched_on_the_local_name():
    """The 0.84, 0.90 and Google extension namespaces all appear in the wild, and
    some publishers emit none at all. Matching local names accepts every variant
    without a namespace table to keep current."""
    bare = b"<urlset><url><loc>https://map-test.org/a</loc></url></urlset>"
    other_ns = (
        b'<urlset xmlns="http://www.google.com/schemas/sitemap/0.84">'
        b"<url><loc>https://map-test.org/a</loc></url></urlset>"
    )
    for body in (bare, other_ns):
        assert parse("u", body)[0] == ["https://map-test.org/a"]


def test_a_gzipped_document_is_decompressed():
    """Sniffed on the magic bytes, not the extension: origins serve `sitemap.xml`
    gzipped and `sitemap.xml.gz` already decoded by the transport, about equally."""
    assert parse("u", gzip.compress(URLSET))[0] == [
        "https://map-test.org/a",
        "https://map-test.org/b",
    ]


def test_an_entity_declaration_is_refused_unparsed():
    """ElementTree really does expand internal general entities, so the
    billion-laughs construction works against it. A sitemap has no legitimate
    reason to declare one, and refusing the construct outright is a check that
    cannot be reasoned around the way a parser setting would have to be
    re-verified on every upgrade."""
    bomb = (
        b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        b"<urlset><url><loc>https://map-test.org/&lol2;</loc></url></urlset>"
    )
    assert parse("u", bomb) == ([], [])


def test_a_non_http_location_never_reaches_the_fetch_path():
    hostile = (
        b"<urlset>"
        b"<url><loc>file:///etc/passwd</loc></url>"
        b"<url><loc>javascript:alert(1)</loc></url>"
        b"<url><loc>https://map-test.org/ok</loc></url>"
        b"</urlset>"
    )
    assert parse("u", hostile)[0] == ["https://map-test.org/ok"]


def test_malformed_xml_is_empty_not_an_exception():
    assert parse("u", b"<urlset><url><loc>oops") == ([], [])


def test_an_empty_body_is_empty():
    assert parse("u", b"") == ([], [])


# --- discovery ---------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """Serve a `path -> (status, body)` map over the crawl's client."""

    def wire(routes: dict[str, tuple[int, bytes]], requested: list[str] | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if requested is not None:
                requested.append(str(request.url))
            status, body = routes.get(request.url.path, (404, b""))
            return httpx.Response(
                status, headers={"content-type": "application/xml"}, content=body
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(fetch, "get_client", lambda: client)
        monkeypatch.setattr(sitemap.fetch_module, "get_client", lambda: client)

    return wire


def test_robots_txt_sitemap_lines_are_preferred(served):
    requested: list[str] = []
    served(
        {
            "/robots.txt": (
                200,
                b"User-agent: *\nSitemap: https://map-test.org/one.xml\n",
            ),
            "/one.xml": (200, URLSET),
        },
        requested,
    )

    assert discover(SEED) == ["https://map-test.org/a", "https://map-test.org/b"]
    assert f"{ORIGIN}/sitemap.xml" not in requested


def test_the_conventional_location_is_the_fallback(served):
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)})
    assert discover(SEED) == ["https://map-test.org/a", "https://map-test.org/b"]


def test_an_index_is_followed_one_level(served):
    served(
        {
            "/robots.txt": (404, b""),
            "/sitemap.xml": (200, INDEX),
            "/one.xml": (200, URLSET),
        }
    )
    assert discover(SEED) == ["https://map-test.org/a", "https://map-test.org/b"]


def test_no_sitemap_anywhere_is_an_empty_list(served):
    served({"/robots.txt": (404, b""), "/sitemap.xml": (404, b"")})
    assert discover(SEED) == []


def test_the_url_cap_is_honoured(served):
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)})
    assert discover(SEED, max_urls=1) == ["https://map-test.org/a"]


def test_the_document_cap_bounds_what_one_seed_can_cost(served):
    """A sitemap index can name hundreds of children; without a cap, one seed
    could spend hundreds of requests before the crawl proper begins."""
    requested: list[str] = []
    big_index = (
        b"<sitemapindex>"
        + b"".join(
            b"<sitemap><loc>https://map-test.org/s%d.xml</loc></sitemap>" % i
            for i in range(20)
        )
        + b"</sitemapindex>"
    )
    routes = {"/robots.txt": (404, b""), "/sitemap.xml": (200, big_index)}
    routes.update({f"/s{i}.xml": (200, URLSET) for i in range(20)})
    served(routes, requested)

    discover(SEED, max_documents=3)

    # One for /sitemap.xml plus two children, and robots.txt outside the budget.
    assert len([u for u in requested if u.endswith(".xml")]) == 3


def test_a_disallowed_sitemap_is_not_fetched(served):
    """A crawl that honours robots.txt for its content while reading whatever it
    likes at /sitemap.xml is honouring it selectively."""
    requested: list[str] = []
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)}, requested)

    assert discover(SEED, allows=lambda url: False) == []
    assert not any(u.endswith("/sitemap.xml") for u in requested)


def test_an_oversized_body_is_truncated_rather_than_read_whole(served):
    served(
        {"/robots.txt": (404, b""), "/sitemap.xml": (200, b"<urlset>" + b"x" * 5000)}
    )
    assert discover(SEED, max_bytes=100) == []


def test_a_sitemap_named_on_another_host_is_never_fetched(served):
    """The `Sitemap:` lines are written by the site being crawled, so without a
    scope check an origin could name any address and have the client request it --
    another tenant's host, an internal service, a cloud instance's link-local
    metadata endpoint. The crawl boundary does not cover this: it gates links
    entering the frontier, and these are documents fetched before that."""
    requested: list[str] = []
    served(
        {
            "/robots.txt": (
                200,
                b"Sitemap: http://169.254.169.254/latest/meta-data/\n"
                b"Sitemap: https://evil-test.org/sitemap.xml\n",
            ),
            "/sitemap.xml": (200, URLSET),
        },
        requested,
    )

    discover(SEED)

    assert not any("169.254" in u or "evil-test" in u for u in requested)


def test_an_index_pointing_off_host_is_ignored(served):
    """Same reasoning one level down: a `<loc>` inside a sitemap index is the same
    untrusted input as a `Sitemap:` line."""
    requested: list[str] = []
    off_host = (
        b"<sitemapindex><sitemap>"
        b"<loc>https://evil-test.org/child.xml</loc>"
        b"</sitemap></sitemapindex>"
    )
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, off_host)}, requested)

    discover(SEED)

    assert not any("evil-test" in u for u in requested)


def test_a_subdomain_sitemap_is_in_scope(served):
    """Scoped at the registrable domain, like every other host comparison in the
    library, so a site that keeps its sitemap on a `static.` host still works."""
    requested: list[str] = []
    served(
        {
            "/robots.txt": (200, b"Sitemap: https://static.map-test.org/s.xml\n"),
            "/s.xml": (200, URLSET),
        },
        requested,
    )

    assert discover(SEED) == ["https://map-test.org/a", "https://map-test.org/b"]


def test_sitemap_fetches_go_through_the_pace_gate(served, monkeypatch):
    """These are the first requests a crawl makes. An opt-in pass that opened with
    half a dozen unspaced hits would undo the politeness the rest of the crawl now
    has, at exactly the moment an origin is deciding what we are."""
    waits: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", waits.append)
    served(
        {
            "/robots.txt": (404, b""),
            "/sitemap.xml": (200, INDEX),
            "/one.xml": (200, URLSET),
        }
    )

    discover(SEED, delay=0.5)

    # robots.txt, then /sitemap.xml, then the child: two gaps between three hits.
    assert len(waits) == 2
    assert all(w > 0 for w in waits)


# --- seeding the frontier ----------------------------------------------------


class SitemapScorer(StubProvider):
    """Records the links it was asked to rank and scores from a table."""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__()
        self.scores = scores
        self.asked: list[list[str]] = []

    def score_links(self, links, page_md, criterion, **kwargs):
        self.asked.append([url for _text, url in links])
        return [(url, self.scores.get(url, 0.1)) for _text, url in links]


def html(*hrefs: str) -> str:
    body = "".join(f'<a href="{h}">to {h}</a>' for h in hrefs)
    return f"<html><body><p>Body.</p>{body}</body></html>"


def test_sitemap_urls_are_scored_and_compete_in_the_same_heap(
    make_extractor, settings, served, fake_tokens
):
    """The property that keeps relevance in charge. A sitemap URL the scorer likes
    beats a page link it doesn't, and vice versa -- neither wins by provenance."""
    dull_link = f"{ORIGIN}/dull"
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)})
    web = StubWeb(
        {
            SEED: html(dull_link),
            dull_link: html(),
            f"{ORIGIN}/a": html(),
            f"{ORIGIN}/b": html(),
        }
    )
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"use_sitemap": True})
    )
    extractor.provider = SitemapScorer({f"{ORIGIN}/a": 0.99, dull_link: 0.2})

    extractor.extract(SEED, max_fetches=3)

    assert web.fetched[:2] == [SEED, f"{ORIGIN}/a"]


def test_the_sitemap_pass_is_off_by_default(make_extractor, served, fake_tokens):
    requested: list[str] = []
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)}, requested)
    web = StubWeb({SEED: html()})

    make_extractor(web).extract(SEED)

    assert requested == []


def test_a_sitemap_url_outside_the_boundary_is_blocked_like_any_other(
    make_extractor, settings, served, fake_tokens
):
    """The reason discovery runs after `allowed` is built and pushes through the
    shared gate: a site must not be able to nominate a domain the caller refused
    just by listing it in its own sitemap."""
    off_site = b"<urlset><url><loc>https://elsewhere-test.org/x</loc></url></urlset>"
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, off_site)})
    web = StubWeb({SEED: html()})
    extractor = make_extractor(
        web,
        allowed_domains=[],
        settings=settings.model_copy(update={"use_sitemap": True}),
    )

    extractor.extract(SEED)

    assert "https://elsewhere-test.org/x" not in web.fetched


def test_a_failed_sitemap_never_costs_the_crawl(
    make_extractor, settings, served, monkeypatch, fake_tokens
):
    def explode(*args, **kwargs):
        raise RuntimeError("the sitemap host is on fire")

    monkeypatch.setattr(
        "agentic_web_extraction.extractor.sitemap_module.discover", explode
    )
    served({})
    web = StubWeb({SEED: html()})
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"use_sitemap": True})
    )

    result = extractor.extract(SEED)

    assert web.fetched == [SEED]
    assert result.pages_fetched == 1


def test_seed_is_content_skips_the_sitemap_entirely(
    make_extractor, settings, served, fake_tokens
):
    """Direct mode means "these seeds are the content"; discovering more URLs to
    rank would contradict the one thing the caller asserted."""
    requested: list[str] = []
    served({"/robots.txt": (404, b""), "/sitemap.xml": (200, URLSET)}, requested)
    web = StubWeb({SEED: html()})
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"use_sitemap": True})
    )

    extractor.extract(SEED, seed_is_content=True)

    assert requested == []
