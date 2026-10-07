import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, Protocol, TypeVar

import httpx
from openai import (
    APIStatusError,
    BadRequestError,
    OpenAI,
    Omit,
    RateLimitError,
    UnprocessableEntityError,
    omit,
)
from openai.types.responses import ParsedResponse
from pydantic import BaseModel, Field

from .. import logsink
from ..config import Settings
from ..result import ScreenVerdict, Usage
from ..schema_outline import schema_outline_safe
from ..tokens import count_tokens

DEFAULT_SCREEN_PROMPT = (
    "You are a precise relevance judge. Decide if the PAGE matches the CRITERION.\n"
    "Return match=true only if the page itself is the target — not a navigation page that\n"
    "merely links to candidates. Provide a one-sentence reason."
)

DEFAULT_SCORE_PROMPT = (
    "You are ranking outgoing links on a web page by how likely each one leads to a page\n"
    "that satisfies the CRITERION. Score each URL from 0.0 (irrelevant) to 1.0 (almost\n"
    "certainly the target). Use anchor text and URL structure. Return one entry per\n"
    "input URL, preserving the URL string exactly."
)

DEFAULT_EXTRACT_PROMPT = (
    "Extract the requested fields from the CONTENT. The content may be the concatenated\n"
    "text of several source pages (each introduced by a '--- SOURCE: <url>' marker); "
    "extract everything the schema asks for across all of it. If a field is not present,\n"
    "leave it null where the schema permits, otherwise infer the most reasonable value\n"
    "from the text. Do not fabricate."
)

DEFAULT_SUMMARIZE_PROMPT = (
    "You are compressing web page content so it fits a downstream extraction model's\n"
    "context window. Rewrite the CONTENT far more concisely while preserving every "
    "detail relevant to the CRITERION -- every name, date, number, identifier, URL,\n"
    "and any other concrete fact a structured extraction might need. Drop boilerplate,\n"
    "navigation, and anything irrelevant to the criterion. Do not add information that\n"
    "is not present. Output only the condensed text."
)

# Appended to the summarize instructions (followed by the rendered schema outline)
# whenever the caller passes the extraction schema. Summarization is the pipeline's
# only lossy step -- the extraction model never sees the original text -- and the
# criterion alone says what is *topically* relevant, not which concrete values a
# schema field will demand. The wording is deliberately framed as a retention list
# rather than a task change: a summarizer that starts emitting JSON would be doing
# the extraction on the cheap model, locking in early mistakes and throwing away the
# context the strong model uses to disambiguate.
SUMMARIZE_SCHEMA_GUIDANCE = (
    "\n\nRETENTION TARGET: a downstream model will populate the schema below from your "
    "output alone -- it never sees the original text. Any fact that could fill any of "
    "these fields must survive in your summary, and literal values (numbers, dates, "
    "quantities, names, identifiers, codes, URLs) must be copied exactly -- never "
    "paraphrase, round, normalize, or abbreviate them. Where a field holds a list of "
    "records, keep the records separated and keep each record's details attached to "
    "it, so details from one cannot be misread as belonging to another. This tells you "
    "what to KEEP; it does not change your task -- output condensed prose, never JSON, "
    "and do not attempt to fill the schema yourself.\n\nTARGET SCHEMA:\n"
)

# Appended to the screen / score instructions only when the caller opts into the
# soft same-domain preference (Extractor(prefer_seed_domain=True)). Both feed the
# LLM a Python-computed on-domain signal and ask it to *disfavor* off-domain
# content -- a nudge, not a hard exclusion.
SCREEN_DOMAIN_PREFERENCE = (
    "\n\nDOMAIN PREFERENCE: You are given SEED_URL (where the crawl started), the "
    "PAGE_URL, and ON_SEED_DOMAIN (whether the page is on the seed's registrable "
    "domain). Disfavor off-domain pages: when ON_SEED_DOMAIN is 'no', treat the page "
    "as less likely to be the target and require clearer evidence before returning "
    "match=true. This is a soft preference, not a hard rule -- a page that is clearly "
    "the target still matches even when off-domain."
)
SCORE_DOMAIN_PREFERENCE = (
    "\n\nDOMAIN PREFERENCE: You are given SEED_URL (where the crawl started), and each "
    "link is annotated with on_seed_domain (whether it is on the seed's registrable "
    "domain). Disfavor off-domain links: assign an off-domain link a lower score than "
    "an on-domain link of otherwise comparable promise. This is a soft preference, not "
    "a hard filter -- a clearly on-target off-domain link may still score highly."
)

PAGE_TRUNC_CHARS = 16000

# Service tiers (Settings.use_flex). "flex" bills at Batch-API rates -- 50% off, and
# stackable with prompt caching -- in exchange for latency and the possibility of
# being refused for lack of capacity. "auto" is the account default (standard
# pricing), used as the per-call fallback when flex has no capacity. Narrowed to the
# two tiers this provider uses; the SDK also accepts "default"/"scale"/"priority".
_Tier = Literal["auto", "flex"]
_TIER_FLEX: _Tier = "flex"
_TIER_STANDARD: _Tier = "auto"

# Read timeouts: flex responses can take far longer than standard ones, so the
# 600s that is ample for a large standard extraction is raised when flex is on.
_TIMEOUT_STANDARD = 600.0
_TIMEOUT_FLEX = 1800.0

_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)


class _RawParsed(Protocol[_T_co]):
    """What `responses.with_raw_response.parse(...)` returns, as far as we use it.

    The raw wrapper defers the SDK's structured parse to `.parse()`, so when that
    parse fails -- the usual shape of an output cap cutting the JSON off mid-string
    -- the billed usage is still readable off `http_response`.
    """

    http_response: httpx.Response

    def parse(self) -> ParsedResponse[_T_co]: ...


def _yn(on_seed_domain: bool | None) -> str:
    """Render the on-domain signal for the prompt (None = host unparseable)."""
    return {True: "yes", False: "no", None: "unknown"}[on_seed_domain]


def _rejects_output_cap(error: APIStatusError) -> bool:
    """True for a 400/422 that refuses the request's max_output_tokens.

    OpenAI names the parameter (`param`); OpenAI-compatible servers often don't,
    and a vLLM-style server refuses an input + cap that exceeds the context length
    with a "maximum context length" message instead. Some compatible servers
    validate request fields with a 422 rather than a 400, so both are accepted.
    Matched loosely on purpose: a false positive costs one uncapped retry of a
    call that failed anyway.
    """
    if getattr(error, "param", None) in ("max_output_tokens", "max_tokens"):
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in ("max_output_tokens", "max_tokens", "maximum context length")
    )


def _billed(raw: _RawParsed[object]) -> object:
    """A stand-in carrying only `usage`, for a response the SDK failed to parse.

    Read leniently, field by field, in the shape `_accumulate` reads: a strict
    model validation would drop the whole count over one missing detail field.
    """
    try:
        usage = raw.http_response.json().get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                input_tokens_details=SimpleNamespace(
                    cached_tokens=details.get("cached_tokens", 0)
                ),
            )
        )
    except Exception:  # noqa: BLE001 -- best-effort accounting, never the failure
        return SimpleNamespace(usage=None)


def _is_capacity_refusal(error: RateLimitError) -> bool:
    """True for the flex-specific 429 that means "no spare capacity right now".

    Flex refuses with `resource_unavailable`, which is *not* billed -- that one is
    worth re-running on the standard tier. An ordinary rate-limit 429 is not: the
    request is fine, the account is just over its limit, and silently escalating it
    to full price would defeat the point of asking for flex. So match on the error
    code, falling back to the message text when the SDK exposes no code.
    """
    code = getattr(error, "code", None)
    if code:
        return code == "resource_unavailable"
    return "resource_unavailable" in str(error).lower()


class _ScreenSchema(BaseModel):
    match: bool
    reason: str


class _LinkScore(BaseModel):
    url: str
    score: float = Field(ge=0.0, le=1.0)


class _LinkScores(BaseModel):
    scores: list[_LinkScore]


@dataclass
class OpenAIProvider:
    settings: Settings
    screen_prompt: str = DEFAULT_SCREEN_PROMPT
    score_prompt: str = DEFAULT_SCORE_PROMPT
    extract_prompt: str = DEFAULT_EXTRACT_PROMPT
    summarize_prompt: str = DEFAULT_SUMMARIZE_PROMPT

    def __post_init__(self) -> None:
        api_key = (
            self.settings.openai_api_key.get_secret_value()
            if self.settings.openai_api_key is not None
            else None
        )
        # max_retries and connect timeout raised above the SDK defaults (2 retries,
        # 5s connect): the crawl fires many calls concurrently across waves and
        # summarization, so a slow-to-connect or transiently-failing endpoint should
        # get more patience before it aborts a page. The read timeout is the SDK
        # default (600s) -- ample for a large extraction call -- and longer still on
        # flex, whose whole trade is latency for price.
        #
        # Note max_retries also covers 429s, so a flex capacity refusal is retried by
        # the SDK before `_tiered` ever sees it and drops to the standard tier. That
        # is the right order (a retry might land on free flex capacity; the fallback
        # costs double) and it is free -- refusals aren't billed -- just slow.
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.settings.openai_base_url,
            max_retries=5,
            timeout=httpx.Timeout(
                _TIMEOUT_FLEX if self.settings.use_flex else _TIMEOUT_STANDARD,
                connect=30.0,
            ),
        )
        if self.settings.use_flex:
            logsink.emit(
                f"[provider] service_tier={_TIER_FLEX} (Batch-API rates; falls back "
                f"to {_TIER_STANDARD} per call when flex capacity is unavailable)"
            )
        # Token usage bucketed by an opaque call-purpose tag ("screen",
        # "score_links", "summarize", "extract", or whatever a caller passes to
        # extract()). _function_model remembers which model each tag ran on, so cost
        # can be reconstructed without baking a tag->model map anywhere downstream.
        # A lock guards both dicts: parallel waves call screen/score_links (and the
        # summarizer runs chunks) concurrently, all funnelling through _accumulate.
        self._usage_lock = threading.Lock()
        self._usage_by_function: dict[str, Usage] = {}
        self._function_model: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model_screen(self) -> str:
        return self.settings.model_screen

    @property
    def model_extract(self) -> str:
        return self.settings.model_extract

    @property
    def prompt_signature(self) -> str:
        """Stable fingerprint of every prompt template this provider sends.

        Covers the four base instructions plus the conditional appendices (the two
        same-domain-preference blocks and the summarize retention block), so editing
        any of them (whether the module defaults or a per-instance override) busts
        the page cache. The appendices are static constants, but including them keeps
        the signature complete even if they change. The rendered schema outline is
        deliberately absent: the schema's JSON already feeds the version stamp
        directly (see `page_cache_version`), so a schema change invalidates without
        it. Order is fixed so the string is deterministic.
        """
        parts = [
            self.screen_prompt,
            self.score_prompt,
            self.extract_prompt,
            self.summarize_prompt,
            SCREEN_DOMAIN_PREFERENCE,
            SCORE_DOMAIN_PREFERENCE,
            SUMMARIZE_SCHEMA_GUIDANCE,
        ]
        return "\x00".join(parts)

    @property
    def usage_by_function(self) -> dict[str, Usage]:
        with self._usage_lock:
            return dict(self._usage_by_function)

    @property
    def function_model(self) -> dict[str, str]:
        with self._usage_lock:
            return dict(self._function_model)

    def _tiered(self, send: Callable[[_Tier | Omit], _T]) -> _T:
        """Issue one request, on the configured service tier, via `send(tier)`.

        With flex off, `omit` is passed and no `service_tier` reaches the wire, so
        the account default applies -- the pre-flex behavior, byte for byte. With
        flex on, the request goes out at `service_tier="flex"` (Batch-API rates) and
        a capacity refusal is retried once on the standard tier, so a run never
        fails purely for want of flex capacity. Any other error propagates to the
        caller's logging/handling.

        Per-call rather than per-run: refusals are momentary, so one page dropping
        to standard shouldn't push the rest of the crawl off flex. `send` takes the
        tier (rather than this method taking **kwargs) so each call site keeps its
        concrete, type-checked argument list.
        """
        if not self.settings.use_flex:
            return send(omit)
        try:
            return send(_TIER_FLEX)
        except RateLimitError as e:
            if not _is_capacity_refusal(e):
                raise
            logsink.emit(
                f"    [llm] flex capacity unavailable; retrying on "
                f"{_TIER_STANDARD} tier (standard pricing)"
            )
            return send(_TIER_STANDARD)

    def _accumulate(self, response: object, model: str, function: str) -> Usage:
        """Add this response's tokens to the per-function running total and return the delta."""
        u = getattr(response, "usage", None)
        if u is None:
            delta = Usage(calls=1)
        else:
            details = getattr(u, "input_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            delta = Usage(
                input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                calls=1,
                cached_input_tokens=cached,
            )
        # Guarded: concurrent waves accumulate through here from worker threads.
        with self._usage_lock:
            self._usage_by_function[function] = (
                self._usage_by_function.get(function, Usage()) + delta
            )
            self._function_model[function] = model
        return delta

    def _log_call(
        self,
        step: str,
        model: str,
        in_chars: int,
        elapsed: float,
        delta: Usage | None,
        error: BaseException | None = None,
    ) -> None:
        if delta is None:
            tok = "tok=?"
        else:
            cached = (
                f"(cached {delta.cached_input_tokens})"
                if delta.cached_input_tokens
                else ""
            )
            tok = f"tok_in={delta.input_tokens}{cached} tok_out={delta.output_tokens}"
        status = f"FAIL:{type(error).__name__}" if error is not None else "ok"
        logsink.emit(
            f"    [llm {step}] model={model} in_chars={in_chars} "
            f"elapsed={elapsed:.2f}s {tok} {status}"
        )

    def _screen_model_cap(self, wanted: int) -> int:
        """Clamp a screen-model output cap to the endpoint's limit, when known."""
        ceiling = self.settings.screen_model_max_output_tokens
        return min(wanted, ceiling) if ceiling > 0 else wanted

    def _structured_call(
        self,
        send: Callable[[_Tier | Omit, int | Omit], _RawParsed[_T]],
        *,
        cap: int | None,
        retry_uncapped: bool,
        what: str,
        step: str,
        model: str,
        function: str,
        in_chars: int,
    ) -> _T:
        """Issue one structured-output call and return its parsed object.

        Shared by screen, score_links and extract, which differ only in the request
        `send(tier, cap)` builds. Three things happen here so they can't drift apart:

        - With `retry_uncapped`, an endpoint that refuses `cap` (a 400, or a 422)
          gets the call once more without one, logged. That is for the screen-model caps,
          which are on by default and grow with the input while nothing knows the
          endpoint's own limit unless Settings.screen_model_max_output_tokens says
          so; without the retry, turning the caps on would break calls that worked
          uncapped. extract() passes False: its cap is one the caller set on
          purpose as a runaway backstop, so a refusal surfaces rather than being
          silently traded for an uncapped call. A non-positive cap is never sent.
        - Usage is recorded whatever the outcome. A cap that cuts the JSON off
          mid-string makes the SDK's parse raise before a response object exists,
          so the request goes through `with_raw_response` and the billed tokens are
          read off the raw body instead of vanishing from usage_by_function.
        - A response with no parsed object (the Responses parser raises nothing
          when the model hit the cap before emitting any text -- unlike the
          chat-completions helper, it does not raise LengthFinishReasonError) is
          logged as a failure and raised, not returned as None.
        """
        t0 = time.monotonic()
        if cap is not None and cap <= 0:
            # e.g. a negative reasoning_output_tokens: the endpoint would refuse it
            # on every call, so it means "no cap" like the per-call knobs' <= 0.
            cap = None
        sent: int | Omit = omit if cap is None else cap
        try:
            try:
                raw = self._tiered(lambda tier: send(tier, sent))
            except (BadRequestError, UnprocessableEntityError) as e:
                if cap is None or not retry_uncapped or not _rejects_output_cap(e):
                    raise
                hint = (
                    ""
                    if self.settings.screen_model_max_output_tokens > 0
                    else " (set AWE_SCREEN_MODEL_MAX_OUTPUT_TOKENS to the model's "
                    "limit to keep one)"
                )
                logsink.emit(
                    f"    [llm {step}] endpoint refused max_output_tokens={cap}; "
                    f"retrying without a cap{hint}"
                )
                raw = self._tiered(lambda tier: send(tier, omit))
        except BaseException as e:
            self._log_call(step, model, in_chars, time.monotonic() - t0, None, e)
            raise
        try:
            response = raw.parse()
        except Exception as e:
            delta = self._accumulate(_billed(raw), model, function)
            self._log_call(step, model, in_chars, time.monotonic() - t0, delta, e)
            raise
        # Accumulated before the outcome is known: a truncated or refused response
        # is billed like any other, and the capped case is exactly the one a caller
        # re-rolls, making those tokens easy to lose track of.
        delta = self._accumulate(response, model, function)
        elapsed = time.monotonic() - t0
        parsed = response.output_parsed
        if parsed is None:
            reason = getattr(response.incomplete_details, "reason", None)
            detail = f"status={response.status}" + (
                f" reason={reason}" if reason else ""
            )
            error = AssertionError(f"{what} returned no parsed object ({detail})")
            self._log_call(step, model, in_chars, elapsed, delta, error)
            raise error
        self._log_call(step, model, in_chars, elapsed, delta)
        return parsed

    def screen(
        self,
        page_md: str,
        criterion: str,
        *,
        page_url: str | None = None,
        seed_url: str | None = None,
        on_seed_domain: bool | None = None,
    ) -> ScreenVerdict:
        truncated = page_md[:PAGE_TRUNC_CHARS]
        # The criterion lives in `instructions` (a stable prefix reused verbatim on
        # every screen call), not in the per-page `input`, so the provider's prompt
        # cache can serve it once instead of re-billing it per page. The domain block
        # + preference instruction are added only when the caller supplies a seed_url
        # (i.e. opted into the same-domain preference).
        instructions = f"{self.screen_prompt}\n\nCRITERION:\n{criterion}"
        domain_block = ""
        if seed_url is not None:
            instructions += SCREEN_DOMAIN_PREFERENCE
            domain_block = (
                f"SEED_URL: {seed_url}\n"
                f"PAGE_URL: {page_url or '(unknown)'}\n"
                f"ON_SEED_DOMAIN: {_yn(on_seed_domain)}\n\n"
            )
        payload = f"{domain_block}PAGE:\n{truncated}"
        # On by default: the answer is a bool and one sentence, so a runaway
        # generation is the only way to need more. See Settings.screen_output_tokens.
        answer = self.settings.screen_output_tokens
        cap = (
            self._screen_model_cap(self.settings.reasoning_output_tokens + answer)
            if answer > 0
            else None
        )
        parsed = self._structured_call(
            lambda tier, max_out: self._client.responses.with_raw_response.parse(
                model=self.model_screen,
                instructions=instructions,
                input=payload,
                text_format=_ScreenSchema,
                max_output_tokens=max_out,
                service_tier=tier,
            ),
            cap=cap,
            retry_uncapped=True,
            what="screening",
            step="screen",
            model=self.model_screen,
            function="screen",
            in_chars=len(payload),
        )
        return ScreenVerdict(match=parsed.match, reason=parsed.reason)

    def score_links(
        self,
        links: list[tuple[str, str]],
        page_md: str,
        criterion: str,
        *,
        seed_url: str | None = None,
        on_seed_domain: dict[str, bool | None] | None = None,
    ) -> list[tuple[str, float]]:
        if not links:
            return []
        page_excerpt = page_md[:4000]
        # The criterion lives in `instructions` (a stable, cache-friendly prefix),
        # not in the per-call `input`. Annotate each link with its on-domain signal
        # (and add the preference instruction) only when the caller opted in via
        # seed_url.
        annotate = seed_url is not None
        instructions = (
            f"{self.score_prompt}"
            f"{SCORE_DOMAIN_PREFERENCE if annotate else ''}"
            f"\n\nCRITERION:\n{criterion}"
        )
        sig = on_seed_domain or {}
        link_lines = []
        for anchor, url in links:
            if annotate:
                link_lines.append(
                    f"- {url}  (anchor: {anchor!r}, on_seed_domain: {_yn(sig.get(url))})"
                )
            else:
                link_lines.append(f"- {url}  (anchor: {anchor!r})")
        link_block = "\n".join(link_lines)
        seed_line = f"SEED_URL: {seed_url}\n\n" if annotate else ""
        payload = (
            f"{seed_line}"
            f"SOURCE PAGE EXCERPT:\n{page_excerpt}\n\n"
            f"LINKS TO SCORE (one per line):\n{link_block}"
        )
        # On by default, unlike extract()'s cap: the output is one url + score per
        # link, so its size is known before the call. The URLs are counted because
        # the scorer echoes each one exactly. A runaway generation then fails in a
        # minute or two instead of outlasting the read timeout and being re-sent by
        # the SDK. See Settings.score_output_tokens_per_link.
        per_link = self.settings.score_output_tokens_per_link
        cap = None
        if per_link > 0:
            urls = "\n".join(url for _, url in links)
            try:
                url_tokens = count_tokens(
                    urls, self.model_screen, self.settings.tiktoken_encoding
                )
            except Exception as e:  # noqa: BLE001 -- sizing must not cost the call
                # tiktoken fetches its table on first use; if it can't, size on
                # characters -- generous for URLs, which are almost all ASCII.
                logsink.emit(
                    f"    [llm score_links] token count unavailable "
                    f"({type(e).__name__}); sizing the cap on characters"
                )
                url_tokens = len(urls)
            cap = self._screen_model_cap(
                self.settings.reasoning_output_tokens
                + url_tokens
                + per_link * len(links)
            )
        parsed = self._structured_call(
            lambda tier, max_out: self._client.responses.with_raw_response.parse(
                model=self.model_screen,
                instructions=instructions,
                input=payload,
                text_format=_LinkScores,
                max_output_tokens=max_out,
                service_tier=tier,
            ),
            cap=cap,
            retry_uncapped=True,
            what="link scoring",
            step=f"score_links[{len(links)}]",
            model=self.model_screen,
            function="score_links",
            in_chars=len(payload),
        )
        url_set = {url for _, url in links}
        scored: dict[str, float] = {}
        for entry in parsed.scores:
            if entry.url in url_set:
                scored[entry.url] = max(0.0, min(1.0, entry.score))
        return [(url, scored.get(url, 0.0)) for _, url in links]

    def summarize(
        self,
        text: str,
        criterion: str,
        *,
        schema: type[BaseModel] | None = None,
        usage_tag: str = "summarize",
    ) -> str:
        """Condense `text` (criterion- and schema-aware) using the cheap screen model.

        The criterion and the schema outline go in `instructions` (a stable prefix
        that is byte-identical across every chunk and every reduce level, so the
        provider's prompt cache serves it instead of re-billing it per chunk); the
        text to compress goes in `input`. Callers pre-chunk `text` to fit the model's
        window (see summarize.py), so nothing is truncated here.

        `schema` is the schema the consolidated extraction will be parsed into. It is
        optional so `summarize` stays a generic provider utility, but the Extractor
        always passes it -- without it the summarizer only knows what is *relevant*,
        not what will be *asked for*.
        """
        instructions = f"{self.summarize_prompt}\n\nCRITERION:\n{criterion}"
        if schema is not None:
            instructions += SUMMARIZE_SCHEMA_GUIDANCE + schema_outline_safe(schema)
        payload = f"CONTENT:\n{text}"
        t0 = time.monotonic()
        try:
            response = self._tiered(
                lambda tier: self._client.responses.create(
                    model=self.model_screen,
                    instructions=instructions,
                    input=payload,
                    service_tier=tier,
                )
            )
        except BaseException as e:
            self._log_call(
                usage_tag,
                self.model_screen,
                len(payload),
                time.monotonic() - t0,
                None,
                e,
            )
            raise
        delta = self._accumulate(response, self.model_screen, usage_tag)
        self._log_call(
            usage_tag, self.model_screen, len(payload), time.monotonic() - t0, delta
        )
        return response.output_text or ""

    def extract(
        self, page_md: str, schema: type[BaseModel], *, usage_tag: str = "extract"
    ) -> BaseModel:
        payload = f"CONTENT:\n{page_md}"
        step = f"{usage_tag}[{schema.__name__}]"
        # Unset (0) sends no cap, so the wire is unchanged from before this knob
        # existed. When set, it bounds a degenerate generation -- see
        # Settings.max_output_tokens for why schema-guided decoding needs the
        # backstop and how to size it. (A non-positive cap is never sent.)
        return self._structured_call(
            lambda tier, max_out: self._client.responses.with_raw_response.parse(
                model=self.model_extract,
                instructions=self.extract_prompt,
                input=payload,
                text_format=schema,
                max_output_tokens=max_out,
                service_tier=tier,
            ),
            cap=self.settings.max_output_tokens,
            retry_uncapped=False,
            what="extraction",
            step=step,
            model=self.model_extract,
            function=usage_tag,
            in_chars=len(payload),
        )
