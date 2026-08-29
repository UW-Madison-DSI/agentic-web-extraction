"""Best-first ordering: the navigation policy itself.

`StubProvider` scores every link 0.9, so these use a provider that scores from a
table instead -- which is what makes "did the crawl go where the model pointed
it" an assertion rather than an assumption.
"""

from agentic_web_extraction.extractor import SEED_SCORE
from agentic_web_extraction.frontier import Frontier, canonical

from .conftest import StubProvider, StubWeb


class ScoringProvider(StubProvider):
    """Scores links from a `url -> score` table; anything unlisted scores 0.1."""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__()
        self.scores = scores

    def score_links(self, links, page_md, criterion, **kwargs):
        return [(url, self.scores.get(url, 0.1)) for _text, url in links]


# --- the heap itself ---------------------------------------------------------


def pop_url(frontier: Frontier) -> str:
    """Pop and return the URL, asserting the heap was not empty.

    `Frontier.pop` returns None when nothing is left, which is the right shape for
    the traversal loop and the wrong shape to subscript in a test that just pushed
    something.
    """
    popped = frontier.pop()
    assert popped is not None
    return popped[0]


def test_the_frontier_pops_in_descending_score_order():
    frontier = Frontier()
    for url, score in [
        ("https://a/low", 0.1),
        ("https://a/high", 0.9),
        ("https://a/mid", 0.5),
    ]:
        frontier.push(url, score=score, source="t")

    popped = [pop_url(frontier) for _ in range(3)]

    assert popped == ["https://a/high", "https://a/mid", "https://a/low"]


def test_a_seed_outranks_every_scored_link():
    """Seeds enter at a sentinel above the 0..1 range a scorer can return, so
    every seed is fetched before anything discovered -- however many seeds and
    however confident the scorer."""
    frontier = Frontier()
    frontier.push("https://a/scored", score=1.0, source="t")
    frontier.push("https://a/seed", score=SEED_SCORE, source="seed")

    assert pop_url(frontier) == "https://a/seed"


def test_equal_scores_keep_insertion_order():
    """The heap's tiebreaker is a monotonic counter, not the URL: without it,
    ties would order by string comparison and a crawl's shape would depend on
    what its pages happen to be called."""
    frontier = Frontier()
    for url in ["https://a/z", "https://a/m", "https://a/a"]:
        frontier.push(url, score=0.5, source="t")

    assert [pop_url(frontier) for _ in range(3)] == [
        "https://a/z",
        "https://a/m",
        "https://a/a",
    ]


def test_a_url_already_queued_is_not_queued_again():
    frontier = Frontier()
    assert frontier.push("https://a/x", score=0.5, source="t") is True
    assert frontier.push("https://a/x", score=0.9, source="t") is False
    assert len(frontier) == 1


def test_urls_that_canonicalize_together_are_one_entry():
    """A fragment, a reordered query and a missing trailing slash are the same
    page; queuing each separately would spend the budget three times on it."""
    frontier = Frontier()
    frontier.push("https://a/x?b=2&a=1", score=0.5, source="t")

    assert frontier.push("https://a/x?a=1&b=2#section", score=0.9, source="t") is False
    assert len(frontier) == 1


def test_a_visited_url_is_never_popped_again():
    frontier = Frontier()
    frontier.push("https://a/x", score=0.5, source="t")
    frontier.mark_visited("https://a/x")

    assert frontier.pop() is None


def test_the_snapshot_covers_both_seen_and_visited():
    """Workers pre-filter links against this, so a URL that is queued but not yet
    fetched has to be in it -- otherwise every page in a wave re-scores the links
    its siblings already queued."""
    frontier = Frontier()
    frontier.push("https://a/queued", score=0.5, source="t")
    frontier.push("https://a/done", score=0.5, source="t")
    frontier.mark_visited("https://a/done")

    snapshot = frontier.snapshot()

    assert canonical("https://a/queued") in snapshot
    assert canonical("https://a/done") in snapshot


# --- ordering as the traversal actually experiences it -----------------------

SEED = "https://order-test.org/"


def linked(*hrefs: str) -> str:
    body = "".join(f'<a href="{h}">to {h}</a>' for h in hrefs)
    return f"<html><body><p>Body.</p>{body}</body></html>"


def test_the_traversal_follows_the_scorer_not_the_page_order(
    make_extractor, fake_tokens
):
    """The seed lists its links worst-first. A crawl that fetched them in document
    order would look identical on a page whose author happened to order them well,
    which is why the fixture orders them badly on purpose."""
    good = "https://order-test.org/good"
    fine = "https://order-test.org/fine"
    poor = "https://order-test.org/poor"
    web = StubWeb(
        {
            SEED: linked(poor, fine, good),
            good: linked(),
            fine: linked(),
            poor: linked(),
        }
    )
    extractor = make_extractor(web)
    extractor.provider = ScoringProvider({good: 0.9, fine: 0.5, poor: 0.1})

    extractor.extract(SEED)

    assert web.fetched == [SEED, good, fine, poor]


def test_the_budget_spends_on_the_best_links_first(make_extractor, fake_tokens):
    """With a budget smaller than the frontier, which pages are reached *is* the
    product. A budget of two must buy the seed and the best link, not the seed and
    whichever link came first."""
    good = "https://order-test.org/good"
    poor = "https://order-test.org/poor"
    web = StubWeb({SEED: linked(poor, good), good: linked(), poor: linked()})
    extractor = make_extractor(web)
    extractor.provider = ScoringProvider({good: 0.9, poor: 0.1})

    extractor.extract(SEED, max_fetches=2)

    assert web.fetched == [SEED, good]


def test_a_later_page_can_outrank_what_is_already_queued(make_extractor, fake_tokens):
    """Best-first is global, not per-page: a high scorer discovered on page two
    must be fetched before a low scorer that has been waiting since page one."""
    ok = "https://order-test.org/ok"
    dull = "https://order-test.org/dull"
    best = "https://order-test.org/best"
    web = StubWeb(
        {
            SEED: linked(ok, dull),
            ok: linked(best),
            dull: linked(),
            best: linked(),
        }
    )
    extractor = make_extractor(web)
    extractor.provider = ScoringProvider({ok: 0.6, dull: 0.2, best: 0.99})

    extractor.extract(SEED)

    assert web.fetched == [SEED, ok, best, dull]


def test_every_seed_is_fetched_before_any_discovered_link(make_extractor, fake_tokens):
    second = "https://other-test.org/"
    child = "https://order-test.org/child"
    web = StubWeb({SEED: linked(child), second: linked(), child: linked()})
    extractor = make_extractor(web)
    extractor.provider = ScoringProvider({child: 0.99})

    extractor.extract([SEED, second])

    assert web.fetched[:2] == [SEED, second]
