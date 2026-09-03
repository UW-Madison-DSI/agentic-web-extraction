"""The fit-or-summarize step: the pipeline's only lossy stage.

The extraction model never sees the original text, so everything here decides
what the strong model is allowed to know. It also runs one cheap-model call per
page, which makes it the second-largest line on the bill after extraction. Both
are reasons the map/reduce arithmetic should be pinned down rather than inferred
from a log line.

`fake_tokens` counts whitespace words instead of loading a real encoding, so
these stay offline (see the fixture).
"""

import pytest

from agentic_web_extraction.cache import SUMMARY_NAMESPACE, SqliteKVCache
from agentic_web_extraction.summarize import fit_pages

from .conftest import Doc


class RecordingProvider:
    """A summarizer that shrinks by a fixed ratio and records what it was asked."""

    name = "recording"
    model_screen = "screen"
    model_extract = "extract"
    prompt_signature = "sig"

    def __init__(self, ratio: int = 2) -> None:
        self.ratio = ratio
        self.calls: list[str] = []
        self.schemas: list[object] = []

    def summarize(self, text: str, criterion: str, **kwargs) -> str:
        self.calls.append(text)
        self.schemas.append(kwargs.get("schema"))
        words = text.split()
        # Keep every `ratio`-th word: shrinks deterministically and stays a
        # function of the input, so a cache hit is observable as a missing call.
        return " ".join(words[:: self.ratio]) or "empty"


def words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def fit(pages, provider, *, budget, always=False, cache=None, log=None):
    return fit_pages(
        pages,
        criterion="anything relevant",
        schema=Doc,
        provider=provider,
        max_context_tokens=budget,
        always=always,
        model="model",
        encoding_name="enc",
        cache=cache,
        version="v1",
        log=log if log is not None else (lambda _m: None),
    )


def test_content_that_fits_is_passed_through_untouched(fake_tokens):
    provider = RecordingProvider()
    pages = [("https://a/1", words(10))]

    text, summarized, content_tokens, input_tokens = fit(pages, provider, budget=1000)

    assert provider.calls == []  # not one cheap-model call
    assert summarized is False
    assert content_tokens == input_tokens
    assert "w0" in text and "w9" in text


def test_the_source_marker_survives_so_citations_stay_traceable(fake_tokens):
    """Every page enters the extraction under a `--- SOURCE:` line. Without it the
    one consolidated call cannot attribute a value to the page it came from."""
    provider = RecordingProvider()
    text, *_ = fit([("https://a/1", words(5))], provider, budget=1000)
    assert "--- SOURCE: https://a/1" in text


def test_overflow_triggers_the_map_pass_page_by_page(fake_tokens):
    """A page is the unit: it is the cache-stable one, and summarizing pages
    independently is what lets an unchanged page replay for free."""
    provider = RecordingProvider()
    pages = [("https://a/1", words(40, "a")), ("https://a/2", words(40, "b"))]

    text, summarized, content_tokens, input_tokens = fit(pages, provider, budget=50)

    assert summarized is True
    assert len(provider.calls) == 2
    assert provider.calls[0].startswith("a0 ")
    assert provider.calls[1].startswith("b0 ")
    assert input_tokens < content_tokens


def test_the_target_schema_reaches_every_summarize_call(fake_tokens):
    """The criterion says what is topically relevant; the schema says which
    concrete values are going to be asked for. A summarizer given only the first
    drops the dates and identifiers the second requires."""
    provider = RecordingProvider()
    fit([("https://a/1", words(40))], provider, budget=10)
    assert provider.schemas and all(s is Doc for s in provider.schemas)


def test_always_runs_the_map_pass_on_content_that_already_fits(fake_tokens):
    provider = RecordingProvider()
    pages = [("https://a/1", words(10))]

    _text, summarized, content_tokens, input_tokens = fit(
        pages, provider, budget=1000, always=True
    )

    assert len(provider.calls) == 1
    assert summarized is True
    assert input_tokens < content_tokens


def test_always_does_not_start_a_reduce_pass_on_content_that_fits(fake_tokens):
    """`always` makes the *map* pass unconditional and nothing else: the reduce
    loop stays keyed on being over budget, so a corpus that already fits costs
    exactly one call per page."""
    provider = RecordingProvider()
    pages = [("https://a/%d" % i, words(4)) for i in range(3)]

    fit(pages, provider, budget=1000, always=True)

    assert len(provider.calls) == 3


def test_reduce_runs_until_it_fits(fake_tokens):
    """One map pass halves it; the budget needs more, so reduce passes follow."""
    provider = RecordingProvider(ratio=2)
    pages = [("https://a/1", words(400))]

    _text, summarized, _content, input_tokens = fit(pages, provider, budget=25)

    assert summarized is True
    assert input_tokens <= 25
    assert len(provider.calls) > 1


def test_a_summarizer_that_stops_shrinking_is_hard_truncated(fake_tokens):
    """The guard that matters: a model that will not compress must still not
    overflow the extraction call. Better a truncated input than a request the
    endpoint rejects after the crawl has already been paid for."""
    provider = RecordingProvider(ratio=1)  # returns the text unchanged
    pages = [("https://a/1", words(200))]

    _text, summarized, _content, input_tokens = fit(pages, provider, budget=20)

    assert summarized is True
    assert input_tokens <= 20


def test_the_reduce_loop_stops_as_soon_as_it_stops_converging(fake_tokens):
    """A summarizer that will not compress must cost one wasted reduce pass, not
    five. The loop's cap is `_MAX_REDUCE_LEVELS`, but the check that actually
    fires is "this pass did not shrink it" -- without that, a model returning its
    input verbatim would spend four more passes over the whole corpus to reach the
    same hard truncation."""
    lines: list[str] = []
    provider = RecordingProvider(ratio=1)  # returns the text unchanged

    fit([("https://a/1", words(200))], provider, budget=20, log=lines.append)

    levels = [ln for ln in lines if "] level " in ln]
    assert len(levels) == 2  # the map pass, then one reduce that proves the point
    assert any("hard-truncated" in ln for ln in lines)


def test_an_unchanged_chunk_replays_from_the_summary_cache(tmp_path, fake_tokens):
    cache = SqliteKVCache(tmp_path / "kv.sqlite")
    pages = [("https://a/1", words(40))]

    first = RecordingProvider()
    text_a, *_ = fit(pages, first, budget=25, cache=cache)
    second = RecordingProvider()
    text_b, *_ = fit(pages, second, budget=25, cache=cache)

    assert first.calls  # the first run paid for it
    assert second.calls == []  # the second did not
    assert text_a == text_b


def test_the_summary_key_does_not_record_why_the_summary_was_wanted(
    tmp_path, fake_tokens
):
    """`SUMMARY` is keyed on the version stamp plus chunk content, and nothing
    else -- in particular not `always`. A run that compressed because it
    overflowed and a run that compressed because the caller asked for it produce
    the same chunks, so the second is free.

    The budget *is* load-bearing here even though it is not in the key: it decides
    the chunk boundaries, so the same page split at two different budgets is two
    different chunks and legitimately two entries. Sharing is across trigger
    reasons, not across budgets."""
    cache = SqliteKVCache(tmp_path / "kv.sqlite")
    pages = [("https://a/1", words(40))]

    fit(pages, RecordingProvider(), budget=25, cache=cache)
    stored_after_first = cache._conn.execute(
        "SELECT COUNT(*) FROM kv WHERE namespace = ?", (SUMMARY_NAMESPACE,)
    ).fetchone()[0]

    second = RecordingProvider()
    fit(pages, second, budget=25, always=True, cache=cache)

    assert second.calls == []
    stored_after_second = cache._conn.execute(
        "SELECT COUNT(*) FROM kv WHERE namespace = ?", (SUMMARY_NAMESPACE,)
    ).fetchone()[0]
    assert stored_after_second == stored_after_first


def test_a_broken_cache_is_a_miss_not_a_failure(fake_tokens):
    """Caching is an optimization, never a correctness input: a store that throws
    costs a re-summarize, not the crawl."""

    class BrokenCache:
        def get(self, namespace, key):
            raise RuntimeError("store is on fire")

        def put(self, namespace, key, value):
            raise RuntimeError("still on fire")

    provider = RecordingProvider()
    _text, summarized, *_ = fit(
        [("https://a/1", words(40))], provider, budget=25, cache=BrokenCache()
    )

    assert summarized is True
    assert provider.calls


@pytest.mark.parametrize("budget", [0, -1])
def test_a_nonsense_budget_does_not_hang_or_divide_by_zero(budget, fake_tokens):
    """`max(1, budget)` is what stops a zero chunk size from producing an empty
    split and an infinite reduce loop."""
    provider = RecordingProvider()
    _text, summarized, *_ = fit([("https://a/1", words(30))], provider, budget=budget)
    assert summarized is True
