import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from .config import get_settings, settings_schema
from .extractor import Extractor

app = typer.Typer(
    add_completion=False,
    help="Agentic best-first traversal that extracts structured data from the web.",
    no_args_is_help=True,
)


@app.callback()
def _main() -> None:
    """Force subcommand mode so `awe extract ...` is the contract."""


def load_schema(spec: str) -> type[BaseModel]:
    if ":" not in spec:
        raise typer.BadParameter(
            "schema must be 'module.path:ClassName' or '/path/file.py:ClassName'"
        )
    head, _, class_name = spec.rpartition(":")
    path = Path(head)
    if path.suffix == ".py" and path.exists():
        module_spec = importlib.util.spec_from_file_location(path.stem, path)
        if module_spec is None or module_spec.loader is None:
            raise typer.BadParameter(f"could not load schema file: {head}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(head)
    obj = getattr(module, class_name, None)
    if obj is None:
        raise typer.BadParameter(f"{class_name!r} not found in {head!r}")
    if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
        raise typer.BadParameter(
            f"{class_name!r} must be a Pydantic BaseModel subclass"
        )
    return obj


def load_criteria(value: str) -> str:
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8").strip()
    return value


@app.command()
def extract(
    schema: Annotated[
        str,
        typer.Option(
            "--schema",
            help="Pydantic model reference: 'module.path:ClassName' or 'path/file.py:ClassName'.",
        ),
    ],
    criteria: Annotated[
        str,
        typer.Option(
            "--criteria",
            help="Screening criterion. Prefix with '@' to read from a file.",
        ),
    ],
    seed_url: Annotated[
        list[str],
        typer.Option(
            "--seed-url",
            help=(
                "URL to start traversal from. Repeat the flag to pass several "
                "seeds; everything screened-in across all of them is pooled into "
                "one extraction, and the fetch budget applies per seed."
            ),
        ),
    ],
    max_fetches: Annotated[
        int | None,
        typer.Option(
            "--max-fetches",
            help="Fetch budget PER SEED. Defaults to AWE_MAX_FETCHES (10).",
        ),
    ] = None,
    max_context_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-context-tokens",
            help=(
                "Input-token budget for the single consolidated extraction. If the "
                "concatenated pages exceed it, they are summarized down first. "
                "Defaults to AWE_MAX_CONTEXT_TOKENS (128000)."
            ),
        ),
    ] = None,
    always_summarize: Annotated[
        bool | None,
        typer.Option(
            "--always-summarize/--no-always-summarize",
            help=(
                "Summarize the concatenated pages even when they already fit "
                "--max-context-tokens (normally summarization only kicks in on "
                "overflow). Compresses boilerplate down to a criteria-relevant "
                "retention list and cuts extraction input cost, at the price of "
                "one summarize call per page and a lossy step the strong model "
                "cannot see past. Defaults to AWE_ALWAYS_SUMMARIZE (off)."
            ),
        ),
    ] = None,
    max_workers: Annotated[
        int | None,
        typer.Option(
            "--max-workers",
            help=(
                "Wave concurrency / beam width: how many top-scored links are "
                "fetched/screened/scored at once. Defaults to AWE_MAX_WORKERS (8)."
            ),
        ),
    ] = None,
    request_delay: Annotated[
        float | None,
        typer.Option(
            "--request-delay",
            help=(
                "Minimum seconds between fetches to the same registrable domain. "
                "Defaults to AWE_REQUEST_DELAY (0.5). Set 0 to disable pacing -- "
                "note the crawl boundary concentrates every worker on one origin, "
                "so unpaced means --max-workers requests at once to one site. When "
                "--respect-robots is on and an origin publishes a Crawl-delay, the "
                "larger of the two applies."
            ),
        ),
    ] = None,
    max_per_domain: Annotated[
        int | None,
        typer.Option(
            "--max-per-domain",
            help=(
                "Cap on fetches in flight to one registrable domain. Defaults to "
                "AWE_MAX_PER_DOMAIN (4, half the default worker count; 0 = no "
                "cap). --request-delay already bounds the rate; this bounds the "
                "connections a small origin's pool sees."
            ),
        ),
    ] = None,
    main_content_only: Annotated[
        bool | None,
        typer.Option(
            "--main-content-only/--no-main-content-only",
            help=(
                "Drop script/style/noscript/template, and header/footer/nav/aside "
                "outside a <main> or <article>, before converting HTML to Markdown. "
                "Cuts site chrome out of every screen, summarize and extract call. "
                "Defaults to AWE_MAIN_CONTENT_ONLY (on); --no-main-content-only "
                "keeps everything, for a site that puts real content in its chrome. "
                "Link discovery reads the unfiltered HTML either way."
            ),
        ),
    ] = None,
    max_links_per_page: Annotated[
        int | None,
        typer.Option(
            "--max-links-per-page",
            help=(
                "Cap on outgoing links from one page sent to the link scorer. "
                "Defaults to AWE_MAX_LINKS_PER_PAGE (0 = no cap). Lossy, and keyed "
                "on document order: a cap can spend its whole allowance on the "
                "site-wide nav at the top of the markup."
            ),
        ),
    ] = None,
    min_page_text_chars: Annotated[
        int | None,
        typer.Option(
            "--min-page-text-chars",
            help=(
                "Treat a 200 carrying less visible text than this as a failure to "
                "obtain content and send it through the recovery chain, so a "
                "client-rendered shell gets rendered rather than silently screened "
                "out. Defaults to AWE_MIN_PAGE_TEXT_CHARS (200); 0 turns it off. "
                "The recovered body only wins if it is fuller than the origin's."
            ),
        ),
    ] = None,
    use_sitemap: Annotated[
        bool | None,
        typer.Option(
            "--use-sitemap/--no-use-sitemap",
            help=(
                "Before traversing, read each seed origin's sitemap and offer its "
                "URLs to the link scorer, so the frontier starts with pages the "
                "site advertises rather than only what the seed page links to. "
                "Discovered URLs are scored, boundary-gated and robots-checked like "
                "any other link. Defaults to AWE_USE_SITEMAP (on); "
                "--no-use-sitemap keeps the frontier to what the seed page links to."
            ),
        ),
    ] = None,
    seed_is_content: Annotated[
        bool | None,
        typer.Option(
            "--seed-is-content/--no-seed-is-content",
            help=(
                "Treat every seed URL as content to extract from directly: skip "
                "pre-screening and link-scoring, then consolidate and extract the "
                "seed pages (no links are followed). Use it when each seed is "
                "already a known target page. Defaults to AWE_SEED_IS_CONTENT (off)."
            ),
        ),
    ] = None,
    prefer_seed_domain: Annotated[
        bool | None,
        typer.Option(
            "--prefer-seed-domain/--no-prefer-seed-domain",
            help=(
                "Softly disfavor pages/links off the seed's registrable domain. "
                "When on, the screen and link-scorer calls are told the seed/page "
                "URL and a computed on-domain signal, and asked to disfavor "
                "off-domain content (a nudge, not a filter -- nothing is excluded). "
                "Defaults to AWE_PREFER_SEED_DOMAIN (off). Cache-stability text "
                "filters are Python-API only; use the Python API to pass them."
            ),
        ),
    ] = None,
    allowed_domain: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-domain",
            help=(
                "Hard crawl boundary: a registrable domain (or a URL/host to take "
                "one from) a link may be queued from. Repeat the flag to allow "
                "several. Every seed's own domain is always included, so passing "
                "just the seed's domain means 'this site only'. Omit the flag "
                "entirely for the default: no boundary, any link the scorer likes "
                "may be fetched."
            ),
        ),
    ] = None,
    allow_seed_redirect_domains: Annotated[
        bool,
        typer.Option(
            "--allow-seed-redirect-domains/--no-allow-seed-redirect-domains",
            help=(
                "When a seed redirects to a different registrable domain (a rebrand, "
                "a moved host), add where it landed to the boundary so the crawl can "
                "continue there. OFF by default: whoever controls the seed's DNS then "
                "decides where it lands, so this is the one way a party other than "
                "you can widen the boundary. Only meaningful with --allowed-domain."
            ),
        ),
    ] = False,
    user_agent: Annotated[
        str | None,
        typer.Option(
            "--user-agent",
            help=(
                "User-Agent sent on every fetch, and the agent robots.txt is "
                "evaluated against. Defaults to AWE_USER_AGENT. Use a string that "
                "names the operator and a real contact URL."
            ),
        ),
    ] = None,
    respect_robots: Annotated[
        bool | None,
        typer.Option(
            "--respect-robots/--no-respect-robots",
            help=(
                "Honor each origin's robots.txt for the configured user agent, "
                "checked before the fetch. Defaults to AWE_RESPECT_ROBOTS (off). "
                "A robots.txt that cannot be obtained fails open."
            ),
        ),
    ] = None,
    robots_override: Annotated[
        list[str] | None,
        typer.Option(
            "--robots-override",
            help=(
                "Domain exempt from the robots.txt check (repeatable). For hosts "
                "whose robots.txt disallows automated clients but whose content you "
                "are authorized to read. Defaults to AWE_ROBOTS_OVERRIDES."
            ),
        ),
    ] = None,
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            help=(
                "Also append timestamped progress lines to this file (lines always "
                "go to stderr regardless). Empty disables it. Defaults to "
                "AWE_LOG_FILE (off)."
            ),
        ),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help=(
                "Disable the on-by-default LLM-response cache. By default an "
                "unchanged page replays its screen/extract/score outputs (and a "
                "merge whose inputs all hit the cache) with no LLM calls; the store "
                "is SQLite at AWE_LLM_CACHE (data/llm_cache.sqlite)."
            ),
        ),
    ] = False,
) -> None:
    model = load_schema(schema)
    criterion = load_criteria(criteria)
    # Apply the settings-only knobs from the CLI via a copy of the base settings
    # (leaving the cached singleton untouched); everything else keeps its
    # AWE_* / env default.
    overrides: dict[str, int | bool | float] = {}
    if max_context_tokens is not None:
        overrides["max_context_tokens"] = max_context_tokens
    if always_summarize is not None:
        overrides["always_summarize"] = always_summarize
    if max_workers is not None:
        overrides["max_workers"] = max_workers
    if request_delay is not None:
        overrides["request_delay"] = request_delay
    if max_per_domain is not None:
        overrides["max_per_domain"] = max_per_domain
    if main_content_only is not None:
        overrides["main_content_only"] = main_content_only
    if max_links_per_page is not None:
        overrides["max_links_per_page"] = max_links_per_page
    if min_page_text_chars is not None:
        overrides["min_page_text_chars"] = min_page_text_chars
    if use_sitemap is not None:
        overrides["use_sitemap"] = use_sitemap
    settings = get_settings().model_copy(update=overrides) if overrides else None
    # Don't pass `cache` unless disabling: omitting it lets the Extractor build the
    # on-by-default store; `cache=None` is the explicit off switch.
    cache_kwargs = {"cache": None} if no_cache else {}
    # No --allowed-domain means no boundary. Click hands back an empty tuple rather
    # than None for an unused repeatable option, and an empty *set* would mean the
    # opposite thing (seeds and nowhere else), so normalize the empty case to None.
    extractor = Extractor(
        schema=model,
        criteria=criterion,
        prefer_seed_domain=prefer_seed_domain,
        allowed_domains=list(allowed_domain) if allowed_domain else None,
        allow_seed_redirect_domains=allow_seed_redirect_domains,
        user_agent=user_agent,
        respect_robots=respect_robots,
        robots_overrides=",".join(robots_override) if robots_override else None,
        settings=settings,
        log_file=log_file,
        **cache_kwargs,
    )
    result = extractor.extract(
        seed_url,
        max_fetches=max_fetches,
        seed_is_content=seed_is_content,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.stopped_reason == "match" else 2)


@app.command()
def schema() -> None:
    """Print the JSON Schema of every AWE_* setting: names, types and defaults.

    For a host codebase that wants to validate a configuration, or build a form
    for one, without importing the Extractor or re-reading the README. Values are
    never included -- this describes the settings, it does not read the
    environment -- so the output is safe to print, log, or serve.
    """
    typer.echo(json.dumps(settings_schema(), indent=2))
