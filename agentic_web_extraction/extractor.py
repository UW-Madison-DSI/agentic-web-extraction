import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit

from pydantic import BaseModel

from . import fetch as fetch_module
from . import logsink
from .cache import (
    EXTRACT_NAMESPACE,
    PAGE_NAMESPACE,
    CachedPage,
    KVCache,
    SqliteKVCache,
    content_hash,
    extract_cache_key,
    page_cache_version,
)
from .config import Settings, get_settings
from .fetch import FetchedPage
from .frontier import Frontier, canonical, registrable_domain
from .normalize import TextFilter, extract_links, to_markdown
from .providers import Provider, get_provider
from .result import ExtractionResult, PageVerdict, StoppedReason, Usage
from .summarize import fit_pages

# Sentinel score for seed URLs: above the 0..1 range a link scorer can return, so
# every seed is popped (and fetched) before any discovered link, regardless of how
# many seeds there are.
SEED_SCORE = float("inf")


class _DefaultCache:
    """Sentinel for the `cache` arg: caller didn't pass one, so build the default.

    Distinguishes "not supplied" (→ the on-by-default SQLite cache at AWE_LLM_CACHE)
    from an explicit `cache=None` (→ caching disabled) and from a caller-supplied
    `KVCache`.
    """


_DEFAULT_CACHE = _DefaultCache()


@dataclass
class _PageOutcome:
    """What a worker thread computes for one popped URL, folded back on the main
    thread (which owns the frontier). Carries no frontier mutations."""

    requested_url: str
    page: FetchedPage
    readable: bool = False  # HTML/PDF body → consumes a budget slot
    has_verdict: bool = False  # a screen result (live or cached) was produced
    screen_match: bool = False
    screen_reason: str = ""
    page_md: str | None = None  # normalized content, kept only when matched
    cache_key: str | None = None
    link_scores: list[list] = field(default_factory=list)


class Extractor:
    def __init__(
        self,
        schema: type[BaseModel],
        criteria: str,
        *,
        provider: Provider | None = None,
        normalize_html: bool | None = None,
        prefer_seed_domain: bool | None = None,
        text_filters: Sequence[TextFilter] | None = None,
        settings: Settings | None = None,
        cache: KVCache | None | _DefaultCache = _DEFAULT_CACHE,
        log_file: str | None = None,
    ) -> None:
        self.schema = schema
        self.criteria = criteria
        self.settings = settings or get_settings()
        # Route every progress/diagnostic line through the shared sink: always to
        # stderr, and -- when a log file path is given -- appended with a
        # timestamp to that file. A single knob: empty path = no file logging.
        # `log_file=None` falls back to settings (AWE_LOG_FILE); pass "" to force
        # file logging off regardless of the environment.
        logsink.configure(
            log_file=log_file if log_file is not None else self.settings.log_file,
        )
        self.provider = provider or get_provider(self.settings)
        self.normalize_html = (
            normalize_html if normalize_html is not None else self.settings.normalize
        )
        # Soft same-domain preference (see Settings.prefer_seed_domain). When True,
        # the screen and link-scorer calls are told the seed/page URL and a
        # Python-computed on_seed_domain signal (generalized to "on ANY seed's
        # registrable domain" for multi-seed runs), with an instruction to disfavor
        # off-domain content -- the model applies its own judgment; nothing is
        # excluded. Off by default. This signal feeds the model, so the set of seed
        # domains is mixed into the page-cache key when it's on (see extract()).
        self.prefer_seed_domain = (
            prefer_seed_domain
            if prefer_seed_domain is not None
            else self.settings.prefer_seed_domain
        )
        # Caller-supplied `str -> str` transforms applied to the normalized
        # markdown (e.g. to strip volatile per-response tokens so the content
        # hash stays stable). The library ships none -- it is site-agnostic; see
        # examples/strippers.py. Empty tuple means "leave the markdown as-is".
        self.text_filters: tuple[TextFilter, ...] = tuple(text_filters or ())
        # Caching is on by default. When the caller doesn't pass `cache`, build the
        # default SQLite store at AWE_LLM_CACHE (empty setting = disabled); an
        # explicit `cache=None` disables it; a supplied KVCache is used as-is.
        self.cache: KVCache | None
        if isinstance(cache, _DefaultCache):
            self.cache = (
                SqliteKVCache(self.settings.llm_cache)
                if self.settings.llm_cache
                else None
            )
        else:
            self.cache = cache
        # Version stamp mixed into every page-cache key so a change to the
        # criterion, schema, prompt templates, models, or normalize flag auto-
        # invalidates entries. `prompt_signature` is optional on the Provider
        # protocol; a provider that omits it contributes an empty signature (and
        # so keeps its existing cache key shape).
        self._cache_version = page_cache_version(
            criteria=self.criteria,
            schema_json=json.dumps(self.schema.model_json_schema(), sort_keys=True),
            prompt_signature=getattr(self.provider, "prompt_signature", "") or "",
            model_screen=self.provider.model_screen,
            model_extract=self.provider.model_extract,
            normalize=self.normalize_html,
        )

    @staticmethod
    def _log(message: str) -> None:
        """Emit a progress/diagnostic line through the shared sink.

        Goes to stderr (never stdout, which carries the result JSON) and -- when
        file logging is on -- to a timestamped log file. See logsink.emit.
        """
        logsink.emit(message)

    def extract(
        self,
        seeds: str | Sequence[str],
        max_fetches: int | None = None,
        *,
        seed_is_content: bool | None = None,
    ) -> ExtractionResult:
        """Traverse from one or more seeds, then run a single consolidated extraction.

        Every seed is pushed into one shared frontier; the budget is
        ``max_fetches`` *per seed* (so ``max_fetches * len(seeds)`` total). The
        frontier is processed in parallel waves. The normalized markdown of every
        page that passes screening (across all seeds) is concatenated and, if it
        exceeds ``max_context_tokens``, summarized down; then one extraction runs
        over the whole thing. `seeds` accepts a single URL string or a sequence.
        """
        seed_list = [seeds] if isinstance(seeds, str) else list(seeds)
        if not seed_list:
            raise ValueError("extract requires at least one seed URL")
        per_seed = max_fetches if max_fetches is not None else self.settings.max_fetches
        budget = per_seed * len(seed_list)
        # Direct mode: the caller asserts each seed page *is* the content, so skip
        # pre-screening (guaranteed match) and link-scoring (no expansion). With no
        # links queued the frontier empties after the seeds.
        direct = (
            seed_is_content
            if seed_is_content is not None
            else self.settings.seed_is_content
        )

        # Registrable domains of all seeds, for the on_seed_domain signal fed to the
        # screen/score calls when prefer_seed_domain is on. A page/link is "on
        # domain" if it shares ANY seed's registrable domain. `seed_ref` is the
        # human-readable SEED context shown to the model.
        seed_domains = frozenset(
            d for d in (registrable_domain(urlsplit(u).netloc) for u in seed_list) if d
        )
        seed_ref = " ".join(seed_list)

        frontier = Frontier()
        for seed in seed_list:
            frontier.push(seed, score=SEED_SCORE, source="seed")

        self._log(
            f"[traverse] {len(seed_list)} seed(s), budget={budget} "
            f"({per_seed}/seed), workers={self.settings.max_workers}"
        )

        path: list[str] = []
        pages_fetched = 0
        verdicts: list[PageVerdict] = []
        usage_by_function_at_start = self.provider.usage_by_function
        # Screened-in pages feeding the one extraction: (resolved_url, markdown,
        # page_cache_key). Deduped on resolved URL via `resolved_seen`.
        matched_pages: list[tuple[str, str, str]] = []
        # Canonical resolved URLs already folded, so two requests that redirect to
        # the same target (or a later pop of an already-resolved URL) count once.
        resolved_seen: set[str] = set()

        worker = partial(
            self._process_page,
            seed_ref=seed_ref,
            seed_domains=seed_domains,
            direct=direct,
        )

        with ThreadPoolExecutor(max_workers=max(1, self.settings.max_workers)) as pool:
            while pages_fetched < budget:
                batch = self._pop_batch(
                    frontier, min(self.settings.max_workers, budget - pages_fetched)
                )
                if not batch:
                    break
                # Reserve every popped URL against re-popping, then snapshot the
                # known set for workers to pre-filter links against.
                for url, _score, _source in batch:
                    frontier.mark_visited(url)
                known = frontier.snapshot()
                outcomes = pool.map(partial(worker, known=known), batch)

                for outcome in outcomes:
                    page = outcome.page
                    rurl = canonical(page.url)
                    if rurl in resolved_seen:
                        self._log(
                            f"    [dedup] {outcome.requested_url} → already-seen "
                            f"{page.url}"
                        )
                        continue
                    resolved_seen.add(rurl)
                    frontier.mark_visited(page.url)
                    path.append(page.url)
                    if not outcome.readable:
                        continue
                    pages_fetched += 1
                    if outcome.has_verdict:
                        verdicts.append(
                            PageVerdict(
                                url=page.url,
                                match=outcome.screen_match,
                                reason=outcome.screen_reason,
                            )
                        )
                    if outcome.screen_match and outcome.page_md is not None:
                        matched_pages.append(
                            (page.url, outcome.page_md, outcome.cache_key or "")
                        )
                    for link_url, score in outcome.link_scores:
                        frontier.push(link_url, score=score, source=page.url)

        return self._consolidate_and_extract(
            matched_pages=matched_pages,
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            usage_by_function_at_start=usage_by_function_at_start,
        )

    @staticmethod
    def _pop_batch(frontier: Frontier, n: int) -> list[tuple[str, float, str]]:
        """Pop up to `n` top-scored links (main thread; frontier isn't concurrent)."""
        batch: list[tuple[str, float, str]] = []
        for _ in range(max(0, n)):
            popped = frontier.pop()
            if popped is None:
                break
            batch.append(popped)
        return batch

    def _on_any_seed_domain(
        self, url: str, seed_domains: frozenset[str]
    ) -> bool | None:
        """True if `url`'s host shares any seed's registrable domain, False if not,
        None if the host is missing/unparseable or no seed domain is known."""
        host = urlsplit(url).netloc if url else ""
        dom = registrable_domain(host)
        if not dom or not seed_domains:
            return None
        return dom in seed_domains

    def _process_page(
        self,
        item: tuple[str, float, str],
        *,
        seed_ref: str,
        seed_domains: frozenset[str],
        direct: bool,
        known: frozenset[str],
    ) -> _PageOutcome:
        """Worker: fetch → normalize → (cache | screen + score_links) for one URL.

        Runs on a pool thread. Does NO frontier mutation — it returns a
        `_PageOutcome` the main thread folds back. Extraction is not done here; the
        one consolidated extraction happens after the whole traversal.
        """
        url, score, _source = item
        score_str = "seed" if score == SEED_SCORE else f"{score:.2f}"
        self._log(f"  [page] (score={score_str}) {url}")
        fetch_t0 = time.monotonic()
        try:
            page = fetch_module.fetch(url)
        except Exception as e:
            self._log(f"    ! fetch failed on {url}: {type(e).__name__}: {e}")
            return _PageOutcome(
                requested_url=url,
                page=FetchedPage(
                    url=url,
                    status=0,
                    content_type="",
                    raw_bytes=b"",
                    text="",
                    kind="error",
                ),
            )
        self._log(
            f"    [fetch] kind={page.kind} elapsed={time.monotonic() - fetch_t0:.2f}s"
        )
        # A page we couldn't read (fetch error / non-HTML-PDF) does no LLM work and
        # consumes no budget slot. The main thread still records it in `path`.
        if page.kind in ("skipped", "error"):
            return _PageOutcome(requested_url=url, page=page)

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
            self._log(f"    ! normalize failed on {page.url}: {type(e).__name__}: {e}")
            return _PageOutcome(requested_url=url, page=page, readable=True)

        # Content-addressed cache: replay a prior run's screen verdict + link scores
        # for this exact page content with no LLM calls. Key segments are optional so
        # the default (both off) path keeps a stable, crawl-independent shape:
        #  - seeddom=<sorted seed domains> when prefer_seed_domain is on (the verdict
        #    and scores depend on the on_seed_domain signal, which depends on the
        #    seed set);
        #  - :direct when seed_is_content is on (screen is forced to a match and no
        #    links are scored, so it must not collide with a screened entry).
        key_prefix = self._cache_version
        if self.prefer_seed_domain:
            key_prefix = f"{key_prefix}:seeddom={','.join(sorted(seed_domains))}"
        if direct:
            key_prefix = f"{key_prefix}:direct"
        cache_key = f"{key_prefix}:{content_hash(page_md)}:{page.url}"
        cached_raw = (
            self.cache.get(PAGE_NAMESPACE, cache_key)
            if self.cache is not None
            else None
        )
        if cached_raw is not None:
            cached = CachedPage.from_json(cached_raw)
            self._log(f"    [cache] hit {page.url}")
            return _PageOutcome(
                requested_url=url,
                page=page,
                readable=True,
                has_verdict=True,
                screen_match=cached.screen_match,
                screen_reason=cached.screen_reason,
                page_md=page_md if cached.screen_match else None,
                cache_key=cache_key,
                link_scores=list(cached.link_scores),
            )

        stage_error = False  # don't cache a page whose LLM stages hit a transient error
        if direct:
            screen_match = True
            screen_reason = (
                "seed_is_content: screening skipped, page treated as a match"
            )
        else:
            screen_kwargs: dict = {}
            if self.prefer_seed_domain:
                screen_kwargs = {
                    "page_url": page.url,
                    "seed_url": seed_ref,
                    "on_seed_domain": self._on_any_seed_domain(page.url, seed_domains),
                }
            try:
                verdict = self.provider.screen(page_md, self.criteria, **screen_kwargs)
            except Exception as e:
                self._log(f"    ! screen failed on {page.url}: {type(e).__name__}: {e}")
                # Readable (counts budget) but no verdict and nothing to cache.
                return _PageOutcome(requested_url=url, page=page, readable=True)
            screen_match = verdict.match
            screen_reason = verdict.reason

        link_scores: list[list] = []
        # Direct mode queues no links. Otherwise score the outgoing links this page
        # introduces (filtered against the wave's snapshot of known URLs).
        if page.kind == "html" and page.text and not direct:
            outgoing = extract_links(page.text, base_url=page.url)
            fresh = [
                (text, link) for text, link in outgoing if canonical(link) not in known
            ]
            if fresh:
                score_kwargs: dict = {}
                if self.prefer_seed_domain:
                    score_kwargs = {
                        "seed_url": seed_ref,
                        "on_seed_domain": {
                            link: self._on_any_seed_domain(link, seed_domains)
                            for _, link in fresh
                        },
                    }
                try:
                    scores = self.provider.score_links(
                        fresh, page_md, self.criteria, **score_kwargs
                    )
                except Exception as e:
                    self._log(
                        f"    ! score_links failed on {page.url}: "
                        f"{type(e).__name__}: {e}"
                    )
                    stage_error = True
                else:
                    link_scores = [[link_url, score] for link_url, score in scores]

        if self.cache is not None and not stage_error:
            self.cache.put(
                PAGE_NAMESPACE,
                cache_key,
                CachedPage(
                    screen_match=screen_match,
                    screen_reason=screen_reason,
                    link_scores=link_scores,
                ).to_json(),
            )

        return _PageOutcome(
            requested_url=url,
            page=page,
            readable=True,
            has_verdict=True,
            screen_match=screen_match,
            screen_reason=screen_reason,
            page_md=page_md if screen_match else None,
            cache_key=cache_key,
            link_scores=link_scores,
        )

    def _consolidate_and_extract(
        self,
        *,
        matched_pages: list[tuple[str, str, str]],
        pages_fetched: int,
        path: list[str],
        verdicts: list[PageVerdict],
        usage_by_function_at_start: dict[str, Usage],
    ) -> ExtractionResult:
        """Concatenate every screened-in page, fit it to the context budget, and run
        the single extraction (cached on the set of contributing pages)."""
        if not matched_pages:
            return self._result(
                data=None,
                stopped="budget_exhausted",
                pages_fetched=pages_fetched,
                path=path,
                verdicts=verdicts,
                usage_by_function_at_start=usage_by_function_at_start,
            )

        # Sort by canonical URL so the concatenation and cache key are deterministic
        # regardless of the (parallel, nondeterministic) order pages were gathered.
        matched_pages.sort(key=lambda t: canonical(t[0]))
        pages_for_fit = [(url, md) for url, md, _ck in matched_pages]
        page_keys = [ck for _u, _m, ck in matched_pages]
        extract_key = (
            f"{self._cache_version}:ctx={self.settings.max_context_tokens}"
            f":enc={self.settings.tiktoken_encoding}:{extract_cache_key(page_keys)}"
        )

        # Extract-cache replay: hits only when the exact same set of pages (same
        # content) recurs. The stored value wraps the object plus the context-size
        # metadata, so a hit replays the full result with zero LLM calls.
        if self.cache is not None:
            cached_raw = self.cache.get(EXTRACT_NAMESPACE, extract_key)
            if cached_raw is not None:
                try:
                    wrapper = json.loads(cached_raw)
                    data = self.schema.model_validate(wrapper["data"])
                except Exception as e:  # noqa: BLE001 - fall through to a live run
                    self._log(
                        f"    ! cached extraction invalid: {type(e).__name__}: {e}"
                    )
                else:
                    self._log("    [cache] extraction hit")
                    return self._result(
                        data=data,
                        stopped="match",
                        pages_fetched=pages_fetched,
                        path=path,
                        verdicts=verdicts,
                        usage_by_function_at_start=usage_by_function_at_start,
                        content_tokens=int(wrapper.get("content_tokens", 0)),
                        extraction_input_tokens=int(
                            wrapper.get("extraction_input_tokens", 0)
                        ),
                        summarized=bool(wrapper.get("summarized", False)),
                    )

        final_text, summarized, content_tokens, extraction_input_tokens = fit_pages(
            pages_for_fit,
            criterion=self.criteria,
            schema=self.schema,
            provider=self.provider,
            max_context_tokens=self.settings.max_context_tokens,
            model=self.provider.model_extract,
            encoding_name=self.settings.tiktoken_encoding,
            cache=self.cache,
            version=self._cache_version,
            log=self._log,
        )

        self._log(
            f"    [extract] consolidating {len(matched_pages)} page(s), "
            f"{extraction_input_tokens} input tokens"
        )
        try:
            data = self.provider.extract(final_text, self.schema)
        except Exception as e:
            self._log(f"    ! extraction failed: {type(e).__name__}: {e}")
            return self._result(
                data=None,
                stopped="budget_exhausted",
                pages_fetched=pages_fetched,
                path=path,
                verdicts=verdicts,
                usage_by_function_at_start=usage_by_function_at_start,
                content_tokens=content_tokens,
                extraction_input_tokens=extraction_input_tokens,
                summarized=summarized,
            )

        if self.cache is not None:
            try:
                self.cache.put(
                    EXTRACT_NAMESPACE,
                    extract_key,
                    json.dumps(
                        {
                            "data": data.model_dump(mode="json"),
                            "content_tokens": content_tokens,
                            "extraction_input_tokens": extraction_input_tokens,
                            "summarized": summarized,
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception as e:  # noqa: BLE001 - caching is best-effort
                self._log(f"    ! extraction cache put failed: {type(e).__name__}: {e}")

        return self._result(
            data=data,
            stopped="match",
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            usage_by_function_at_start=usage_by_function_at_start,
            content_tokens=content_tokens,
            extraction_input_tokens=extraction_input_tokens,
            summarized=summarized,
        )

    def _result(
        self,
        *,
        data: BaseModel | None,
        stopped: StoppedReason,
        pages_fetched: int,
        path: list[str],
        verdicts: list[PageVerdict],
        usage_by_function_at_start: dict[str, Usage],
        content_tokens: int = 0,
        extraction_input_tokens: int = 0,
        summarized: bool = False,
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
            protocol=self.provider.name,
            usage_by_function=usage_by_function,
            function_model=self.provider.function_model,
            content_tokens=content_tokens,
            extraction_input_tokens=extraction_input_tokens,
            summarized=summarized,
        )
