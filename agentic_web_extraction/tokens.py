"""Token counting and token-aware text splitting, backed by tiktoken.

The consolidate-then-extract flow needs to know whether the concatenated page
content fits the extraction model's context window, and -- when it doesn't --
to split content into windows the summarizer can chew on. Both are token
operations, so this module wraps tiktoken.

tiktoken's `encoding_for_model` only knows the models OpenAI has shipped a
mapping for; a swappable-provider setup routinely uses names it has never heard
of (a future OpenAI model like ``gpt-5.5``, or a non-OpenAI model like
``gemma-4-26b-a4b-it`` served over an OpenAI-compatible endpoint). So we fall
back to a configurable *base* encoding (``o200k_base`` by default, the encoding
the current OpenAI frontier models use). For non-OpenAI models the count is only
an approximation -- which is fine: it drives a fit-or-summarize decision with
headroom baked into the default budget, not billing.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

DEFAULT_ENCODING = "o200k_base"


@lru_cache(maxsize=32)
def _encoding(model: str, encoding_name: str) -> tiktoken.Encoding:
    """Resolve an encoding for `model`, falling back to `encoding_name`.

    Cached so repeated calls (one per page, plus every summarization chunk)
    don't re-parse the encoding tables. Try the model-specific mapping first;
    on any miss (unknown model) use the configured base encoding; if even that
    name is bad, fall back to the hardcoded default so token counting can never
    hard-fail a crawl.
    """
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        pass
    try:
        return tiktoken.get_encoding(encoding_name)
    except (ValueError, KeyError):
        return tiktoken.get_encoding(DEFAULT_ENCODING)


def count_tokens(text: str, model: str, encoding_name: str = DEFAULT_ENCODING) -> int:
    """Estimated token count of `text` for `model` (see module docstring)."""
    if not text:
        return 0
    return len(_encoding(model, encoding_name).encode(text))


def split_by_tokens(
    text: str,
    max_tokens: int,
    model: str,
    encoding_name: str = DEFAULT_ENCODING,
) -> list[str]:
    """Split `text` into consecutive chunks each at most `max_tokens` tokens.

    Slices the token stream and decodes each slice back to text -- token-exact,
    so no chunk can overflow the summarizer's window. Coarse (it cuts mid-
    sentence at a token boundary), which is acceptable for summarization input.
    Returns ``[text]`` unchanged when it already fits or `max_tokens` is
    non-positive.
    """
    if max_tokens <= 0:
        return [text]
    enc = _encoding(model, encoding_name)
    ids = enc.encode(text)
    if len(ids) <= max_tokens:
        return [text]
    return [enc.decode(ids[i : i + max_tokens]) for i in range(0, len(ids), max_tokens)]
