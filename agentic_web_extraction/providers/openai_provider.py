import sys
import time
from dataclasses import dataclass

from openai import OpenAI
from pydantic import BaseModel, Field

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
    "Extract the requested fields from the PAGE. If a field is not present, leave it\n"
    "null where the schema permits, otherwise infer the most reasonable value from the\n"
    "page text. Do not fabricate."
)

PAGE_TRUNC_CHARS = 16000


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

    def __post_init__(self) -> None:
        api_key = (
            self.settings.openai_api_key.get_secret_value()
            if self.settings.openai_api_key is not None
            else None
        )
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.settings.openai_base_url,
        )
        # Token usage bucketed by an opaque call-purpose tag ("screen",
        # "score_links", "extract", or whatever a caller passes to extract()).
        # _function_model remembers which model each tag ran on, so cost can be
        # reconstructed without baking a tag->model map anywhere downstream.
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
    def usage_by_function(self) -> dict[str, Usage]:
        return dict(self._usage_by_function)

    @property
    def function_model(self) -> dict[str, str]:
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
        self._usage_by_function[function] = self._usage_by_function.get(function, Usage()) + delta
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
            cached = f"(cached {delta.cached_input_tokens})" if delta.cached_input_tokens else ""
            tok = f"tok_in={delta.input_tokens}{cached} tok_out={delta.output_tokens}"
        status = f"FAIL:{type(error).__name__}" if error is not None else "ok"
        print(
            f"    [llm {step}] model={model} in_chars={in_chars} "
            f"elapsed={elapsed:.2f}s {tok} {status}",
            file=sys.stderr,
            flush=True,
        )

    def screen(self, page_md: str, criterion: str) -> ScreenVerdict:
        truncated = page_md[:PAGE_TRUNC_CHARS]
        payload = f"CRITERION:\n{criterion}\n\nPAGE:\n{truncated}"
        t0 = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model_screen,
                instructions=self.screen_prompt,
                input=payload,
                text_format=_ScreenSchema,
            )
        except BaseException as e:
            self._log_call("screen", self.model_screen, len(payload), time.monotonic() - t0, None, e)
            raise
        delta = self._accumulate(response, self.model_screen, "screen")
        self._log_call("screen", self.model_screen, len(payload), time.monotonic() - t0, delta)
        parsed = response.output_parsed
        assert parsed is not None
        return ScreenVerdict(match=parsed.match, reason=parsed.reason)

    def score_links(
        self,
        links: list[tuple[str, str]],
        page_md: str,
        criterion: str,
    ) -> list[tuple[str, float]]:
        if not links:
            return []
        page_excerpt = page_md[:4000]
        link_block = "\n".join(
            f"- {url}  (anchor: {anchor!r})" for anchor, url in links
        )
        payload = (
            f"CRITERION:\n{criterion}\n\n"
            f"SOURCE PAGE EXCERPT:\n{page_excerpt}\n\n"
            f"LINKS TO SCORE (one per line):\n{link_block}"
        )
        t0 = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model_screen,
                instructions=self.score_prompt,
                input=payload,
                text_format=_LinkScores,
            )
        except BaseException as e:
            self._log_call(f"score_links[{len(links)}]", self.model_screen, len(payload), time.monotonic() - t0, None, e)
            raise
        delta = self._accumulate(response, self.model_screen, "score_links")
        self._log_call(f"score_links[{len(links)}]", self.model_screen, len(payload), time.monotonic() - t0, delta)
        parsed = response.output_parsed
        assert parsed is not None
        url_set = {url for _, url in links}
        scored: dict[str, float] = {}
        for entry in parsed.scores:
            if entry.url in url_set:
                scored[entry.url] = max(0.0, min(1.0, entry.score))
        return [(url, scored.get(url, 0.0)) for _, url in links]

    def extract(
        self, page_md: str, schema: type[BaseModel], *, usage_tag: str = "extract"
    ) -> BaseModel:
        payload = f"PAGE:\n{page_md}"
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
            self._log_call(step, self.model_extract, len(payload), time.monotonic() - t0, None, e)
            raise
        delta = self._accumulate(response, self.model_extract, usage_tag)
        self._log_call(step, self.model_extract, len(payload), time.monotonic() - t0, delta)
        parsed = response.output_parsed
        assert parsed is not None
        return parsed
