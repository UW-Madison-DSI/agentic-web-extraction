from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AWE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    # Which provider backend to use (env: AWE_PROVIDER). Resolved by
    # providers.get_provider; "openai" is the only v0 implementation.
    provider: str = "openai"
    # Model for the structured-extraction call (env: AWE_MODEL_EXTRACT). The
    # stronger/more expensive model, since it must fill the caller's schema.
    model_extract: str = "gpt-5.5"
    # Model shared by the pre-screen and link-scorer calls (env: AWE_MODEL_SCREEN).
    # Both are cheap comparison calls, so they default to a smaller/faster model.
    model_screen: str = "gpt-5.4-mini"
    # Send every LLM call on the "flex" service tier (env: AWE_USE_FLEX). Flex bills
    # at Batch-API rates -- 50% off input and output -- but synchronously, so it needs
    # no restructuring of the wave loop, and its discount still stacks with the
    # provider-side prompt caching the screen/score prompts are shaped for. The price
    # is latency (calls can be much slower, so the client's read timeout is raised)
    # and capacity: flex may refuse a request outright with an uncharged 429
    # `resource_unavailable`, in which case the provider retries that one call on the
    # standard tier rather than lose the page's work. Off by default -- opt in for
    # bulk/offline runs where wall-clock doesn't matter. Not part of any cache key:
    # the tier changes price and latency, never response content.
    use_flex: bool = False
    # Whether to convert fetched HTML to Markdown before the LLM sees it
    # (env: AWE_NORMALIZE). On by default to cut token cost; PDFs are always
    # converted regardless.
    normalize: bool = True
    # Whether to fetch and read linked PDFs as page content (env: AWE_FOLLOW_PDF).
    # When False, PDF responses are treated as skipped (no LLM work, no budget cost).
    follow_pdf: bool = True
    # Ordered, comma-separated recovery routes tried when a fetch comes back
    # non-2xx (env: AWE_FETCH_FALLBACKS). Empty disables recovery, leaving only
    # the status guard: an error body is dropped rather than mistaken for the
    # page. Known routes are "jina" (r.jina.ai renders the URL live and reads
    # PDFs) and "wayback" (the Internet Archive's newest capture); unknown names
    # are ignored with a log line. Recovered content is returned under the
    # original URL, so paths and citations stay canonical, and the route is
    # recorded in FetchedPage.via / ExtractionResult.fallbacks_used.
    #
    # NOTE both routes send the URL being crawled to a third party. Set this
    # empty if that is unacceptable for your deployment.
    fetch_fallbacks: str = "jina,wayback"
    # What the Jina reader should return (env: AWE_JINA_RETURN_FORMAT), sent as
    # its X-Return-Format header. "html" (the default) yields the full DOM, so
    # normalization, link extraction, and the frontier behave exactly as they do
    # on a direct fetch. Empty selects Jina's readability pass instead: markdown
    # with the nav chrome stripped -- markedly fewer tokens, but only the links
    # its extraction kept, so the crawl has less to expand into.
    jina_return_format: str = "html"
    # Refuse Internet Archive captures older than this many days (env:
    # AWE_WAYBACK_MAX_AGE_DAYS). 0 (the default) accepts any age. Raise it above
    # zero when the criterion is time-sensitive and a years-old capture would be
    # worse than no page at all.
    wayback_max_age_days: int = 0
    # Fetch budget PER SEED: the max number of readable pages the traversal will
    # spend LLM calls on for each seed URL (env: AWE_MAX_FETCHES). With N seeds the
    # single shared frontier gets a total budget of max_fetches * N. Errored and
    # skipped (non-HTML/PDF) fetches don't count against it.
    max_fetches: int = 10
    # Treat every seed URL as content to extract from directly (env:
    # AWE_SEED_IS_CONTENT). When True, each seed is taken as a guaranteed match:
    # the pre-screen LLM call is skipped (pages are not judged for relevance) and
    # link-scoring is skipped (no outgoing links are queued), so the traversal
    # fetches exactly the seeds, consolidates them, extracts once, and stops. Use
    # it when you already know each seed is a target page and only want the
    # structured extraction, skipping the discovery machinery. Default False
    # preserves the screen-then-crawl behavior. Page caching still applies (a
    # distinct key segment keeps direct-mode entries from colliding with screened
    # ones for the same page).
    seed_is_content: bool = False
    # Input-token budget for the single consolidated extraction call (env:
    # AWE_MAX_CONTEXT_TOKENS). The normalized markdown of every screened-in page is
    # concatenated; if the result exceeds this budget it is summarized down (see
    # summarize.py) before extraction, which also uses this value as the per-chunk
    # target for the summarizer. The default sits safely under a large frontier-
    # model context window (e.g. gpt-5.5) while leaving room for the schema,
    # instructions, and output; lower it for models with smaller windows.
    max_context_tokens: int = 128000
    # Summarize the concatenated pages unconditionally, not just when they overflow
    # `max_context_tokens` (env: AWE_ALWAYS_SUMMARIZE). Off by default: summarization
    # is the only lossy step in the pipeline (the extract model never sees the
    # original text), so it is normally reserved for content that cannot otherwise
    # fit. Turn it on when the compression is wanted for its own sake -- to strip
    # boilerplate/navigation chrome down to a criteria-relevant retention list before
    # the strong model reads it, or to cut extraction cost on a long-but-fitting
    # concatenation. The map pass always runs; the reduce passes still only trigger
    # while the result is over budget, so a small corpus costs exactly one summarize
    # call per page. Part of the extraction cache key (it changes the extraction
    # input), and per-chunk summaries stay shared with overflow-triggered runs.
    always_summarize: bool = False
    # Output-token cap for the structured-extraction call (env:
    # AWE_MAX_OUTPUT_TOKENS). 0 (the default) sends no cap, leaving the endpoint's
    # own limit in charge -- the historical behavior, byte for byte on the wire.
    #
    # Set it when the extract model is prone to degenerate generation. A JSON
    # grammar permits arbitrary whitespace between tokens, so `\n  ` is always a
    # legal next token and schema-guided decoding cannot break a repetition loop
    # the way it would for a malformed key: a model that falls into one emits blank
    # indentation until *something* stops it. Uncapped, that something is the
    # endpoint's output limit, which can exceed the client read timeout -- the call
    # then surfaces as a timeout, gets silently re-sent by the SDK's own retries,
    # and one extraction burns many minutes without the caller ever seeing a
    # recoverable error. Capping converts that into a prompt failure the caller can
    # catch and re-roll cheaply -- a pydantic ValidationError when the truncated
    # document still parses as text (the usual case), or the AssertionError raised
    # in extract() when the SDK yields no parsed object at all. Note this is the
    # Responses API: unlike the chat-completions helper, it does not raise
    # LengthFinishReasonError on a length cutoff.
    #
    # Size it above the largest legitimate extraction for the schema in use; a cap
    # below that truncates good output, turning a working call into a failing one.
    max_output_tokens: int = 0
    # Wave concurrency / beam width (env: AWE_MAX_WORKERS). The traversal processes
    # the frontier in waves: it pops up to this many top-scored links at once and
    # fetches/screens/scores them concurrently in a thread pool, then folds the
    # results back. 1 makes the crawl strictly sequential (classic best-first);
    # higher values trade a little best-first strictness (best-first *within* a
    # wave) for parallelism, even on a single seed.
    max_workers: int = 8
    # Base tiktoken encoding used for token counting when the model name is unknown
    # to tiktoken (env: AWE_TIKTOKEN_ENCODING). Swappable providers routinely use
    # names tiktoken has no mapping for; the count is then an approximation, which
    # is fine -- it only drives the fit-or-summarize decision, not billing.
    tiktoken_encoding: str = "o200k_base"
    # Soft same-domain preference, expressed to the LLM rather than as a math
    # weight (env: AWE_PREFER_SEED_DOMAIN). When True, the pre-screen and
    # link-scorer calls are told the seed URL, the page/link URL, and a
    # Python-computed `on_seed_domain` signal, with an instruction to *disfavor*
    # off-domain pages/links -- a soft preference the model applies with its own
    # judgment, not a hard filter (a clearly on-target off-domain page still
    # matches / scores high). Off by default: pure LLM-score ordering with no
    # domain information supplied. The registrable-domain comparison is generic
    # (Public Suffix List, see frontier.py) -- no logic tied to any particular site.
    prefer_seed_domain: bool = False
    # User-Agent header sent on every crawl fetch (env: AWE_USER_AGENT), and the
    # agent name the robots.txt check below is evaluated against. The default is
    # the library's own generic string; deployments should replace it with one
    # naming the operator and a real contact URL, e.g.
    # "my-pipeline/1.0 (+https://example.edu/crawler; Some Team)". An
    # unattributable crawler is the reason a site operator's only recourse is a
    # complaint to whoever owns the IP.
    user_agent: str = "agentic-web-extraction/0.1 (+https://github.com/)"
    # Honor each origin's robots.txt for the configured user_agent
    # (env: AWE_RESPECT_ROBOTS). Off by default so v0.2 behavior is unchanged;
    # turn it on for any crawl of sites you don't own. A disallowed URL is skipped
    # before it is fetched -- no request, no budget slot, no LLM work. Failures to
    # obtain robots.txt fail OPEN (see robots.py for why).
    respect_robots: bool = False
    # Registrable domains exempt from the robots.txt check when respect_robots is
    # on (env: AWE_ROBOTS_OVERRIDES, comma-separated). For hosts whose robots.txt
    # blanket-disallows automated clients but whose content you are authorized to
    # read anyway -- your own sites, a portal you have an agreement with. Empty
    # (the default) exempts nothing. Note the crawl *boundary* is a separate,
    # stricter thing: see Extractor(allowed_domains=...).
    robots_overrides: str = ""
    # Content-addressed LLM-response cache path (SQLite), env: AWE_LLM_CACHE. On by
    # default: when a page's normalized content is unchanged from a prior run the
    # crawler replays its screen/extract/link-score outputs (and the final merge, if
    # every contributing page hit the cache) with zero LLM calls. Set to empty to
    # disable caching entirely. Fetching is unaffected -- this only skips model work.
    llm_cache: str = "data/llm_cache.sqlite"
    # Log file path (env: AWE_LOG_FILE), resolved relative to the current working
    # directory. Empty (the default) disables file logging entirely -- a single
    # knob. Progress lines always go to stderr regardless; setting a path adds a
    # durable, timestamped record for a host codebase that wants one.
    log_file: str = ""

    # Provider credentials, read from the un-prefixed OPENAI_* env vars (not AWE_*)
    # so a standard OpenAI environment works as-is. API key is a SecretStr so it
    # doesn't leak into logs/reprs; base URL lets you point at any OpenAI-compatible
    # endpoint. Both optional here; the OpenAI SDK errors at call time if unset.
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias="OPENAI_BASE_URL",
    )

    # Jina reader credential, read from the un-prefixed JINA_API_KEY (same
    # rationale as OPENAI_*: a standard environment works as-is). Optional --
    # r.jina.ai serves anonymous requests, just at a tighter per-IP rate limit,
    # which a wave of blocked pages can trip. SecretStr so it stays out of logs.
    jina_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="JINA_API_KEY",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
