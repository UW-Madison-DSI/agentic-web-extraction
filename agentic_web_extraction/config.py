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
    # Whether to convert fetched HTML to Markdown before the LLM sees it
    # (env: AWE_NORMALIZE). On by default to cut token cost; PDFs are always
    # converted regardless.
    normalize: bool = True
    # Whether to fetch and read linked PDFs as page content (env: AWE_FOLLOW_PDF).
    # When False, PDF responses are treated as skipped (no LLM work, no budget cost).
    follow_pdf: bool = True
    # Fetch budget: the max number of readable pages the traversal will spend LLM
    # calls on (env: AWE_MAX_FETCHES). The only traversal knob in v0. Errored and
    # skipped (non-HTML/PDF) fetches don't count against it.
    max_fetches: int = 10
    # When True, the traversal returns as soon as one page matches and is
    # extracted, instead of spending the whole budget gathering every match and
    # merging them (env: AWE_STOP_ON_FIRST_MATCH). Default False preserves the
    # gather-all-then-merge behavior.
    stop_on_first_match: bool = False
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
    # On-disk HTTP response cache (hishel), env: AWE_HTTP_CACHE. Persisted across
    # runs so weekly re-crawls can issue conditional GETs; empty string uses an
    # in-memory cache.
    http_cache: str = "data/http_cache.sqlite"
    # Log file path (env: AWE_LOG_FILE), resolved relative to the current working
    # directory. Empty (the default) disables file logging entirely -- a single
    # knob, mirroring AWE_HTTP_CACHE's empty-means-off convention. Progress lines
    # always go to stderr regardless; setting a path adds a durable, timestamped
    # record for a host codebase that wants one.
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
