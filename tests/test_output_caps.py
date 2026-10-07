"""Output caps on the structured calls (screen, score_links, extract).

Drives OpenAIProvider against a fake client that records each request, so nothing
reaches the network. Every cap knob is passed explicitly, so AWE_* in the
environment (or a developer's .env) can't decide whether a test passes.
"""

import httpx
import pytest
from openai import BadRequestError, UnprocessableEntityError, omit
from pydantic import BaseModel, SecretStr, ValidationError

from agentic_web_extraction.config import Settings
from agentic_web_extraction.providers import openai_provider
from agentic_web_extraction.providers.openai_provider import (
    OpenAIProvider,
    _LinkScore,
    _LinkScores,
    _ScreenSchema,
)

LINKS = [("a", "https://example.org/a"), ("b", "https://example.org/b")]
USAGE = {"input_tokens": 10, "output_tokens": 11700, "total_tokens": 11710}

CAPS = {
    "score_output_tokens_per_link": 100,
    "screen_output_tokens": 1000,
    "reasoning_output_tokens": 4000,
    "screen_model_max_output_tokens": 0,
    "max_output_tokens": 0,
}


class _Response:
    def __init__(self, parsed, usage=None):
        self.output_parsed = parsed
        self.usage = usage
        self.status = "incomplete" if parsed is None else "completed"
        self.incomplete_details = (
            type("D", (), {"reason": "max_output_tokens"})() if parsed is None else None
        )


class _Raw:
    """Stand-in for the SDK's raw-response wrapper."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.http_response = httpx.Response(200, json={"usage": USAGE})

    def parse(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Response(self.outcome)


class _FakeRawResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, (BadRequestError, UnprocessableEntityError)):
            raise outcome
        return _Raw(outcome)


class _FakeResponses:
    def __init__(self, outcomes):
        self.with_raw_response = _FakeRawResponses(outcomes)


class _FakeClient:
    def __init__(self, outcomes):
        self.responses = _FakeResponses(outcomes)


def _provider(*outcomes, **updates) -> tuple[OpenAIProvider, list[dict]]:
    settings = Settings(
        openai_api_key=SecretStr("sk-test"), llm_cache="", log_file=""
    ).model_copy(update={**CAPS, **updates})
    provider = OpenAIProvider(settings=settings)
    client = _FakeClient(outcomes)
    provider._client = client  # ty: ignore[invalid-assignment]
    return provider, client.responses.with_raw_response.calls


def _cap_refused(message="max_output_tokens is too large", param=None, status=400):
    request = httpx.Request("POST", "https://api.example/v1/responses")
    error = BadRequestError if status == 400 else UnprocessableEntityError
    return error(
        message,
        response=httpx.Response(status, request=request),
        body={"param": param} if param else None,
    )


def test_screen_model_caps_are_on_by_default():
    # Read off the fields, not a constructed Settings, for the reason above.
    fields = Settings.model_fields
    assert fields["score_output_tokens_per_link"].default == 100
    assert fields["screen_output_tokens"].default == 1000
    assert fields["reasoning_output_tokens"].default == 4000
    assert fields["screen_model_max_output_tokens"].default == 0


def test_score_cap_counts_links_and_their_urls(fake_tokens):
    parsed = _LinkScores(scores=[_LinkScore(url=LINKS[0][1], score=0.7)])
    provider, calls = _provider(parsed)

    scores = provider.score_links(LINKS, "page", "criterion")

    # fake_tokens counts whitespace words: each URL is one "token".
    assert calls[0]["max_output_tokens"] == 4000 + len(LINKS) + 100 * len(LINKS)
    assert scores == [(LINKS[0][1], 0.7), (LINKS[1][1], 0.0)]


def test_long_urls_raise_the_score_cap(fake_tokens):
    # The scorer echoes every URL, so a page of long URLs needs more room.
    long_links = [(a, f"{u} {'seg ' * 50}") for a, u in LINKS]
    provider, calls = _provider(_LinkScores(scores=[]), _LinkScores(scores=[]))

    provider.score_links(LINKS, "page", "criterion")
    provider.score_links(long_links, "page", "criterion")

    assert calls[1]["max_output_tokens"] - calls[0]["max_output_tokens"] == 100


@pytest.mark.parametrize("per_link", [0, -1])
def test_non_positive_per_link_sends_no_score_cap(fake_tokens, per_link):
    # A negative value must not become a negative (or tiny) cap that the endpoint
    # rejects on every call, leaving every page's links unscored.
    provider, calls = _provider(
        _LinkScores(scores=[]), score_output_tokens_per_link=per_link
    )

    provider.score_links(LINKS, "page", "criterion")

    assert calls[0]["max_output_tokens"] is omit


def test_screen_is_capped_by_default():
    provider, calls = _provider(_ScreenSchema(match=True, reason="r"))

    provider.screen("page", "criterion")

    assert calls[0]["max_output_tokens"] == 4000 + 1000


@pytest.mark.parametrize("answer", [0, -1])
def test_non_positive_screen_output_sends_no_cap(answer):
    provider, calls = _provider(
        _ScreenSchema(match=True, reason="r"), screen_output_tokens=answer
    )

    provider.screen("page", "criterion")

    assert calls[0]["max_output_tokens"] is omit


def test_known_endpoint_limit_clamps_both_caps(fake_tokens):
    many = [(str(i), f"https://example.org/{i}") for i in range(500)]
    provider, calls = _provider(
        _LinkScores(scores=[]),
        _ScreenSchema(match=True, reason="r"),
        screen_model_max_output_tokens=4500,
    )

    provider.score_links(many, "page", "criterion")
    provider.screen("page", "criterion")

    assert calls[0]["max_output_tokens"] == 4500
    assert calls[1]["max_output_tokens"] == 4500


@pytest.mark.parametrize(
    "refusal",
    [
        _cap_refused(param="max_output_tokens"),
        _cap_refused("This model's maximum context length is 32768 tokens."),
        _cap_refused("max_output_tokens: must be <= 16384", status=422),
    ],
)
def test_refused_cap_is_retried_once_without_one(fake_tokens, refusal):
    # Before the caps existed this call went out uncapped and worked; an endpoint
    # whose limit is below the computed cap must not turn it into a failure.
    provider, calls = _provider(refusal, _LinkScores(scores=[]))

    assert provider.score_links(LINKS, "page", "criterion") == [
        (LINKS[0][1], 0.0),
        (LINKS[1][1], 0.0),
    ]
    assert isinstance(calls[0]["max_output_tokens"], int)
    assert calls[1]["max_output_tokens"] is omit


def test_unrelated_400_is_not_retried(fake_tokens):
    provider, calls = _provider(_cap_refused("invalid model", param="model"))

    with pytest.raises(BadRequestError):
        provider.score_links(LINKS, "page", "criterion")
    assert len(calls) == 1


def test_cutoff_mid_json_still_counts_billed_tokens(fake_tokens):
    # The cap cuts the JSON off mid-string, so the SDK's parse raises before a
    # response object exists -- the tokens were billed all the same.
    try:
        _LinkScores.model_validate_json('{"scores": [{"url": "https://exa')
    except ValidationError as e:
        cutoff = e
    provider, _ = _provider(cutoff)

    with pytest.raises(ValidationError):
        provider.score_links(LINKS, "page", "criterion")

    usage = provider.usage_by_function["score_links"]
    assert (usage.calls, usage.output_tokens) == (1, 11700)


def test_cutoff_without_parsed_object_raises_with_reason(fake_tokens):
    provider, _ = _provider(None)

    with pytest.raises(AssertionError, match="reason=max_output_tokens"):
        provider.score_links(LINKS, "page", "criterion")


class _Thing(BaseModel):
    name: str


def test_extract_cap_is_off_by_default_and_sent_when_set():
    provider, calls = _provider(_Thing(name="a"), _Thing(name="b"))
    provider.extract("content", _Thing)
    provider.settings.max_output_tokens = 9000
    provider.extract("content", _Thing)

    assert calls[0]["max_output_tokens"] is omit
    assert calls[1]["max_output_tokens"] == 9000


def test_non_positive_total_cap_is_not_sent():
    # A negative reasoning allowance must not put a negative cap on the wire,
    # which the endpoint would refuse on every call.
    provider, calls = _provider(
        _ScreenSchema(match=True, reason="r"), reasoning_output_tokens=-5000
    )

    provider.screen("page", "criterion")

    assert calls[0]["max_output_tokens"] is omit


def test_explicit_extract_cap_is_not_dropped_on_refusal():
    # The caller set it as a runaway backstop; trading it for an uncapped call
    # would silently restore the failure it exists to prevent.
    provider, calls = _provider(
        _cap_refused(param="max_output_tokens"), max_output_tokens=200_000
    )

    with pytest.raises(BadRequestError):
        provider.extract("content", _Thing)
    assert len(calls) == 1


def test_score_cap_survives_an_unavailable_tokenizer(monkeypatch):
    # Sizing the cap is not worth the call: an encoding that can't load (offline,
    # first use) falls back to characters instead of failing every page's scoring.
    def broken(*_args, **_kwargs):
        raise OSError("no network")

    monkeypatch.setattr(openai_provider, "count_tokens", broken)
    provider, calls = _provider(_LinkScores(scores=[]))

    provider.score_links(LINKS, "page", "criterion")

    urls = "\n".join(url for _, url in LINKS)
    assert calls[0]["max_output_tokens"] == 4000 + len(urls) + 100 * len(LINKS)
