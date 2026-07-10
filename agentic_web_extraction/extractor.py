import json
from collections.abc import Sequence

from pydantic import BaseModel

from . import fetch as fetch_module
from .cache import (
    PAGE_NAMESPACE,
    CachedPage,
    KVCache,
    content_hash,
    page_cache_version,
)
from .config import Settings, get_settings
from .frontier import Frontier, registrable_domain, same_registrable_domain
from .normalize import TextFilter, extract_links, to_markdown
from .providers import Provider, get_provider
from .result import ExtractionResult, PageVerdict, StoppedReason, Usage


class Extractor:
    def __init__(
        self,
        schema: type[BaseModel],
        criteria: str,
        *,
        provider: Provider | None = None,
        normalize_html: bool | None = None,
        off_domain_weight: float | None = None,
        text_filters: Sequence[TextFilter] | None = None,
        settings: Settings | None = None,
        cache: KVCache | None = None,
    ) -> None:
        self.schema = schema
        self.criteria = criteria
        self.settings = settings or get_settings()
        self.provider = provider or get_provider(self.settings)
        self.normalize_html = (
            normalize_html if normalize_html is not None else self.settings.normalize
        )
        # Soft same-domain navigation preference (see Settings.off_domain_weight).
        # A single knob: 1.0 disables it, < 1.0 opts in. Caller-defined and applied
        # at frontier-push time, not baked into the page cache, so changing it does
        # not invalidate cached scores.
        self.off_domain_weight = (
            off_domain_weight
            if off_domain_weight is not None
            else self.settings.off_domain_weight
        )
        # Caller-supplied `str -> str` transforms applied to the normalized
        # markdown (e.g. to strip volatile per-response tokens so the content
        # hash stays stable). The library ships none -- it is site-agnostic; see
        # examples/strippers.py. Empty tuple means "leave the markdown as-is".
        self.text_filters: tuple[TextFilter, ...] = tuple(text_filters or ())
        self.cache = cache
        # Version stamp mixed into every page-cache key so a change to the
        # criterion, schema, models, or normalize flag auto-invalidates entries.
        self._cache_version = page_cache_version(
            criteria=self.criteria,
            schema_json=json.dumps(self.schema.model_json_schema(), sort_keys=True),
            model_screen=self.provider.model_screen,
            model_extract=self.provider.model_extract,
            normalize=self.normalize_html,
        )

    def extract(
        self,
        seed_url: str,
        max_fetches: int | None = None,
        *,
        stop_on_first_match: bool | None = None,
    ) -> ExtractionResult:
        budget = max_fetches if max_fetches is not None else self.settings.max_fetches
        stop_first = (
            stop_on_first_match
            if stop_on_first_match is not None
            else self.settings.stop_on_first_match
        )
        frontier = Frontier()
        frontier.push(seed_url, score=1.0, source="seed")

        # Registrable domain of the seed, used to softly down-weight off-domain
        # outgoing links when `off_domain_weight` < 1.0 (see _frontier_score).
        # Empty when the seed host is unparseable, which disables the re-weighting.
        from urllib.parse import urlsplit as _urlsplit

        seed_domain = registrable_domain(_urlsplit(seed_url).netloc)

        path: list[str] = []
        pages_fetched = 0
        verdicts: list[PageVerdict] = []
        usage_by_function_at_start = self.provider.usage_by_function
        matches: list[tuple[str, BaseModel]] = []

        import sys as _sys
        import time as _time

        while pages_fetched < budget:
            popped = frontier.pop()
            if popped is None:
                break
            url, _score, _source = popped

            print(
                f"  [page {pages_fetched + 1}/{budget}] (score={_score:.2f}) {url}",
                file=_sys.stderr,
                flush=True,
            )
            fetch_t0 = _time.monotonic()
            try:
                page = fetch_module.fetch(url)
            except Exception as e:
                print(
                    f"    ! fetch failed on {url}: {type(e).__name__}: {e}",
                    file=_sys.stderr,
                    flush=True,
                )
                frontier.mark_visited(url)
                continue
            print(
                f"    [fetch] kind={page.kind} elapsed={_time.monotonic() - fetch_t0:.2f}s",
                file=_sys.stderr,
                flush=True,
            )
            frontier.mark_visited(url)
            # A fetch can redirect, and distinct requested URLs (classically
            # `/foo` and `/foo/`) can resolve to the same final page. If this
            # run already processed the resolved URL, skip it so no page is
            # fetched-through-to-screen, counted, cached, or path-listed twice.
            if page.url != url and frontier.is_visited(page.url):
                print(
                    f"    [dedup] {url} resolved to already-seen {page.url}",
                    file=_sys.stderr,
                    flush=True,
                )
                continue
            frontier.mark_visited(page.url)
            path.append(page.url)

            # A page we couldn't actually read -- a fetch error, or a non-HTML/PDF
            # content type -- triggers no screen/extract/score LLM work, so it does
            # NOT consume a `max_fetches` slot. The budget caps the pages we spend
            # model calls on, not raw fetch attempts, so a run of dead links or
            # binary files can't starve the crawl of real pages. It's still
            # recorded in `path` (it was visited) and marked so it isn't retried.
            if page.kind in ("skipped", "error"):
                continue
            pages_fetched += 1

            try:
                page_md = (
                    to_markdown(
                        page.raw_bytes,
                        page.content_type,
                        url=page.url,
                        text_filters=self.text_filters,
                    )
                    if self.normalize_html or page.kind == "pdf"
                    else page.text
                )
            except Exception as e:
                print(
                    f"    ! normalize failed on {page.url}: {type(e).__name__}: {e}",
                    flush=True,
                )
                continue

            # Content-addressed cache: if this exact page content was screened,
            # extracted, and scored on a prior run, replay those outcomes with no
            # LLM calls. The version stamp is mixed into the key so a criterion/
            # schema/model change misses. We still fetched (above) to get here, so
            # `pages_fetched` and the budget are unaffected.
            cache_key = f"{self._cache_version}:{content_hash(page_md)}:{page.url}"
            cached_raw = (
                self.cache.get(PAGE_NAMESPACE, cache_key)
                if self.cache is not None
                else None
            )
            if cached_raw is not None:
                cached = CachedPage.from_json(cached_raw)
                print(f"    [cache] hit {page.url}", file=_sys.stderr, flush=True)
                verdicts.append(
                    PageVerdict(
                        url=page.url,
                        match=cached.screen_match,
                        reason=cached.screen_reason,
                    )
                )
                if cached.screen_match and cached.extracted is not None:
                    try:
                        data = self.schema.model_validate(cached.extracted)
                    except Exception as e:
                        print(
                            f"    ! cached extract invalid on {page.url}: {type(e).__name__}: {e}",
                            flush=True,
                        )
                    else:
                        matches.append((page.url, data))
                for link_url, score in cached.link_scores:
                    frontier.push(
                        link_url,
                        score=self._frontier_score(link_url, score, seed_domain),
                        source=page.url,
                    )
                continue

            stage_error = (
                False  # don't cache a page whose LLM stages hit a transient error
            )
            try:
                verdict = self.provider.screen(page_md, self.criteria)
            except Exception as e:
                print(
                    f"    ! screen failed on {page.url}: {type(e).__name__}: {e}",
                    flush=True,
                )
                continue
            verdicts.append(
                PageVerdict(url=page.url, match=verdict.match, reason=verdict.reason)
            )
            extracted_dump: dict | None = None
            matched_here = False
            if verdict.match:
                try:
                    data = self.provider.extract(page_md, self.schema)
                except Exception as e:
                    print(
                        f"    ! extract failed on {page.url}: {type(e).__name__}: {e}",
                        flush=True,
                    )
                    stage_error = True
                else:
                    matches.append((page.url, data))
                    extracted_dump = data.model_dump(mode="json")
                    matched_here = True

            # Early-exit mode: the caller asked to stop at the first successful
            # match instead of spending the whole budget gathering every match.
            # Skip link scoring, frontier expansion, and caching for this page --
            # its cache record would be incomplete (no link scores) and could
            # mislead a later gather-all run that replays it.
            if matched_here and stop_first:
                print(
                    f"    [stop] first match on {page.url}; stopping traversal",
                    file=_sys.stderr,
                    flush=True,
                )
                break

            link_scores: list[list] = []
            if page.kind == "html" and page.text:
                outgoing = extract_links(page.text, base_url=page.url)
                fresh = [
                    (text, link)
                    for text, link in outgoing
                    if not frontier.is_visited(link)
                ]
                if fresh:
                    try:
                        scores = self.provider.score_links(
                            fresh, page_md, self.criteria
                        )
                    except Exception as e:
                        print(
                            f"    ! score_links failed on {page.url}: {type(e).__name__}: {e}",
                            flush=True,
                        )
                        stage_error = True
                    else:
                        for link_url, score in scores:
                            frontier.push(
                                link_url,
                                score=self._frontier_score(
                                    link_url, score, seed_domain
                                ),
                                source=page.url,
                            )
                            # Cache the raw LLM score, not the domain-adjusted one,
                            # so the re-weighting stays a push-time policy the cache
                            # is oblivious to (toggling it doesn't invalidate entries).
                            link_scores.append([link_url, score])

            if self.cache is not None and not stage_error:
                self.cache.put(
                    PAGE_NAMESPACE,
                    cache_key,
                    CachedPage(
                        screen_match=verdict.match,
                        screen_reason=verdict.reason,
                        extracted=extracted_dump,
                        link_scores=link_scores,
                    ).to_json(),
                )

        if matches:
            merge = getattr(self.schema, "merge_extractions", None)
            if callable(merge):
                try:
                    data = merge(matches, provider=self.provider, cache=self.cache)
                except TypeError:
                    # Backwards-compat: schema's merge_extractions may accept
                    # neither cache nor provider. Degrade one kwarg at a time.
                    try:
                        data = merge(matches, provider=self.provider)
                    except TypeError:
                        data = merge(matches)
            else:
                data = matches[0][1]
            return self._result(
                data=data,
                stopped="match",
                pages_fetched=pages_fetched,
                path=path,
                verdicts=verdicts,
                usage_by_function_at_start=usage_by_function_at_start,
            )

        return self._result(
            data=None,
            stopped="budget_exhausted",
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            usage_by_function_at_start=usage_by_function_at_start,
        )

    def _frontier_score(self, link_url: str, score: float, seed_domain: str) -> float:
        """Apply the opt-in soft same-domain preference to a link's raw score.

        When `off_domain_weight` is < 1.0 and the link is on a *different*
        registrable domain than the seed, its score is multiplied by that weight
        — a nudge that lowers its frontier priority without excluding it, so a
        high-scoring off-domain page can still be visited. Links whose host is
        unparseable, or that share the seed's domain, are left untouched. A no-op
        (and skips the domain lookup entirely) when the weight is 1.0 or the seed
        domain is unknown, so the default behavior is pure LLM-score ordering.
        """
        if self.off_domain_weight == 1.0 or not seed_domain:
            return score
        if same_registrable_domain(link_url, seed_domain) is False:
            return score * self.off_domain_weight
        return score

    def extract_batch(
        self,
        seed_urls: list[str],
        max_fetches: int | None = None,
        *,
        stop_on_first_match: bool | None = None,
    ) -> list[ExtractionResult]:
        return [
            self.extract(
                url, max_fetches=max_fetches, stop_on_first_match=stop_on_first_match
            )
            for url in seed_urls
        ]

    def _result(
        self,
        *,
        data: BaseModel | None,
        stopped: StoppedReason,
        pages_fetched: int,
        path: list[str],
        verdicts: list[PageVerdict],
        usage_by_function_at_start: dict[str, Usage],
    ) -> ExtractionResult:
        usage_by_function: dict[str, Usage] = {}
        for func, end in self.provider.usage_by_function.items():
            start = usage_by_function_at_start.get(func, Usage())
            func_delta = Usage(
                input_tokens=end.input_tokens - start.input_tokens,
                output_tokens=end.output_tokens - start.output_tokens,
                calls=end.calls - start.calls,
                cached_input_tokens=end.cached_input_tokens - start.cached_input_tokens,
            )
            if func_delta.calls:
                usage_by_function[func] = func_delta
        return ExtractionResult(
            data=data,
            stopped_reason=stopped,
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            provider=self.provider.name,
            usage_by_function=usage_by_function,
            function_model=self.provider.function_model,
        )
