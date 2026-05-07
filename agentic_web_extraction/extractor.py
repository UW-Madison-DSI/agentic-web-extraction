from pydantic import BaseModel

from . import fetch as fetch_module
from .config import Settings, get_settings
from .frontier import Frontier
from .normalize import extract_links, to_markdown
from .providers import Provider, get_provider
from .result import ExtractionResult, ScreenVerdict, StoppedReason, Usage


class Extractor:
    def __init__(
        self,
        schema: type[BaseModel],
        criteria: str,
        *,
        provider: Provider | None = None,
        normalize_html: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.schema = schema
        self.criteria = criteria
        self.settings = settings or get_settings()
        self.provider = provider or get_provider(self.settings)
        self.normalize_html = (
            normalize_html if normalize_html is not None else self.settings.normalize
        )

    def extract(
        self,
        seed_url: str,
        max_fetches: int | None = None,
    ) -> ExtractionResult:
        budget = max_fetches if max_fetches is not None else self.settings.max_fetches
        frontier = Frontier()
        frontier.push(seed_url, score=1.0, source="seed")

        path: list[str] = []
        pages_fetched = 0
        last_verdict: ScreenVerdict | None = None
        usage_at_start = self.provider.usage

        while pages_fetched < budget:
            popped = frontier.pop()
            if popped is None:
                break
            url, _score, _source = popped

            page = fetch_module.fetch(url)
            pages_fetched += 1
            path.append(page.url)
            frontier.mark_visited(url)
            if page.url != url:
                frontier.mark_visited(page.url)

            if page.kind in ("skipped", "error"):
                continue

            page_md = (
                to_markdown(page.raw_bytes, page.content_type, url=page.url)
                if self.normalize_html or page.kind == "pdf"
                else page.text
            )

            verdict = self.provider.screen(page_md, self.criteria)
            last_verdict = verdict
            if verdict.match:
                data = self.provider.extract(page_md, self.schema)
                return self._result(
                    data=data,
                    stopped="match",
                    pages_fetched=pages_fetched,
                    path=path,
                    verdict=verdict,
                    usage_at_start=usage_at_start,
                )

            if page.kind == "html" and page.text:
                outgoing = extract_links(page.text, base_url=page.url)
                fresh = [(text, link) for text, link in outgoing if not frontier.is_visited(link)]
                if fresh:
                    scores = self.provider.score_links(fresh, page_md, self.criteria)
                    for link_url, score in scores:
                        frontier.push(link_url, score=score, source=page.url)

        return self._result(
            data=None,
            stopped="budget_exhausted",
            pages_fetched=pages_fetched,
            path=path,
            verdict=last_verdict,
            usage_at_start=usage_at_start,
        )

    def extract_batch(
        self,
        seed_urls: list[str],
        max_fetches: int | None = None,
    ) -> list[ExtractionResult]:
        return [self.extract(url, max_fetches=max_fetches) for url in seed_urls]

    def _result(
        self,
        *,
        data: BaseModel | None,
        stopped: StoppedReason,
        pages_fetched: int,
        path: list[str],
        verdict: ScreenVerdict | None,
        usage_at_start: Usage,
    ) -> ExtractionResult:
        delta = Usage(
            input_tokens=self.provider.usage.input_tokens - usage_at_start.input_tokens,
            output_tokens=self.provider.usage.output_tokens - usage_at_start.output_tokens,
            calls=self.provider.usage.calls - usage_at_start.calls,
        )
        return ExtractionResult(
            data=data,
            stopped_reason=stopped,
            pages_fetched=pages_fetched,
            path=path,
            verdict=verdict,
            provider=self.provider.name,
            model=self.provider.model_extract,
            usage=delta,
        )
