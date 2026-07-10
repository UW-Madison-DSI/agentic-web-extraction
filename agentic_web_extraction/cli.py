import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

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
        str,
        typer.Option("--seed-url", help="URL to start traversal from."),
    ],
    max_fetches: Annotated[
        int | None,
        typer.Option(
            "--max-fetches",
            help="Fetch budget. Defaults to AWE_MAX_FETCHES (10).",
        ),
    ] = None,
    stop_on_first_match: Annotated[
        bool | None,
        typer.Option(
            "--stop-on-first-match/--gather-all-matches",
            help=(
                "Stop as soon as one page matches, or spend the whole budget "
                "gathering every match and merging them. Defaults to "
                "AWE_STOP_ON_FIRST_MATCH (gather-all)."
            ),
        ),
    ] = None,
    off_domain_weight: Annotated[
        float | None,
        typer.Option(
            "--off-domain-weight",
            help=(
                "Score weight for outgoing links off the seed's registrable "
                "domain. 1.0 (default) = full weight, no preference; < 1.0 softly "
                "down-weights them (a nudge, not a filter); 0.0 is the strongest "
                "preference. Defaults to AWE_OFF_DOMAIN_WEIGHT (1.0). Cache-"
                "stability text filters are Python-API only; use the Python API "
                "to pass them."
            ),
        ),
    ] = None,
) -> None:
    model = load_schema(schema)
    criterion = load_criteria(criteria)
    extractor = Extractor(
        schema=model,
        criteria=criterion,
        off_domain_weight=off_domain_weight,
    )
    result = extractor.extract(
        seed_url=seed_url,
        max_fetches=max_fetches,
        stop_on_first_match=stop_on_first_match,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.stopped_reason == "match" else 2)
