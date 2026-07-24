import threading
import time
from dataclasses import dataclass

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field

from .. import logsink
from ..config import Settings
from ..result import ScreenVerdict, Usage

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
    "detail relevant to the CRITERION -- names, dates, deadlines, amounts, eligibility,\n"
    "URLs, and any concrete fact a structured extraction might need. Drop boilerplate,\n"
    "navigation, and anything irrelevant to the criterion. Do not add information that\n"
    "is not present. Output only the condensed text."
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


def _yn(on_seed_domain: bool | None) -> str:
    """Render the on-domain signal for the prompt (None = host unparseable)."""
    return {True: "yes", False: "no", None: "unknown"}[on_seed_domain]


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
        # get more patience before it aborts a page. Overall/read timeout stays at
        # the SDK default (600s), ample for a large extraction call.
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.settings.openai_base_url,
            max_retries=5,
            timeout=httpx.Timeout(600.0, connect=30.0),
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

        Covers the three base instructions plus the two same-domain-preference
        appendices, so editing any of them (whether the module defaults or a
        per-instance override) busts the page cache. The domain appendices are
        static constants, but including them keeps the signature complete even if
        they change. Order is fixed so the string is deterministic.
        """
        parts = [
            self.screen_prompt,
            self.score_prompt,
            self.extract_prompt,
            self.summarize_prompt,
            SCREEN_DOMAIN_PREFERENCE,
            SCORE_DOMAIN_PREFERENCE,
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
        t0 = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model_screen,
                instructions=instructions,
                input=payload,
                text_format=_ScreenSchema,
            )
        except BaseException as e:
            self._log_call(
                "screen",
                self.model_screen,
                len(payload),
                time.monotonic() - t0,
                None,
                e,
            )
            raise
        delta = self._accumulate(response, self.model_screen, "screen")
        self._log_call(
            "screen", self.model_screen, len(payload), time.monotonic() - t0, delta
        )
        parsed = response.output_parsed
        assert parsed is not None
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
        t0 = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model_screen,
                instructions=instructions,
                input=payload,
                text_format=_LinkScores,
            )
        except BaseException as e:
            self._log_call(
                f"score_links[{len(links)}]",
                self.model_screen,
                len(payload),
                time.monotonic() - t0,
                None,
                e,
            )
            raise
        delta = self._accumulate(response, self.model_screen, "score_links")
        self._log_call(
            f"score_links[{len(links)}]",
            self.model_screen,
            len(payload),
            time.monotonic() - t0,
            delta,
        )
        parsed = response.output_parsed
        assert parsed is not None
        url_set = {url for _, url in links}
        scored: dict[str, float] = {}
        for entry in parsed.scores:
            if entry.url in url_set:
                scored[entry.url] = max(0.0, min(1.0, entry.score))
        return [(url, scored.get(url, 0.0)) for _, url in links]

    def summarize(
        self, text: str, criterion: str, *, usage_tag: str = "summarize"
    ) -> str:
        """Condense `text` (criterion-aware) using the cheap screen model.

        The criterion goes in `instructions` (stable, cacheable prefix); the text
        to compress goes in `input`. Callers pre-chunk `text` to fit the model's
        window (see summarize.py), so nothing is truncated here.
        """
        instructions = f"{self.summarize_prompt}\n\nCRITERION:\n{criterion}"
        payload = f"CONTENT:\n{text}"
        t0 = time.monotonic()
        try:
            response = self._client.responses.create(
                model=self.model_screen,
                instructions=instructions,
                input=payload,
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
        t0 = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model_extract,
                instructions=self.extract_prompt,
                input=payload,
                text_format=schema,
            )
        except BaseException as e:
            self._log_call(
                step, self.model_extract, len(payload), time.monotonic() - t0, None, e
            )
            raise
        delta = self._accumulate(response, self.model_extract, usage_tag)
        self._log_call(
            step, self.model_extract, len(payload), time.monotonic() - t0, delta
        )
        parsed = response.output_parsed
        assert parsed is not None
        return parsed
