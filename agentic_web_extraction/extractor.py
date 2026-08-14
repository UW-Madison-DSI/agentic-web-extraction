import json
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit

from pydantic import BaseModel

from . import fallback as fallback_module
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
from .frontier import (
    Frontier,
    canonical,
    domain_of,
    normalize_domains,
    registrable_domain,
    split_domains,
)
from .normalize import TextFilter, extract_links, to_markdown
from .providers import Provider, get_provider
from .result import ExtractionResult, PageVerdict, StoppedReason, Usage
from .robots import RobotsPolicy
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
    policy_skipped: bool = False  # refused before the fetch (robots.txt): never visited
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
        allowed_domains: Iterable[str] | None = None,
        allow_seed_redirect_domains: bool = True,
        user_agent: str | None = None,
        respect_robots: bool | None = None,
        robots_overrides: str | None = None,
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
        # Hard crawl boundary: the set of registrable domains a link may be queued
        # from. `None` (the default) is unrestricted -- every link the scorer likes
        # is fair game, wherever it points. Passing a set makes it default-deny:
        # anything off it is dropped at the queue point with a [blocked] log line.
        # Each seed's own domain is added per call (see extract), so an empty
        # iterable means "the seeds and nowhere else".
        #
        # This is the *only* hard navigation limit in the library. `prefer_seed_domain`
        # is a soft hint to the LLM that excludes nothing, which is not something a
        # deployment can point at when asked to guarantee where its crawler goes.
        self.allowed_domains = (
            None if allowed_domains is None else normalize_domains(allowed_domains)
        )
        # When a seed redirects to a different registrable domain (a rebrand, an
        # org that moved host), add where it landed to the boundary for this call.
        # Without it, restricting to the domain you seeded would break the crawl the
        # moment the site moves -- the seed itself resolves fine (httpx follows
        # redirects inside one fetch; the boundary governs queuing, not requests),
        # but every link on the page it lands on is off-boundary.
        self.allow_seed_redirect_domains = allow_seed_redirect_domains
        # Identify the crawler to the sites it fetches. Module-level configuration
        # (the fetch/fallback clients are process-wide singletons), following the
        # logsink.configure precedent -- so with several Extractors in one process
        # the last one constructed sets the User-Agent for all of them.
        self.user_agent = (
            user_agent if user_agent is not None else self.settings.user_agent
        ) or fetch_module.USER_AGENT
        fetch_module.configure(user_agent=self.user_agent)
        fallback_module.configure(user_agent=self.user_agent)
        # robots.txt, off by default (see Settings.respect_robots). Evaluated against
        # the User-Agent above, per origin, before the fetch; failures fail open.
        self.respect_robots = (
            respect_robots
            if respect_robots is not None
            else self.settings.respect_robots
        )
        overrides = (
            robots_overrides
            if robots_overrides is not None
            else self.settings.robots_overrides
        )
        self.robots: RobotsPolicy | None = (
            RobotsPolicy(
                user_agent=self.user_agent,
                overrides=split_domains(overrides),
            )
            if self.respect_robots
            else None
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

    def _cache_get(self, namespace: str, key: str) -> str | None:
        """Read from the cache, treating any store failure as a miss.

        Caching is an optimization, never a correctness input: a locked/corrupt
        SQLite file (or a caller-supplied `KVCache` that throws) must not abort a
        crawl -- and `_process_page` runs on pool threads, where an escaping
        exception would surface out of `pool.map` and discard the whole run.
        """
        if self.cache is None:
            return None
        try:
            return self.cache.get(namespace, key)
        except Exception as e:  # noqa: BLE001 - caching is best-effort
            self._log(f"    ! cache get failed ({namespace}): {type(e).__name__}: {e}")
            return None

    def _cache_put(self, namespace: str, key: str, value: str) -> None:
        """Write to the cache, swallowing any store failure (see `_cache_get`)."""
        if self.cache is None:
            return
        try:
            self.cache.put(namespace, key, value)
        except Exception as e:  # noqa: BLE001 - caching is best-effort
            self._log(f"    ! cache put failed ({namespace}): {type(e).__name__}: {e}")

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
        exceeds ``max_context_tokens`` (or always, under ``always_summarize``),
        summarized down; then one extraction runs over the whole thing. `seeds`
        accepts a single URL string or a sequence.

        When the Extractor was given ``allowed_domains``, the boundary for this
        call is that set plus every seed's own registrable domain (plus, under
        ``allow_seed_redirect_domains``, wherever a seed redirects to): a scored
        link outside it is dropped instead of queued, and logged as ``[blocked]``.
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

        # Crawl boundary for this call: the configured allowed domains plus every
        # seed's own domain -- a caller that hands over a seed URL is asking for
        # that domain by definition, so callers never have to repeat it. `None`
        # keeps the pre-0.3 unrestricted behavior. Mutable because a seed that
        # redirects off-domain may widen it (see the fold loop below); it is only
        # ever read/written on the main thread.
        allowed: set[str] | None = None
        if self.allowed_domains is not None:
            allowed = set(self.allowed_domains) | set(normalize_domains(seed_list))
            self._log(
                f"[boundary] allowed domains: {', '.join(sorted(allowed)) or '(none)'}"
            )
        # Canonical seed URLs, so the redirect rule above applies to seeds only.
        seed_keys = {canonical(u) for u in seed_list}

        frontier = Frontier()
        for seed in seed_list:
            frontier.push(seed, score=SEED_SCORE, source="seed")

        # Clamped once and used for BOTH the pool size and the wave's pop count: a
        # non-positive setting must not silently turn the crawl into a no-op (a 0
        # pop count pops nothing, breaks out of the loop, and returns an empty
        # result as though the frontier had run dry).
        workers = max(1, self.settings.max_workers)

        self._log(
            f"[traverse] {len(seed_list)} seed(s), budget={budget} "
            f"({per_seed}/seed), workers={workers}, "
            f"boundary={'off' if allowed is None else 'on'}, "
            f"robots={'on' if self.robots is not None else 'off'}, "
            f"ua={self.user_agent!r}"
        )

        path: list[str] = []
        pages_fetched = 0
        verdicts: list[PageVerdict] = []
        # Pages the origin refused that a fallback route recovered (see fetch.py /
        # fallback.py), url -> route. Empty on a crawl that got everything first-hand.
        fallbacks_used: dict[str, str] = {}
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

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while pages_fetched < budget:
                batch = self._pop_batch(frontier, min(workers, budget - pages_fetched))
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
                    if outcome.policy_skipped:
                        # Refused before any request went out, so it is not a page
                        # the crawl visited: keep it out of `path`, which records
                        # what was actually retrieved. It stays marked visited (done
                        # above) so it is not re-popped, and the worker's log line is
                        # the durable record of why it was left alone.
                        continue
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
                    if page.via:
                        fallbacks_used[page.url] = page.via
                    if not outcome.readable:
                        continue
                    pages_fetched += 1
                    # Widen the boundary to where a seed landed -- but only for a seed
                    # that came back with readable content. A parked or lapsed domain
                    # answers a redirect chain with an error or a non-page body, and
                    # letting *that* into the boundary would hand the set to whoever
                    # now owns the name: the exact failure the boundary exists to stop.
                    if (
                        allowed is not None
                        and self.allow_seed_redirect_domains
                        and canonical(outcome.requested_url) in seed_keys
                    ):
                        landed = domain_of(page.url)
                        if landed and landed not in allowed:
                            allowed.add(landed)
                            self._log(
                                f"    [boundary] seed {outcome.requested_url} resolved "
                                f"to {landed} — added to the allowed domains"
                            )
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
                        # The boundary is enforced HERE, at the queue point, and
                        # nowhere else. Filtering in the worker instead would bake
                        # the current allowed set into the page cache's stored link
                        # scores, so a later run with a different boundary would
                        # replay the old one; filtering the HTTP request instead
                        # would break redirects, which httpx follows inside a single
                        # fetch. A link that is never queued is never fetched, which
                        # is the whole guarantee.
                        if allowed is not None and domain_of(link_url) not in allowed:
                            self._log(
                                f"    [blocked] {link_url} — outside the crawl boundary"
                            )
                            continue
                        frontier.push(link_url, score=score, source=page.url)

        return self._consolidate_and_extract(
            matched_pages=matched_pages,
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            fallbacks_used=fallbacks_used,
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

    @staticmethod
    def _policy_skip(url: str) -> _PageOutcome:
        """Outcome for a URL policy refused: no content, no budget slot, no `path`
        entry. Carries a placeholder page so the fold loop's shape is unchanged."""
        return _PageOutcome(
            requested_url=url,
            page=FetchedPage(
                url=url,
                status=0,
                content_type="",
                raw_bytes=b"",
                text="",
                kind="skipped",
            ),
            policy_skipped=True,
        )

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
        # robots.txt (opt-in) is checked before the request, so a disallowed URL
        # costs no fetch, no budget slot and no LLM call. Safe from a worker thread:
        # the policy owns its cache and lock, and touches no traversal state.
        if self.robots is not None and not self.robots.allows(url):
            self._log(
                f"    [robots] {url} disallowed for {self.user_agent!r} — skipping"
            )
            return self._policy_skip(url)
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

        # The fetch follows redirects internally, so the URL checked above is not
        # necessarily the one that answered. Re-check where it landed: the request is
        # already spent (httpx followed it inside a single call, and refusing to
        # follow redirects would break the rebrand case the boundary depends on), but
        # a body from a path the origin disallows must not be read, screened, or
        # pooled into the extraction.
        if self.robots is not None and canonical(page.url) != canonical(url):
            if not self.robots.allows(page.url):
                self._log(
                    f"    [robots] {url} redirected to {page.url}, disallowed for "
                    f"{self.user_agent!r} — discarding unread"
                )
                return self._policy_skip(url)

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
        cached_raw = self._cache_get(PAGE_NAMESPACE, cache_key)
        if cached_raw is not None:
            try:
                cached = CachedPage.from_json(cached_raw)
            except Exception as e:  # noqa: BLE001 - fall through to a live run
                self._log(f"    ! cached page invalid: {type(e).__name__}: {e}")
            else:
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
            try:
                fresh = [
                    (text, link)
                    for text, link in extract_links(page.text, base_url=page.url)
                    if canonical(link) not in known
                ]
            except Exception as e:
                # A single malformed href must not cost the whole crawl. `urlsplit`
                # raises ValueError on a bracketed-host URL (`http://a[b]c.com/`),
                # and both extract_links and the `canonical` filter go through it --
                # unguarded, that escapes the worker, surfaces out of pool.map in the
                # fold loop, and discards every page already collected. Losing one
                # page's links is the cheap failure; losing the crawl is not.
                self._log(
                    f"    ! link extraction failed on {page.url}: "
                    f"{type(e).__name__}: {e}"
                )
                fresh = []
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

        if not stage_error:
            self._cache_put(
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
        fallbacks_used: dict[str, str],
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
                fallbacks_used=fallbacks_used,
                usage_by_function_at_start=usage_by_function_at_start,
            )

        # Sort by canonical URL so the concatenation and cache key are deterministic
        # regardless of the (parallel, nondeterministic) order pages were gathered.
        matched_pages.sort(key=lambda t: canonical(t[0]))
        pages_for_fit = [(url, md) for url, md, _ck in matched_pages]
        page_keys = [ck for _u, _m, ck in matched_pages]
        # `always` joins ctx/enc in the key: like them it decides what text reaches
        # the extraction call, so a run with it flipped must not replay the other's
        # object. (Per-chunk SUMMARY entries are keyed on content alone and stay
        # shared between always-on and overflow-triggered runs.)
        extract_key = (
            f"{self._cache_version}:ctx={self.settings.max_context_tokens}"
            f":always={'1' if self.settings.always_summarize else '0'}"
            f":enc={self.settings.tiktoken_encoding}:{extract_cache_key(page_keys)}"
        )

        # Extract-cache replay: hits only when the exact same set of pages (same
        # content) recurs. The stored value wraps the object plus the context-size
        # metadata, so a hit replays the full result with zero LLM calls.
        cached_raw = self._cache_get(EXTRACT_NAMESPACE, extract_key)
        if cached_raw is not None:
            try:
                wrapper = json.loads(cached_raw)
                data = self.schema.model_validate(wrapper["data"])
            except Exception as e:  # noqa: BLE001 - fall through to a live run
                self._log(f"    ! cached extraction invalid: {type(e).__name__}: {e}")
            else:
                self._log("    [cache] extraction hit")
                return self._result(
                    data=data,
                    stopped="match",
                    pages_fetched=pages_fetched,
                    path=path,
                    verdicts=verdicts,
                    fallbacks_used=fallbacks_used,
                    usage_by_function_at_start=usage_by_function_at_start,
                    content_tokens=int(wrapper.get("content_tokens", 0)),
                    extraction_input_tokens=int(
                        wrapper.get("extraction_input_tokens", 0)
                    ),
                    summarized=bool(wrapper.get("summarized", False)),
                )

        try:
            final_text, summarized, content_tokens, extraction_input_tokens = fit_pages(
                pages_for_fit,
                criterion=self.criteria,
                schema=self.schema,
                provider=self.provider,
                max_context_tokens=self.settings.max_context_tokens,
                always=self.settings.always_summarize,
                model=self.provider.model_extract,
                encoding_name=self.settings.tiktoken_encoding,
                cache=self.cache,
                version=self._cache_version,
                log=self._log,
            )
        except Exception as e:
            # A summarize call that fails (or a chunk the screen model refuses) must
            # degrade to the uniform result like any other stage, not raise out of
            # `extract` and throw away the whole traversal.
            self._log(f"    ! fitting content failed: {type(e).__name__}: {e}")
            return self._result(
                data=None,
                stopped="budget_exhausted",
                pages_fetched=pages_fetched,
                path=path,
                verdicts=verdicts,
                fallbacks_used=fallbacks_used,
                usage_by_function_at_start=usage_by_function_at_start,
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
                fallbacks_used=fallbacks_used,
                usage_by_function_at_start=usage_by_function_at_start,
                content_tokens=content_tokens,
                extraction_input_tokens=extraction_input_tokens,
                summarized=summarized,
            )

        # Serializing is part of the best-effort write: a schema that won't
        # round-trip through JSON must cost the caller a cache entry, not the
        # extraction they already paid for.
        try:
            wrapper_json = json.dumps(
                {
                    "data": data.model_dump(mode="json"),
                    "content_tokens": content_tokens,
                    "extraction_input_tokens": extraction_input_tokens,
                    "summarized": summarized,
                },
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001 - caching is best-effort
            self._log(f"    ! extraction cache encode failed: {type(e).__name__}: {e}")
        else:
            self._cache_put(EXTRACT_NAMESPACE, extract_key, wrapper_json)

        return self._result(
            data=data,
            stopped="match",
            pages_fetched=pages_fetched,
            path=path,
            verdicts=verdicts,
            fallbacks_used=fallbacks_used,
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
        fallbacks_used: dict[str, str],
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
            fallbacks_used=dict(fallbacks_used),
        )
