"""score_links output cap: on by default, sized per link, 0 disables it.

Drives OpenAIProvider against a fake client that records the request, so nothing
reaches the network.
"""

from types import SimpleNamespace

import pytest
from openai import omit
from pydantic import SecretStr

from agentic_web_extraction.config import Settings
from agentic_web_extraction.providers.openai_provider import (
    _SCORE_OUTPUT_BASE_TOKENS,
    OpenAIProvider,
    _LinkScore,
    _LinkScores,
)

LINKS = [("a", "https://example.org/a"), ("b", "https://example.org/b")]


class _FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.parsed,
            usage=None,
            status="incomplete" if self.parsed is None else "completed",
            incomplete_details=(
                SimpleNamespace(reason="max_output_tokens")
                if self.parsed is None
                else None
            ),
        )


def _provider(parsed, **updates) -> tuple[OpenAIProvider, _FakeResponses]:
    settings = Settings(
        openai_api_key=SecretStr("sk-test"), llm_cache="", log_file="", **updates
    )
    provider = OpenAIProvider(settings=settings)
    responses = _FakeResponses(parsed)
    provider._client = SimpleNamespace(responses=responses)  # ty: ignore[invalid-assignment]
    return provider, responses


def test_cap_is_on_by_default():
    # Read off the field, not a constructed Settings: AWE_* in the environment
    # (or a developer's .env) would otherwise decide whether this passes.
    assert Settings.model_fields["score_output_tokens_per_link"].default == 100


def test_cap_scales_with_link_count():
    parsed = _LinkScores(scores=[_LinkScore(url=LINKS[0][1], score=0.7)])
    provider, responses = _provider(parsed, score_output_tokens_per_link=100)

    scores = provider.score_links(LINKS, "page", "criterion")

    assert responses.calls[0]["max_output_tokens"] == (
        _SCORE_OUTPUT_BASE_TOKENS + 100 * len(LINKS)
    )
    assert scores == [(LINKS[0][1], 0.7), (LINKS[1][1], 0.0)]


def test_zero_sends_no_cap():
    provider, responses = _provider(
        _LinkScores(scores=[]), score_output_tokens_per_link=0
    )

    provider.score_links(LINKS, "page", "criterion")

    assert responses.calls[0]["max_output_tokens"] is omit


def test_negative_sends_no_cap():
    # A negative value must not become a negative (or tiny) cap that the endpoint
    # rejects on every call, leaving every page's links unscored.
    provider, responses = _provider(
        _LinkScores(scores=[]), score_output_tokens_per_link=-1
    )

    provider.score_links(LINKS, "page", "criterion")

    assert responses.calls[0]["max_output_tokens"] is omit


def test_cutoff_without_parsed_object_raises_with_reason():
    provider, _ = _provider(None)

    with pytest.raises(AssertionError, match="reason=max_output_tokens"):
        provider.score_links(LINKS, "page", "criterion")
