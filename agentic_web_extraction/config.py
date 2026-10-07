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
    # Drop non-content chrome from the DOM before the HTML->Markdown conversion
    # (env: AWE_MAIN_CONTENT_ONLY). `script`/`style`/`noscript`/`template` are
    # always dropped when this runs -- they carry no reader-visible text -- and so
    # are `header`/`footer`/`nav`/`aside` elements that are NOT inside a `main` or
    # `article` (an article's own `<header>` usually holds its title and date,
    # which is exactly what a schema asks for).
    #
    # ON by default: site chrome is most of the DOM on a typical page and none of
    # it answers the criterion, so leaving it in means every screen, summarize and
    # extract call pays for the same masthead again. It *is* lossy -- content a
    # site puts in an `<aside>` outside a `main`/`article` does not reach the
    # extraction -- so turn it off (`--no-main-content-only`) when a site keeps
    # real content in its chrome. Flipping it either way is safe with a warm
    # cache: the page-cache key hashes the *filtered* markdown, so a change of
    # this setting misses rather than replaying the other rendering.
    #
    # Note it governs the *markdown* only: link discovery reads the unfiltered
    # HTML, so filtering a nav out of the extraction input never hides the links
    # in it from the scorer.
    main_content_only: bool = True
    # Cap on how many outgoing links from one page are sent to the link scorer
    # (env: AWE_MAX_LINKS_PER_PAGE, 0 = no cap, the default). A backstop for the
    # mega-navigation page that bills one scoring call for eight hundred links.
    #
    # The one knob here left OFF, and the reason is that no cap is right for the
    # typical page. Truncation is keyed on *document order*, so on an ordinary
    # site a cap spends its allowance on the site-wide nav at the top of the
    # markup and drops the in-content links underneath it -- the crawl does not
    # get a thinner frontier, it gets the wrong one. Every other default here can
    # at worst make a crawl slower or a page thinner; this one decides which
    # pages exist. Set it per crawl for the specific site that needs it.
    max_links_per_page: int = 0
    # --- politeness -------------------------------------------------------
    # Minimum seconds between the *starts* of two fetches to the same registrable
    # domain (env: AWE_REQUEST_DELAY). ON by default, because being impolite is a
    # defect whose cost lands on somebody else: the
    # crawl boundary exists to keep a traversal on one site, so `max_workers`
    # workers concentrate on a single origin and, before this, arrived as fast as
    # httpx would go. 0.5s caps one origin at ~2 requests/second however many
    # workers are running, which is well inside what the per-page LLM stages
    # sustain anyway -- so in practice it costs wall-clock only on a cache-warm
    # replay. Set to 0.0 for the pre-0.3 behavior.
    #
    # Enforced in fetch.py (transport state, like the domain memo beside it) and
    # keyed through frontier.domain_of like every other host comparison. When
    # respect_robots is on and an origin publishes a Crawl-delay, the larger of
    # the two wins for that origin.
    request_delay: float = 0.5
    # Hard cap on fetches in flight to one registrable domain at a time
    # (env: AWE_MAX_PER_DOMAIN, 0 = no cap). `request_delay` already bounds the
    # *rate*, which is what a site operator feels; this bounds concurrent
    # connections, which is what a small origin's connection pool feels.
    #
    # ON at 4, which is half the default `max_workers` and therefore a ceiling
    # rather than a schedule: with starts already 0.5s apart it binds only when
    # an origin is answering slowly enough that five requests overlap, which is
    # exactly when a fragile origin should not be sent a sixth. Costs a healthy
    # site nothing. Set 0 for no cap.
    max_per_domain: int = 4

    # Minimum characters of visible text a *successfully fetched* HTML page must
    # carry before it is accepted as content (env: AWE_MIN_PAGE_TEXT_CHARS,
    # 0 = off, the default). A single-page app answers 200 with an empty shell:
    # that sails past the status guard, normalizes to almost nothing, gets
    # screened out as irrelevant, and leaves a log that says nothing was wrong.
    # Set this and a sub-threshold body is treated as a failure to *obtain*
    # content -- the same trigger the status guard and the transport handler use --
    # so the recovery chain gets a turn at rendering it.
    #
    # ON at 200, matching `min_recovered_text_chars` so one number means "this is
    # the page" on both sides of the recovery chain. A client-rendered shell is
    # otherwise the one refusal with no trigger and no trace, and the check cannot
    # make a result worse: the recovered body only wins if it carries *more*
    # visible text than the origin's.
    #
    # What it costs is requests, at a third party, on a page the origin already
    # answered -- a genuinely short page is indistinguishable from a shell by
    # character count, so a "this document has moved" stub reaches jina/wayback
    # too. Lower it rather than zeroing it if that trade is wrong for you; the
    # shells it exists for measure in the tens of characters. Set 0 to accept any
    # 200 as content, which is also what an empty `fetch_fallbacks` amounts to.
    min_page_text_chars: int = 200

    # Whether to fetch and read linked PDFs as page content (env: AWE_FOLLOW_PDF).
    # When False, PDF responses are treated as skipped (no LLM work, no budget cost).
    follow_pdf: bool = True
    # How many times an origin fetch is attempted before it is given up on
    # (env: AWE_FETCH_ATTEMPTS, default 3 = the historical behavior). Applies to
    # 5xx and connect errors, which genuinely are transient. A *read* timeout is
    # capped at two attempts regardless: the failure mode it stands for in
    # practice is an edge CDN tarpitting a non-browser client, which is
    # deterministic -- the later attempts only spend the read timeout again
    # (30s each, and with max_workers of them in flight the whole wave stalls)
    # to be refused identically. Recovery (below) is the thing that can actually
    # turn that page back into content, so get there sooner.
    fetch_attempts: int = 3
    # How many *unanswered* fetches on one registrable domain write the default
    # transport off for the rest of the process (env: AWE_TRANSPORT_MEMO_FAILURES,
    # 0 disables). "Unanswered" means no response arrived at all -- a read timeout,
    # a dropped connection, a malformed redirect header. An origin that tarpits
    # non-browser clients does that to every URL, so without a memo each page pays
    # the whole attempt budget again (~35s apiece, and a crawl of one such host was
    # observed spending ~10 minutes on it) before reaching the recovery that can
    # actually read it. Once the count is reached, later URLs on that domain skip
    # straight to the routes in fetch_fallbacks -- and only when at least one route
    # is configured, since with recovery off there is nothing to skip to.
    #
    # Deliberately hard to latch, because a wrong write-off routes a healthy site
    # through the recovery chain. Any response clears the count -- a 403 or a 5xx
    # included, since a host refusing out loud is answering in one round-trip, which
    # is nothing to skip -- and a domain that has answered *even once* is never
    # written off, however many later fetches time out (a host serving six pages and
    # timing out on two big ones is slow, not silent, and interleaved workers make
    # the count alone unable to tell those apart). Only timeouts and network-level
    # failures count; a malformed Location header or an unfetchable URL says nothing
    # about the host. The failure is attributed to the host that actually failed,
    # which on a redirect is not the one asked for.
    #
    # It can never lose a page either: if no route can read a URL on a written-off
    # domain, the origin is asked after all (and a fetch that then succeeds clears
    # the memo -- the only way back). Each crawl starts by forgetting everything, so
    # a write-off lasts exactly as long as it pays for itself.
    transport_memo_failures: int = 2
    # Ordered, comma-separated recovery routes tried when a fetch fails to
    # produce content (env: AWE_FETCH_FALLBACKS) -- a non-2xx response, or no
    # response at all (a connection dropped or tarpitted by an edge CDN that
    # would rather stay silent than refuse out loud). Empty disables recovery,
    # leaving only the status guard: an error body is dropped rather than
    # mistaken for the page. Known routes are "impersonate" (re-request the
    # origin directly with a browser TLS/HTTP fingerprint; needs the knobs
    # below), "jina" (r.jina.ai renders the URL live and reads PDFs) and
    # "wayback" (the Internet Archive's newest capture); unknown names are
    # ignored with a log line. Recovered content is returned under the original
    # URL, so paths and citations stay canonical, and the route is recorded in
    # FetchedPage.via / ExtractionResult.fallbacks_used.
    #
    # NOTE jina and wayback send the URL being crawled to a third party;
    # "impersonate" talks to the origin only. Set this empty if outbound
    # recovery is unacceptable for your deployment.
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
    # Minimum visible-text length (characters, markup and script stripped) for a
    # recovered body to be accepted as the page (env: AWE_MIN_RECOVERED_TEXT_CHARS,
    # 0 disables). A route that answers 200 with a client-rendered shell -- one
    # observed homepage came back as 554 bytes of empty <div>s, where a rendering
    # route returned 147KB for the same URL -- otherwise ends the chain, because
    # "a body arrived" was read as "the page was obtained". Under the threshold the
    # route is treated as a decline and the next one is tried.
    #
    # Never a way to lose a page: if no route clears the threshold, the fullest body
    # obtained is returned anyway. PDFs are exempt (their text is carried as bytes,
    # not as `text`).
    #
    # The cost is that a *genuinely* short page -- a "this document has moved" stub, a
    # link-only landing page -- also falls through, so its URL reaches whatever comes
    # next in the chain even though the origin already served it. Where that matters
    # more than the shell (jina/wayback disclose the URL to a third party; the
    # origin-only impersonate route does not), lower the threshold rather than
    # disabling it: the shells this exists for measure in the tens of characters.
    min_recovered_text_chars: int = 200
    # curl_cffi impersonation target for the "impersonate" recovery route
    # (env: AWE_IMPERSONATE) -- "chrome", "chrome124", "safari", "firefox", "edge".
    # Empty (the default) disables the route entirely, so nothing changes for an
    # existing deployment even if the route is named in fetch_fallbacks.
    #
    # What it buys: some edge CDNs refuse on TLS/HTTP *fingerprint* alone -- the
    # shape of the handshake, not who we claim to be -- and answer a plain httpx
    # client with a silent drop. curl_cffi re-requests through a libcurl built to
    # produce a browser's handshake, so those origins serve the page to the same
    # honest User-Agent they were refusing. Requires the optional dependency:
    # `pip install "agentic-web-extraction[impersonate]"`. Absent, the route
    # declines with a log line rather than failing the crawl.
    impersonate: str = ""
    # Send the impersonated browser's own User-Agent instead of ours on that route
    # (env: AWE_IMPERSONATE_BROWSER_UA). Off by default, and think before turning
    # it on: it DROPS ATTRIBUTION. With it off the route sends a browser handshake
    # under the crawl's own identifying string, which is a shape mismatch some bot
    # managers reject outright but leaves a site operator someone to write to. With
    # it on, the request is indistinguishable from a browser -- a full masquerade,
    # typically at a site that is refusing to identify itself to us in return. That
    # is an institutional call about a site's refusal signal, not a default, so it
    # is a separate switch from `impersonate` and an operator has to type it out.
    impersonate_browser_ua: bool = False
    # Restrict the impersonate route to these registrable domains
    # (env: AWE_IMPERSONATE_DOMAINS, comma-separated). Empty (the default) means
    # every host, which only takes effect once `impersonate` is set. Scope it to
    # the handful of sites that actually need the escalation rather than changing
    # the posture of a whole crawl -- especially alongside impersonate_browser_ua.
    impersonate_domains: str = ""
    # Per-request timeout for the impersonate route in seconds
    # (env: AWE_IMPERSONATE_TIMEOUT). Shorter than the jina/wayback client's
    # generous 120s because the origin is answering directly -- there is no
    # server-side render or cold-storage replay to wait out.
    impersonate_timeout: float = 30.0
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
    # Per-link output-token cap for the link-scoring call (env:
    # AWE_SCORE_OUTPUT_TOKENS_PER_LINK). Each score_links call is capped at
    # 4000 + this × len(links); 0 sends no cap. The 4000 base leaves room for
    # reasoning tokens, which bill as output.
    #
    # The scorer is prone to the same whitespace/repetition loop described at
    # max_output_tokens above, but unlike extraction its output size is known in
    # advance -- one url and one score per link -- so the cap is on by default.
    # Normal calls use roughly 1.8-2.3k output tokens for 28-77 links, so the
    # default leaves 3-5x headroom. A capped runaway fails as it did before (the
    # page's links stay unscored and nothing is cached), only in a minute or two
    # rather than the 13-22 minutes it takes to outlast the read timeout and the
    # SDK's retries. Not part of any cache key: it changes cost and latency, not
    # what a successful call returns.
    score_output_tokens_per_link: int = 100
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
    # --- sitemap seeding --------------------------------------------------
    # Before the traversal starts, read each seed origin's sitemap and offer its
    # URLs to the link scorer, so the frontier begins with pages the site itself
    # advertises rather than only whatever the seed page links to
    # (env: AWE_USE_SITEMAP). This is best-first search's weakest spot: page 12 of
    # a listing is often reachable only through a paginator nothing scores highly,
    # and a sitemap puts it in the frontier at the start.
    #
    # ON by default. Discovered URLs are scored by the same scorer, gated by the
    # same crawl boundary, and (with respect_robots on) checked against the same
    # policy as any other link -- they enter the frontier, they do not bypass it,
    # so the worst case is candidates the scorer ranks low and never pops. What it
    # costs is bounded and spent at the origin being crawled, not a third party:
    # at most `sitemap_max_documents` extra requests per seed origin, paced like
    # every other fetch, before the traversal starts. It does change which pages a
    # fixed budget reaches, which is the point -- set it false when you want the
    # frontier to contain only what the seed page itself links to.
    use_sitemap: bool = True
    # Most sitemap documents fetched per seed origin, index files included
    # (env: AWE_SITEMAP_MAX_DOCUMENTS). A sitemap index can name hundreds of
    # children; this bounds what one seed can cost before the crawl begins.
    sitemap_max_documents: int = 5
    # Most URLs handed to the scorer from one origin's sitemap
    # (env: AWE_SITEMAP_MAX_URLS). A real sitemap can carry 50,000 entries, which
    # would be one enormous scoring call and a frontier nothing else can outrank.
    sitemap_max_urls: int = 200
    # Largest sitemap document body read, in bytes (env: AWE_SITEMAP_MAX_BYTES).
    # These are attacker-controlled XML from a third party; the parser guards
    # against entity expansion separately (see sitemap.py), and this bounds the
    # plain "serve a 2 GB file" case.
    sitemap_max_bytes: int = 10_000_000

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


def settings_schema() -> dict:
    """JSON Schema for every ``AWE_*`` knob: names, types, and defaults.

    Published so a host codebase can validate a configuration, or generate a form
    for one, without importing the Extractor. Also what ``awe schema`` prints.

    It carries the *shape*, not the rationale: pydantic builds a schema from field
    types and defaults, and the reasoning for each knob lives in the comments above
    it, which do not travel. Read those (or the README) for why a default is what
    it is; read this to find out that ``request_delay`` is a number defaulting to
    0.5 without importing anything.

    Each property additionally carries an ``env`` key naming the variable that
    sets it. Without one a consumer has to know that most fields take an ``AWE_``
    prefix while the credentials -- which pydantic renders under their aliases --
    do not; "guess the naming convention" is exactly the job this exists to remove.

    Values are never included: this describes the settings, it does not read the
    environment, so it is safe to print, log, or serve. The credentials appear as
    key *names* only, like every other setting.
    """
    schema = Settings.model_json_schema()
    properties = schema.get("properties", {})
    prefix = Settings.model_config.get("env_prefix", "")
    for name, field in Settings.model_fields.items():
        # A field with a string validation alias is rendered under that alias, and
        # the alias is already the literal environment variable name.
        alias = field.validation_alias
        alias = alias if isinstance(alias, str) else None
        key = alias or name
        if key in properties:
            properties[key]["env"] = alias or f"{prefix}{name}".upper()
    return schema
