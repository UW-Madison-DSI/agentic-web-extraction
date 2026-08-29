"""The cache keys, which are the only thing standing between a prompt edit and a
crawl that silently replays the old prompt's answers.

The cache is on by default and persists across runs, so a key that fails to
change when it should is not a slow crawl -- it is a wrong result served
confidently, from a store nobody looks inside. These are the invalidation rules
written down as assertions.
"""

import json

import pytest
from pydantic import BaseModel

from agentic_web_extraction.cache import (
    PAGE_NAMESPACE,
    SqliteKVCache,
    content_hash,
    extract_cache_key,
    page_cache_version,
)

from .conftest import Doc, StubWeb, page


def stamp(
    *,
    criteria: str = "grants for PIs",
    schema_json: str = '{"type":"object"}',
    prompt_signature: str = "v1",
    model_screen: str = "small",
    model_extract: str = "big",
    normalize: bool = True,
) -> str:
    """A version stamp over a fixed baseline, with one input at a time varied."""
    return page_cache_version(
        criteria=criteria,
        schema_json=schema_json,
        prompt_signature=prompt_signature,
        model_screen=model_screen,
        model_extract=model_extract,
        normalize=normalize,
    )


def test_the_version_stamp_is_stable_for_identical_inputs():
    assert stamp() == stamp()


@pytest.mark.parametrize(
    "field, value",
    [
        ("criteria", "something else entirely"),
        ("schema_json", '{"type":"object","properties":{}}'),
        ("prompt_signature", "v2"),
        ("model_screen", "small-2"),
        ("model_extract", "big-2"),
        ("normalize", False),
    ],
)
def test_every_input_that_changes_an_answer_changes_the_stamp(field, value):
    """Each of these decides what the model is asked or which model answers, so a
    stamp that survived any of them would replay an answer to a different
    question."""
    assert stamp(**{field: value}) != stamp()


def test_the_stamp_does_not_collide_across_field_boundaries():
    """Fields are joined with a NUL separator rather than concatenated: without
    one, moving a character from the end of the criterion to the start of the
    schema would produce the same stamp for two genuinely different configs."""
    assert stamp(criteria="ab", schema_json="c") != stamp(
        criteria="a", schema_json="bc"
    )


def test_the_extract_key_ignores_the_order_pages_were_gathered_in():
    """Waves finish in a nondeterministic order, so an order-sensitive key would
    miss at random on a crawl that found exactly the same pages."""
    keys = ["page-a", "page-b", "page-c"]
    assert extract_cache_key(keys) == extract_cache_key(list(reversed(keys)))


def test_the_extract_key_changes_when_the_set_of_pages_changes():
    assert extract_cache_key(["a", "b"]) != extract_cache_key(["a", "b", "c"])
    assert extract_cache_key(["a", "b"]) != extract_cache_key(["a", "c"])
    assert extract_cache_key(["a", "b"]) != extract_cache_key(["a"])


def test_the_extract_key_separates_its_members():
    """The same NUL-separator argument as the stamp, one level up: `["ab"]` and
    `["a", "b"]` are different sets of contributing pages."""
    assert extract_cache_key(["ab"]) != extract_cache_key(["a", "b"])


def test_content_hash_tracks_the_text_the_model_sees():
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("hello ")


# --- the keys the Extractor actually writes ---------------------------------

SEED = "https://key-test.org/a"
OTHER = "https://key-test.org/b"


def written_page_keys(cache: SqliteKVCache) -> list[str]:
    rows = cache._conn.execute(
        "SELECT key FROM kv WHERE namespace = ?", (PAGE_NAMESPACE,)
    ).fetchall()
    return sorted(row[0] for row in rows)


@pytest.fixture
def cache(tmp_path) -> SqliteKVCache:
    return SqliteKVCache(tmp_path / "kv.sqlite")


def test_the_page_key_carries_the_url_so_two_pages_never_share_an_entry(
    make_extractor, cache, fake_tokens
):
    """Identical content at two URLs is a real thing (a listing and its canonical
    alias), and their link scores are relative to their own base URL."""
    body = page()
    web = StubWeb({SEED: body, OTHER: body})
    make_extractor(web, cache=cache).extract([SEED, OTHER])

    keys = written_page_keys(cache)
    assert len(keys) == 2
    # `version:hash:url`, split from the left: the URL is the last segment and
    # contains colons of its own, so it is not safe to split from the right.
    stamps, hashes, urls = zip(*(k.split(":", 2) for k in keys))
    assert set(urls) == {SEED, OTHER}
    # Same content, so the hash segment matches: only the URL tells them apart.
    assert len(set(hashes)) == 1
    assert len(set(stamps)) == 1


def test_prefer_seed_domain_gets_its_own_key_space(make_extractor, cache, fake_tokens):
    """The signal changes what the screen and scorer are told, so an entry written
    with it on must not be replayed for a run with it off."""
    web = StubWeb({SEED: page()})
    make_extractor(web, cache=cache).extract(SEED)
    plain = written_page_keys(cache)

    make_extractor(
        StubWeb({SEED: page()}), cache=cache, prefer_seed_domain=True
    ).extract(SEED)
    both = written_page_keys(cache)

    assert len(both) == 2
    added = [k for k in both if k not in plain]
    assert "seeddom=key-test.org" in added[0]


def test_seed_is_content_gets_its_own_key_space(make_extractor, cache, fake_tokens):
    """Direct mode forces a match and scores no links, so its entry says nothing
    about what a screened run would have decided."""
    web = StubWeb({SEED: page()})
    make_extractor(web, cache=cache).extract(SEED)
    plain = written_page_keys(cache)

    make_extractor(StubWeb({SEED: page()}), cache=cache).extract(
        SEED, seed_is_content=True
    )
    both = written_page_keys(cache)

    assert len(both) == 2
    assert ":direct:" in [k for k in both if k not in plain][0]


def test_a_changed_criterion_misses_the_stored_page(make_extractor, cache, fake_tokens):
    """The end-to-end version of the stamp test: the same page, a different
    question, and no reuse of the old answer."""
    make_extractor(StubWeb({SEED: page()}), cache=cache).extract(SEED)
    first = written_page_keys(cache)

    extractor = make_extractor(StubWeb({SEED: page()}), cache=cache)
    extractor.criteria = "a completely different question"
    extractor._cache_version = page_cache_version(
        criteria=extractor.criteria,
        schema_json=json.dumps(Doc.model_json_schema(), sort_keys=True),
        prompt_signature="stub-v1",
        model_screen="stub-screen",
        model_extract="stub-extract",
        normalize=True,
    )
    extractor.extract(SEED)

    assert len(written_page_keys(cache)) == len(first) + 1


class OtherDoc(BaseModel):
    """A different extraction target for the same pages."""

    titles: list[str] = []


def test_a_different_schema_misses_the_stored_page(make_extractor, cache, fake_tokens):
    make_extractor(StubWeb({SEED: page()}), cache=cache).extract(SEED)
    first = written_page_keys(cache)

    extractor = make_extractor(StubWeb({SEED: page()}), cache=cache)
    extractor.schema = OtherDoc
    extractor._cache_version = page_cache_version(
        criteria=extractor.criteria,
        schema_json=json.dumps(OtherDoc.model_json_schema(), sort_keys=True),
        prompt_signature="stub-v1",
        model_screen="stub-screen",
        model_extract="stub-extract",
        normalize=True,
    )
    extractor.extract(SEED)

    assert len(written_page_keys(cache)) == len(first) + 1


def test_the_link_cap_gets_its_own_key_space(
    make_extractor, cache, settings, fake_tokens
):
    """The cap decides which links get scored, so it decides the `link_scores` the
    entry stores. Without it in the key, raising the cap would replay the old
    truncated list -- the same failure the crawl boundary is kept out of the worker
    to avoid."""
    body = (
        "<html><body><p>x</p>"
        + "".join(f'<a href="/p{i}">l{i}</a>' for i in range(10))
        + "</body></html>"
    )

    make_extractor(
        StubWeb({SEED: body}),
        cache=cache,
        settings=settings.model_copy(update={"max_links_per_page": 2}),
    ).extract(SEED, max_fetches=1)
    capped = written_page_keys(cache)
    assert "links=2" in capped[0]

    make_extractor(
        StubWeb({SEED: body}),
        cache=cache,
        settings=settings.model_copy(update={"max_links_per_page": 5}),
    ).extract(SEED, max_fetches=1)

    assert len(written_page_keys(cache)) == 2


def test_no_cap_leaves_the_key_shape_alone(make_extractor, cache, fake_tokens):
    """Off by default, so the default key must look exactly as it did before the
    cap existed -- otherwise upgrading invalidates every stored page."""
    make_extractor(StubWeb({SEED: page()}), cache=cache).extract(SEED)
    assert "links=" not in written_page_keys(cache)[0]
