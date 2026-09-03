"""Structured progress events, and the `awe schema` surface.

Both exist for the same reason: before them, a host codebase's only way to learn
anything about a crawl in flight was to parse stderr, and its only way to learn
what a setting was called was to read the README.
"""

import json
from collections.abc import Callable

from typer.testing import CliRunner

from agentic_web_extraction import logsink
from agentic_web_extraction.cli import app
from agentic_web_extraction.config import Settings, settings_schema

from .conftest import StubWeb, page

SEED = "https://event-test.org/"


def collect() -> tuple[list[logsink.Event], Callable[[logsink.Event], None]]:
    events: list[logsink.Event] = []
    return events, events.append


def test_a_subscriber_sees_the_traversal(make_extractor, fake_tokens):
    events, sink = collect()
    web = StubWeb({SEED: page()})

    make_extractor(web, on_event=sink).extract(SEED)

    assert any(e.kind == "traverse" for e in events)
    assert any(e.kind == "page" for e in events)
    assert any(e.kind == "fetch" for e in events)


def test_the_kind_comes_from_the_line_tag(make_extractor, fake_tokens):
    """The library's log lines already carry a bracketed tag by convention, so a
    subscriber can branch on `kind` without parsing prose. Best-effort, not a
    stable API -- which is why the message is carried verbatim alongside it."""
    events, sink = collect()

    make_extractor(StubWeb({SEED: page()}), on_event=sink).extract(SEED)

    fetches = [e for e in events if e.kind == "fetch"]
    assert fetches and fetches[0].message.lstrip().startswith("[fetch]")


def test_an_untagged_line_has_an_empty_kind():
    events, sink = collect()
    with logsink.subscribed(sink):
        logsink.emit("no tag on this one")
    assert events == [logsink.Event(kind="", message="no tag on this one")]


def test_an_explicit_kind_overrides_the_derived_one():
    events, sink = collect()
    with logsink.subscribed(sink):
        logsink.emit("[fetch] something", kind="custom")
    assert events[0].kind == "custom"


def test_the_subscription_ends_with_the_crawl(make_extractor, fake_tokens):
    """Scoped to `extract` rather than to construction, so a subscriber never sees
    lines from a crawl it did not ask about -- and so a long-lived Extractor does
    not accumulate a listener that outlives its caller."""
    events, sink = collect()
    make_extractor(StubWeb({SEED: page()}), on_event=sink).extract(SEED)
    before = len(events)

    logsink.emit("[after] this line belongs to nobody")

    assert len(events) == before


def test_events_reach_the_subscriber_from_other_modules(make_extractor, fake_tokens):
    """It hooks the sink, not the Extractor's own `_log`: the transport, robots
    and fallback lines are emitted from their own modules and are among the most
    interesting things a progress display can show."""
    events, sink = collect()

    with logsink.subscribed(sink):
        logsink.emit("    [transport] pretend this came from fetch.py")

    assert any(e.kind == "transport" for e in events)


def test_a_subscriber_that_raises_does_not_break_the_crawl(make_extractor, fake_tokens):
    """A progress display is not allowed to cost a crawl that is otherwise
    working."""

    def hostile(event):
        raise RuntimeError("the display is on fire")

    result = make_extractor(StubWeb({SEED: page()}), on_event=hostile).extract(SEED)

    assert result.pages_fetched == 1


def test_no_subscriber_is_the_default_and_costs_nothing(make_extractor, fake_tokens):
    result = make_extractor(StubWeb({SEED: page()})).extract(SEED)
    assert result.pages_fetched == 1


def test_a_subscriber_may_log_without_deadlocking():
    """`logsink._lock` is not reentrant, so subscribers are notified outside it.
    Logging from a subscriber is a plausible thing to do -- forwarding to another
    logger is half the point -- and it must not hang the crawl."""
    seen: list[str] = []

    def chatty(event):
        if not seen:
            seen.append(event.message)
            logsink.emit("[echo] subscriber logged something")

    with logsink.subscribed(chatty):
        logsink.emit("[first] hello")

    assert seen == ["[first] hello"]


# --- the published settings schema -------------------------------------------


def test_the_schema_names_every_setting():
    """Including the credentials, which pydantic renders under their aliases
    rather than their field names."""
    properties = settings_schema()["properties"]
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        assert (alias if isinstance(alias, str) else name) in properties


def test_the_schema_carries_types_and_defaults():
    properties = settings_schema()["properties"]
    assert properties["request_delay"]["default"] == 0.5
    assert properties["max_fetches"]["type"] == "integer"
    assert properties["use_sitemap"]["default"] is True
    assert properties["max_links_per_page"]["default"] == 0


def test_every_property_names_the_variable_that_sets_it():
    """Otherwise a consumer has to know that most settings take an `AWE_` prefix
    and the credentials do not -- which is the convention this is meant to spare
    them from learning."""
    properties = settings_schema()["properties"]
    assert properties["request_delay"]["env"] == "AWE_REQUEST_DELAY"
    assert properties["max_fetches"]["env"] == "AWE_MAX_FETCHES"
    assert properties["OPENAI_API_KEY"]["env"] == "OPENAI_API_KEY"
    assert all("env" in prop for prop in properties.values())


def test_the_schema_does_not_read_the_environment(monkeypatch):
    """It describes the settings; it does not resolve them. That is what makes the
    output safe to print, log, or serve -- a schema that reflected the live
    environment would put whatever `AWE_USER_AGENT` was set to into anything that
    published it."""
    monkeypatch.setenv("AWE_USER_AGENT", "secret-internal-crawler/9")
    assert "secret-internal-crawler" not in json.dumps(settings_schema())


def test_awe_schema_prints_valid_json():
    result = CliRunner().invoke(app, ["schema"])
    assert result.exit_code == 0
    assert "request_delay" in json.loads(result.stdout)["properties"]
