import inspect
import json
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel

from . import fetch as fetch_module
from . import logsink
from .cache import (
    MERGE_NAMESPACE,
    PAGE_NAMESPACE,
    CachedPage,
    KVCache,
    SqliteKVCache,
    content_hash,
    merge_cache_key,
    page_cache_version,
)
from .config import Settings, get_settings
from .frontier import Frontier, registrable_domain, same_registrable_domain
from .normalize import TextFilter, extract_links, to_markdown
from .providers import Provider, get_provider
from .result import ExtractionResult, PageVerdict, StoppedReason, Usage


class _DefaultCache:
    """Sentinel for the `cache` arg: caller didn't pass one, so build the default.

    Distinguishes "not supplied" (→ the on-by-default SQLite cache at AWE_LLM_CACHE)
    from an explicit `cache=None` (→ caching disabled) and from a caller-supplied
    `KVCache`.
    """


_DEFAULT_CACHE = _DefaultCache()


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
        # Python-computed on_seed_domain signal, with an instruction to disfavor
        # off-domain content -- the model applies its own judgment; nothing is
        # excluded. Off by default. Unlike the raw LLM score, this signal does feed
        # the model, so a crawl's seed domain is mixed into the page-cache key when
        # it's on (see extract()).
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
        # criterion, schema, models, or normalize flag auto-invalidates entries.
        self._cache_version = page_cache_version(
            criteria=self.criteria,
            schema_json=json.dumps(self.schema.model_json_schema(), sort_keys=True),
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

        # Registrable domain of the seed, used to compute the on_seed_domain signal
        # fed to the screen/score LLM calls when `prefer_seed_domain` is on. Empty
        # when the seed host is unparseable, which yields an "unknown" signal.
        seed_domain = registrable_domain(urlsplit(seed_url).netloc)

        path: list[str] = []
        pages_fetched = 0
        verdicts: list[PageVerdict] = []
        usage_by_function_at_start = self.provider.usage_by_function
        matches: list[tuple[str, BaseModel]] = []
        # Page cache key of each matched page, in match order. These feed the merge
        # cache key so the merge replays only when every contributing page is
        # unchanged (see cache.merge_cache_key).
        matched_keys: list[str] = []

        while pages_fetched < budget:
            popped = frontier.pop()
            if popped is None:
                break
            url, _score, _source = popped

            self._log(
                f"  [page {pages_fetched + 1}/{budget}] (score={_score:.2f}) {url}"
            )
            fetch_t0 = time.monotonic()
            try:
                page = fetch_module.fetch(url)
            except Exception as e:
                self._log(f"    ! fetch failed on {url}: {type(e).__name__}: {e}")
                frontier.mark_visited(url)
                continue
            self._log(
                f"    [fetch] kind={page.kind} elapsed={time.monotonic() - fetch_t0:.2f}s"
            )
            frontier.mark_visited(url)
            # A fetch can redirect, and distinct requested URLs (classically
            # `/foo` and `/foo/`) can resolve to the same final page. If this
            # run already processed the resolved URL, skip it so no page is
            # fetched-through-to-screen, counted, cached, or path-listed twice.
            if page.url != url and frontier.is_visited(page.url):
                self._log(f"    [dedup] {url} resolved to already-seen {page.url}")
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
                self._log(
                    f"    ! normalize failed on {page.url}: {type(e).__name__}: {e}"
                )
                continue

            # Content-addressed cache: if this exact page content was screened,
            # extracted, and scored on a prior run, replay those outcomes with no
            # LLM calls. The version stamp is mixed into the key so a criterion/
            # schema/model change misses. We still fetched (above) to get here, so
            # `pages_fetched` and the budget are unaffected.
            # When the same-domain preference is on, the screen verdict and link
            # scores depend on the seed's registrable domain (via on_seed_domain),
            # so it's mixed into the key. The default (off) path keeps the exact
            # key shape it had before, so existing cache entries still hit.
            if self.prefer_seed_domain:
                cache_key = (
                    f"{self._cache_version}:seeddom={seed_domain}:"
                    f"{content_hash(page_md)}:{page.url}"
                )
            else:
                cache_key = f"{self._cache_version}:{content_hash(page_md)}:{page.url}"
            cached_raw = (
                self.cache.get(PAGE_NAMESPACE, cache_key)
                if self.cache is not None
                else None
            )
            if cached_raw is not None:
                cached = CachedPage.from_json(cached_raw)
                self._log(f"    [cache] hit {page.url}")
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
                        self._log(
                            f"    ! cached extract invalid on {page.url}: "
                            f"{type(e).__name__}: {e}"
                        )
                    else:
                        matches.append((page.url, data))
                        matched_keys.append(cache_key)
                for link_url, score in cached.link_scores:
                    frontier.push(link_url, score=score, source=page.url)
                continue

            stage_error = (
                False  # don't cache a page whose LLM stages hit a transient error
            )
            # When the same-domain preference is on, hand the screen call the
            # seed/page URL and the Python-computed on-domain signal so it can
            # disfavor off-domain pages; otherwise call it exactly as before.
            screen_kwargs: dict = {}
            if self.prefer_seed_domain:
                screen_kwargs = {
                    "page_url": page.url,
                    "seed_url": seed_url,
                    "on_seed_domain": same_registrable_domain(page.url, seed_domain),
                }
            try:
                verdict = self.provider.screen(page_md, self.criteria, **screen_kwargs)
            except Exception as e:
                self._log(f"    ! screen failed on {page.url}: {type(e).__name__}: {e}")
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
                    self._log(
                        f"    ! extract failed on {page.url}: {type(e).__name__}: {e}"
                    )
                    stage_error = True
                else:
                    matches.append((page.url, data))
                    matched_keys.append(cache_key)
                    extracted_dump = data.model_dump(mode="json")
                    matched_here = True

            # Early-exit mode: the caller asked to stop at the first successful
            # match instead of spending the whole budget gathering every match.
            # Skip link scoring, frontier expansion, and caching for this page --
            # its cache record would be incomplete (no link scores) and could
            # mislead a later gather-all run that replays it.
            if matched_here and stop_first:
                self._log(f"    [stop] first match on {page.url}; stopping traversal")
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
                    # When the same-domain preference is on, annotate each link with
                    # its on-domain signal so the scorer can disfavor off-domain
                    # links; otherwise call it exactly as before.
                    score_kwargs: dict = {}
                    if self.prefer_seed_domain:
                        score_kwargs = {
                            "seed_url": seed_url,
                            "on_seed_domain": {
                                link: same_registrable_domain(link, seed_domain)
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
                        for link_url, score in scores:
                            frontier.push(link_url, score=score, source=page.url)
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
                data = self._merge_cached(merge, matches, matched_keys)
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

    def _merge_cached(
        self,
        merge: Callable[..., BaseModel],
        matches: list[tuple[str, BaseModel]],
        matched_keys: list[str],
    ) -> BaseModel:
        """Replay the merge from cache when every contributing page hit the cache.

        The merge cache key is derived from the contributing pages' page-cache keys
        (see cache.merge_cache_key), so it hits exactly when the same set of pages
        with the same content produced the same per-page extractions -- i.e. every
        source page was unchanged. On a hit we replay the stored merged object with
        no LLM `merge` call; on a miss we run the merge and store its result.
        """
        if self.cache is None or not matched_keys:
            return self._merge(merge, matches)

        merge_key = f"{self._cache_version}:{merge_cache_key(matched_keys)}"
        cached_raw = self.cache.get(MERGE_NAMESPACE, merge_key)
        if cached_raw is not None:
            try:
                data = self.schema.model_validate_json(cached_raw)
            except Exception as e:
                self._log(f"    ! cached merge invalid: {type(e).__name__}: {e}")
            else:
                self._log("    [cache] merge hit")
                return data

        data = self._merge(merge, matches)
        try:
            self.cache.put(MERGE_NAMESPACE, merge_key, data.model_dump_json())
        except Exception as e:  # noqa: BLE001 - caching is best-effort
            self._log(f"    ! merge cache put failed: {type(e).__name__}: {e}")
        return data

    def _merge(
        self,
        merge: Callable[..., BaseModel],
        matches: list[tuple[str, BaseModel]],
    ) -> BaseModel:
        """Call the schema's `merge_extractions`, passing only the optional kwargs
        it actually declares.

        Which of `provider` / `cache` a schema accepts is decided by *inspecting
        the signature*, not by calling and catching `TypeError`. Catching
        `TypeError` could not tell "this call has the wrong arity" apart from "a
        `TypeError` was raised inside a correctly-matched call", so it would
        silently re-run the merge (and any LLM calls it makes) up to two more
        times and then surface the wrong error. Probing the signature calls the
        merge exactly once.
        """
        available = {"provider": self.provider, "cache": self.cache}
        try:
            params = inspect.signature(merge).parameters
        except (TypeError, ValueError):
            # Uninspectable callable (rare); offer everything and let it choose.
            return merge(matches, **available)
        accepts_var_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs = {
            name: value
            for name, value in available.items()
            if accepts_var_kwargs or name in params
        }
        return merge(matches, **kwargs)

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
            protocol=self.provider.name,
            usage_by_function=usage_by_function,
            function_model=self.provider.function_model,
        )
