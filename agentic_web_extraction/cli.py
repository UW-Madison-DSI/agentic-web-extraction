import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from .config import get_settings
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
    overrides: dict[str, int | bool] = {}
    if max_context_tokens is not None:
        overrides["max_context_tokens"] = max_context_tokens
    if always_summarize is not None:
        overrides["always_summarize"] = always_summarize
    if max_workers is not None:
        overrides["max_workers"] = max_workers
    settings = get_settings().model_copy(update=overrides) if overrides else None
    # Don't pass `cache` unless disabling: omitting it lets the Extractor build the
    # on-by-default store; `cache=None` is the explicit off switch.
    cache_kwargs = {"cache": None} if no_cache else {}
    extractor = Extractor(
        schema=model,
        criteria=criterion,
        prefer_seed_domain=prefer_seed_domain,
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
