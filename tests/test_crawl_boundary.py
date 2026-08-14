"""The hard crawl boundary: which domains a scored link may be queued from.

The incident these cover: a grants crawl seeded at one foundation followed
LLM-scored links onto unrelated third-party sites, which a URL-categorization
appliance flagged as malware. `allowed_domains` is default-deny, so a link off
the set is never queued and therefore never fetched.
"""

from agentic_web_extraction.frontier import domain_of, normalize_domains, split_domains

from .conftest import StubWeb, page

SEED = "https://glpf-test.org/grants"
ON_SITE = "https://grants.glpf-test.org/program"
OFF_SITE = "https://algae-test.com/algae"
PORTAL = "https://smapply-test.com/apply/123"


def three_link_web() -> StubWeb:
    return StubWeb(
        {
            SEED: page(ON_SITE, OFF_SITE, PORTAL),
            ON_SITE: page(),
            OFF_SITE: page(),
            PORTAL: page(),
        }
    )


def test_off_boundary_link_is_never_fetched(make_extractor):
    web = three_link_web()
    extractor = make_extractor(web, allowed_domains=["glpf-test.org"])

    extractor.extract(SEED)

    assert OFF_SITE not in web.fetched
    assert PORTAL not in web.fetched
    # A subdomain of an allowed registrable domain is inside the boundary.
    assert ON_SITE in web.fetched


def test_extra_allowed_domain_is_fetched(make_extractor):
    """The application-portal case: an off-seed domain the caller vetted."""
    web = three_link_web()
    extractor = make_extractor(
        web, allowed_domains=["glpf-test.org", "smapply-test.com"]
    )

    extractor.extract(SEED)

    assert PORTAL in web.fetched
    assert OFF_SITE not in web.fetched


def test_no_allowed_domains_is_unrestricted(make_extractor):
    """Default (`None`) must preserve pre-0.3 behavior exactly: no boundary."""
    web = three_link_web()
    extractor = make_extractor(web)

    extractor.extract(SEED)

    assert OFF_SITE in web.fetched
    assert PORTAL in web.fetched


def test_seed_domain_is_implicitly_allowed(make_extractor):
    """A caller passing only extras still crawls the site it seeded."""
    web = three_link_web()
    extractor = make_extractor(web, allowed_domains=["smapply-test.com"])

    extractor.extract(SEED)

    assert ON_SITE in web.fetched
    assert OFF_SITE not in web.fetched


def test_empty_allowed_domains_means_seeds_only(make_extractor):
    web = three_link_web()
    extractor = make_extractor(web, allowed_domains=[])

    extractor.extract(SEED)

    assert ON_SITE in web.fetched  # same domain as the seed
    assert OFF_SITE not in web.fetched
    assert PORTAL not in web.fetched


def test_blocked_link_is_logged(make_extractor, capsys):
    """ "Why didn't we fetch X" has to be answerable from the log."""
    web = three_link_web()
    extractor = make_extractor(web, allowed_domains=["glpf-test.org"])

    extractor.extract(SEED)

    stderr = capsys.readouterr().err
    assert f"[blocked] {OFF_SITE}" in stderr


# --- seed redirects (the rebrand case) -------------------------------------

OLD_SEED = "https://openphil-test.org/grants"
NEW_HOME = "https://coefficient-test.org/grants"
NEW_LINK = "https://coefficient-test.org/opportunity/1"


def redirecting_web() -> StubWeb:
    return StubWeb(
        pages={NEW_HOME: page(NEW_LINK), NEW_LINK: page()},
        redirects={OLD_SEED: NEW_HOME},
    )


def test_seed_redirect_widens_the_boundary_when_opted_in(make_extractor):
    web = redirecting_web()
    extractor = make_extractor(
        web,
        allowed_domains=["openphil-test.org"],
        allow_seed_redirect_domains=True,
    )

    result = extractor.extract(OLD_SEED)

    assert NEW_HOME in result.path  # the redirect itself always resolves
    assert NEW_LINK in web.fetched  # ...and its links stay crawlable


def test_seed_redirect_does_not_widen_by_default(make_extractor):
    """Off by default: whoever controls the seed's DNS decides where it lands, so
    widening is the one path by which someone else could grow the boundary."""
    web = redirecting_web()
    extractor = make_extractor(web, allowed_domains=["openphil-test.org"])

    result = extractor.extract(OLD_SEED)

    # httpx follows redirects inside one fetch, so the seed page is still read --
    # the boundary governs queuing, not requests.
    assert NEW_HOME in result.path
    assert NEW_LINK not in web.fetched


def test_redirect_rule_applies_to_seeds_only(make_extractor):
    """A *link* that redirects off-boundary must not widen anything -- only a seed
    does, and only because the caller named it."""
    hop = "https://glpf-test.org/go/elsewhere"
    landing = "https://algae-test.com/landing"
    web = StubWeb(
        pages={
            SEED: page(hop),
            landing: page("https://algae-test.com/deeper"),
            "https://algae-test.com/deeper": page(),
        },
        redirects={hop: landing},
    )
    extractor = make_extractor(web, allowed_domains=["glpf-test.org"])

    extractor.extract(SEED)

    # The hop is on-boundary so it is fetched, and it lands off-boundary (httpx
    # followed the redirect) -- but nothing it links to becomes crawlable.
    assert hop in web.fetched
    assert "https://algae-test.com/deeper" not in web.fetched


def test_a_dead_seed_does_not_widen_the_boundary(make_extractor):
    """A lapsed seed domain that now parks on somebody else's host must not hand
    that host the boundary -- widening requires readable content."""
    parked = "https://parked-test.com/lp"
    hub = "https://hub-test.org/links"
    web = StubWeb(
        # `parked` is absent from `pages`, so the stub answers it as a 404 error page.
        pages={hub: page("https://parked-test.com/deeper")},
        redirects={OLD_SEED: parked},
    )
    extractor = make_extractor(
        web,
        allowed_domains=["hub-test.org"],
        allow_seed_redirect_domains=True,
    )

    extractor.extract([OLD_SEED, hub])

    assert "https://parked-test.com/deeper" not in web.fetched


def test_widening_is_independent_of_fold_order(make_extractor, settings):
    """All seeds share SEED_SCORE, so they arrive in one wave. A link scored on one
    seed's page must not be dropped for a domain another seed in the same wave
    legitimizes — otherwise the boundary depends on which worker finished first."""
    hub = "https://hub-test.org/links"
    landing = "https://coefficient-test.org/grants"
    deeper = "https://coefficient-test.org/opportunity/1"
    web = StubWeb(
        # `hub` links to the domain the other seed is about to legitimize.
        pages={hub: page(deeper), landing: page(), deeper: page()},
        redirects={OLD_SEED: landing},
    )
    extractor = make_extractor(
        web,
        # 2 workers puts both seeds in a single wave, where the race lived.
        settings=settings.model_copy(update={"max_workers": 2}),
        allowed_domains=["hub-test.org"],
        allow_seed_redirect_domains=True,
    )

    extractor.extract([hub, OLD_SEED])

    assert deeper in web.fetched


def test_a_blocked_link_is_logged_once_per_crawl(make_extractor, capsys):
    """A site-wide footer link is re-offered by every page; repeating the line per
    page buries the log without adding a fact."""
    other = "https://glpf-test.org/about"
    web = StubWeb(
        {
            SEED: page(other, OFF_SITE),
            other: page(OFF_SITE),
        }
    )
    extractor = make_extractor(web, allowed_domains=["glpf-test.org"])

    extractor.extract(SEED)

    stderr = capsys.readouterr().err
    assert other in web.fetched  # both pages offered the same off-boundary link
    assert stderr.count(f"[blocked] {OFF_SITE}") == 1


# --- domain keys -----------------------------------------------------------


def test_domain_of_accepts_urls_hosts_and_ports():
    assert domain_of("https://www.grants.glpf-test.org/a/b?c=1") == "glpf-test.org"
    assert domain_of("GLPF-TEST.org") == "glpf-test.org"
    assert domain_of("glpf-test.org:8443") == "glpf-test.org"
    # Multi-label public suffixes come from the PSL, not a hand-rolled list.
    assert domain_of("https://sub.dept.example.co.uk/x") == "example.co.uk"
    # No registrable domain: keyed by bare host so it can still be named.
    assert domain_of("http://localhost:8000/x") == "localhost"
    assert domain_of("http://127.0.0.1:8000/x") == "127.0.0.1"
    assert domain_of("") == ""
    assert domain_of("not a url") == ""


def test_normalize_and_split_domains():
    assert normalize_domains(
        ["https://a-test.org/x", "b-test.com", "", "  "]
    ) == frozenset({"a-test.org", "b-test.com"})
    assert split_domains("smapply-test.com, paperform-test.com ,") == frozenset(
        {"smapply-test.com", "paperform-test.com"}
    )
    assert split_domains("") == frozenset()
