"""What reaches the model, and what the scorer is billed for.

Two separate mechanisms that are easy to conflate:

* `main_content_only` filters the DOM before the Markdown conversion. It is
  lossy, so it is off by default -- and it must never affect link discovery,
  which reads the unfiltered HTML.
* the extension filter drops links no fetch could ever read as a page. It is
  free of any change in outcome, so it is always on -- and it must stay a pure
  function of the URL, because its result is what the page cache stores.
"""

from agentic_web_extraction.normalize import (
    NON_CONTENT_EXTENSIONS,
    extract_links,
    is_probably_content_url,
    strip_boilerplate,
    to_markdown,
)

from .conftest import StubProvider, StubWeb

PAGE = """<html><head><title>T</title><script>tracker()</script>
<style>.a{}</style></head><body>
<nav><a href="/nav-target">Navigation</a>NAV CHROME</nav>
<header>SITE BANNER</header>
<main><article>
<header><h1>The Real Title</h1><time>2026-08-29</time></header>
<p>The body of the article.</p>
</article></main>
<aside>RELATED JUNK</aside>
<footer>FOOTER JUNK</footer>
</body></html>"""


def md(main_content_only: bool) -> str:
    return to_markdown(
        PAGE.encode("utf-8"),
        "text/html; charset=utf-8",
        url="https://filter-test.org/",
        main_content_only=main_content_only,
    )


def test_off_by_default_nothing_is_removed():
    text = md(main_content_only=False)
    assert "NAV CHROME" in text
    assert "FOOTER JUNK" in text


def test_site_chrome_is_removed_when_asked():
    text = md(main_content_only=True)
    for junk in ("NAV CHROME", "SITE BANNER", "RELATED JUNK", "FOOTER JUNK"):
        assert junk not in text


def test_an_articles_own_header_survives():
    """The reason this is not WaterCrawl's "remove every header" rule. An
    article's `<header>` is where its title and date live, which is exactly the
    concrete values a target schema asks for -- so the naive rule loses data on
    precisely the pages worth extracting from."""
    text = md(main_content_only=True)
    assert "The Real Title" in text
    assert "2026-08-29" in text
    assert "The body of the article." in text


def test_invisible_elements_go_whether_or_not_chrome_does():
    for flag in (True, False):
        text = md(main_content_only=flag)
        assert "tracker()" not in text


def test_filtering_never_hides_a_link_from_the_scorer():
    """The separation that makes this safe to turn on: link discovery reads the
    raw HTML, so filtering a navigation block out of the *extraction input* leaves
    the crawl able to follow every link that was in it."""
    links = extract_links(PAGE, base_url="https://filter-test.org/")
    assert ("Navigation", "https://filter-test.org/nav-target") in links


def test_strip_boilerplate_leaves_a_page_with_no_chrome_alone():
    html = "<html><body><p>Just prose.</p></body></html>"
    assert "Just prose." in strip_boilerplate(html, main_content_only=True)


# --- the link extension filter ----------------------------------------------


def test_links_no_fetch_could_read_are_dropped_before_scoring():
    html = """<a href="/a.zip">z</a><a href="/b.jpg">i</a><a href="/c.mp4">v</a>
    <a href="/d.css">s</a><a href="/e.docx">d</a>"""
    assert extract_links(html, base_url="https://filter-test.org/") == []


def test_pdfs_are_kept():
    """`.pdf` is absent from the deny list and deliberately not gated on
    `follow_pdf`: this filter runs in the worker, whose output the page cache
    stores, so anything varying with configuration would be baked into an entry
    and replayed under a different configuration later. With `follow_pdf` off the
    fetch skips the PDF cheaply instead."""
    links = extract_links(
        '<a href="/paper.pdf">p</a>', base_url="https://filter-test.org/"
    )
    assert links == [("p", "https://filter-test.org/paper.pdf")]


def test_pages_with_a_dynamic_extension_are_kept():
    """A deny list, not an allow list: `.php`, `.aspx` and `.do` are pages, and an
    allow list would have to enumerate every server technology ever shipped."""
    for name in ("index.php", "Default.aspx", "view.do", "page.html", "plain"):
        assert is_probably_content_url(f"https://filter-test.org/{name}")


def test_an_extension_in_a_query_string_is_not_a_file_extension():
    assert is_probably_content_url("https://filter-test.org/download?file=x.zip")


def test_a_dot_in_a_directory_name_is_not_an_extension():
    assert is_probably_content_url("https://filter-test.org/v1.2/overview")


def test_the_filter_is_case_insensitive():
    assert not is_probably_content_url("https://filter-test.org/PHOTO.JPG")


def test_an_unparseable_url_is_left_for_the_fetch_to_judge():
    """Not evidence of anything. Guessing here would drop a page over a URL this
    module could not read, and `fetch` is the thing that actually knows."""
    assert is_probably_content_url("http://a[b]c.com/x")


def test_the_deny_list_does_not_claim_pdf():
    assert "pdf" not in NON_CONTENT_EXTENSIONS


# --- the per-page link cap ---------------------------------------------------

MANY = "".join(f'<a href="/p{i}">link {i}</a>' for i in range(50))


def test_extract_links_never_truncates():
    """The cap is applied by the caller, after it has removed links the crawl has
    already seen. Truncating here would spend the whole allowance on the site-wide
    navigation at the top of every page."""
    assert len(extract_links(MANY, base_url="https://filter-test.org/")) == 50


class RecordingScorer(StubProvider):
    """Records exactly which links the traversal asked it to rank."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[list[str]] = []

    def score_links(self, links, page_md, criterion, **kwargs):
        self.asked.append([url for _text, url in links])
        return [(url, 0.5) for _text, url in links]


def crawl_one_page(make_extractor, settings, body: str, **overrides):
    """Fetch a single page and hand back what its links cost the scorer."""
    seed = "https://filter-test.org/"
    extractor = make_extractor(
        StubWeb({seed: body}), settings=settings.model_copy(update=overrides)
    )
    scorer = RecordingScorer()
    extractor.provider = scorer
    extractor.extract(seed, max_fetches=1)
    return scorer.asked


def test_the_cap_reaches_the_scorer_through_the_traversal(
    make_extractor, settings, fake_tokens
):
    asked = crawl_one_page(
        make_extractor,
        settings,
        f"<html><body><p>x</p>{MANY}</body></html>",
        max_links_per_page=4,
    )

    assert asked == [["https://filter-test.org/p%d" % i for i in range(4)]]


def test_the_cap_is_applied_after_links_already_seen_are_removed(
    make_extractor, settings, fake_tokens
):
    """The starvation bug this ordering exists to prevent. A site-wide nav appears
    at the top of every page; capping the raw list first would hand it the entire
    allowance on page two, leave nothing new to score, and stall the frontier
    after the seed."""
    seed = "https://filter-test.org/"
    nav = "".join(f'<a href="/nav{i}">nav {i}</a>' for i in range(4))
    second = f"{seed}nav0"
    web = StubWeb(
        {
            seed: f"<html><body><p>x</p>{nav}</body></html>",
            second: f'<html><body><p>x</p>{nav}<a href="/only-here">unique</a></body></html>',
        }
    )
    extractor = make_extractor(
        web, settings=settings.model_copy(update={"max_links_per_page": 4})
    )
    scorer = RecordingScorer()
    extractor.provider = scorer
    extractor.extract(seed, max_fetches=2)

    # Page two offers one link the crawl has not seen; the nav is already queued,
    # so the cap has room for it.
    assert scorer.asked[1] == ["https://filter-test.org/only-here"]


def test_the_scorer_is_never_billed_for_an_unreadable_link(
    make_extractor, settings, fake_tokens
):
    """The whole point of the extension filter: these were fetched, classified
    `skipped` and dropped -- after the scorer had already ranked them."""
    body = (
        '<html><body><p>x</p><a href="/real">page</a>'
        '<a href="/a.zip">z</a><a href="/b.png">i</a></body></html>'
    )

    asked = crawl_one_page(make_extractor, settings, body)

    assert asked == [["https://filter-test.org/real"]]


def test_a_charset_declared_only_in_the_markup_is_honoured():
    """bs4 gets the undecoded bytes so it can consult `<meta charset>`. Decoding
    first -- which this did at one point -- turns a windows-1251 page whose only
    charset declaration is in the markup into replacement characters, silently, in
    the text the extraction model reads."""
    body = (
        '<html><head><meta charset="windows-1251"></head>'
        "<body><p>\u041f\u0440\u0438\u0432\u0435\u0442</p></body></html>"
    ).encode("windows-1251")

    text = to_markdown(body, "text/html", main_content_only=True)

    assert "\u041f\u0440\u0438\u0432\u0435\u0442" in text
    assert "\ufffd" not in text


def test_the_header_charset_is_used_when_the_markup_declares_none():
    body = "<html><body><p>caf\u00e9</p></body></html>".encode("latin-1")

    text = to_markdown(body, "text/html; charset=latin-1", main_content_only=True)

    assert "caf\u00e9" in text
