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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
