"""Render a caller's Pydantic schema as a compact, LLM-readable field outline.

Summarization is the only lossy step in the pipeline: once the map-reduce in
[summarize.py](summarize.py) has compressed a page, the extraction model never
sees the original text. The criterion tells the summarizer what is *topically*
relevant, but not which concrete values the extraction is obliged to produce --
so a criterion-only summarizer happily discards the dates, numbers, identifiers,
and URLs that a schema field requires. Handing the summarizer the schema fixes
that: it is a precise, field-by-field statement of what must survive.

The raw ``model_json_schema()`` would do, but it is mostly scaffolding -- every
leaf carries a ``title`` and a ``type`` wrapper, nullable fields expand to an
``anyOf`` pair, and nested models sit behind ``$ref`` indirection the model has
to resolve on its own. This module renders the same information as a short
outline instead, typically a fraction of the tokens.

Why an outline and not a flat list of dotted paths: a flat list duplicates every
shared ``$defs`` entry once per referencing field, and a self-referential schema
has infinitely many dotted paths (so flattening needs a depth cap that loses the
very structure it set out to express). Emitting each definition once, as its own
named block, keeps nesting intact and makes recursion a non-issue.

Domain-agnostic by construction: it reads only the JSON Schema that Pydantic
emits, so it works for any caller schema and knows nothing about any domain.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def _oneline(text: str) -> str:
    """Collapse a description to a single line so one field stays one row."""
    return " ".join(text.split())


def _resolve_root(schema: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Follow the top-level ``$ref`` a self-referential model produces.

    A model that refers to itself is hoisted wholesale into ``$defs``, leaving the
    root document as a bare ``{"$ref": ..., "$defs": {...}}``. Resolving it here
    means the root block renders like any other. The resolved name is returned too
    so the caller can suppress the duplicate definition block -- the root's own
    fields will reference it right back.
    """
    ref = schema.get("$ref")
    if not ref:
        return schema, None
    name = ref.rsplit("/", 1)[-1]
    definition = (schema.get("$defs") or {}).get(name)
    if not isinstance(definition, dict):
        return schema, None
    return definition, name


def _type_name(node: dict[str, Any], refs: list[str]) -> str:
    """Human-readable type for one JSON Schema node.

    Appends every ``$defs`` name encountered to `refs`, in first-seen order, so
    the caller can emit definition blocks in the order they are referenced.
    """
    # Pydantic wraps a $ref in a single-element allOf when the field also carries
    # a default or description.
    all_of = node.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        return _type_name(all_of[0], refs)

    ref = node.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name not in refs:
            refs.append(name)
        return name

    any_of = node.get("anyOf") or node.get("oneOf")
    if isinstance(any_of, list):
        nullable = any(b.get("type") == "null" for b in any_of)
        inner = [_type_name(b, refs) for b in any_of if b.get("type") != "null"]
        base = " | ".join(inner) or "any"
        return f"{base}?" if nullable else base

    enum = node.get("enum")
    if enum is not None:
        return "one of " + "|".join(str(v) for v in enum)

    node_type = node.get("type")
    if node_type == "array":
        return f"list[{_type_name(node.get('items') or {}, refs)}]"
    if node_type == "object":
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            return f"dict[string, {_type_name(extra, refs)}]"
        return "object"
    if isinstance(node_type, list):  # e.g. ["string", "null"]
        non_null = [t for t in node_type if t != "null"]
        base = " | ".join(non_null) or "any"
        return f"{base}?" if "null" in node_type else base
    return node_type or "any"


def _block(name: str, node: dict[str, Any], refs: list[str]) -> str:
    """Render one object definition: its name, docstring, and one row per field."""
    lines = [f"{name}:"]
    description = node.get("description")
    if description:
        lines.append(f"  # {_oneline(description)}")
    required = set(node.get("required") or [])
    properties = node.get("properties") or {}
    if not properties:
        lines.append("  # (no fields)")
    for field, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        marker = " (required)" if field in required else ""
        field_desc = spec.get("description")
        suffix = f" -- {_oneline(field_desc)}" if field_desc else ""
        lines.append(f"  {field}: {_type_name(spec, refs)}{marker}{suffix}")
    return "\n".join(lines)


def outline_json_schema(schema: dict[str, Any]) -> str:
    """Render a JSON Schema document as a compact outline (see module docstring)."""
    root, root_name = _resolve_root(schema)
    defs = schema.get("$defs") or {}
    refs: list[str] = []
    blocks = [_block(root_name or root.get("title") or "Root", root, refs)]
    # Breadth-first over `refs`, which _block/_type_name append to as they render:
    # definitions come out in first-reference order, and each is emitted once, so a
    # recursive schema terminates naturally. The root is pre-marked when it came from
    # $defs, so a self-referential model doesn't print its block a second time.
    emitted: set[str] = {root_name} if root_name else set()
    i = 0
    while i < len(refs):
        name = refs[i]
        i += 1
        if name in emitted:
            continue
        emitted.add(name)
        definition = defs.get(name)
        if isinstance(definition, dict) and definition.get("properties") is not None:
            blocks.append(_block(name, definition, refs))
        elif isinstance(definition, dict) and definition.get("enum") is not None:
            values = "|".join(str(v) for v in definition["enum"])
            blocks.append(f"{name}: one of {values}")
    # Anything unreachable from the root (shouldn't happen, but a hand-written or
    # customized schema could manage it) is appended in a stable order.
    for name in sorted(set(defs) - emitted):
        definition = defs[name]
        if isinstance(definition, dict) and definition.get("properties") is not None:
            blocks.append(_block(name, definition, refs))
    return "\n\n".join(blocks)


def schema_outline(schema: type[BaseModel]) -> str:
    """Compact field outline for a Pydantic model, for use in a prompt.

    Example (the container-with-a-list shape the consolidated extraction expects)::

        RecordSet:
          items: list[Record]

        Record:
          title: string (required)
          date: string?  -- ISO date if stated
          quantity: string?

    Never raises on an exotic schema: an unrenderable node degrades to its type
    name rather than aborting a crawl over a prompt detail.
    """
    return outline_json_schema(schema.model_json_schema())


def schema_outline_safe(schema: type[BaseModel]) -> str:
    """`schema_outline`, but falling back to compact JSON if rendering fails.

    The outline is a prompt nicety, not a correctness requirement, so a schema
    this renderer cannot handle should degrade to the raw JSON Schema (still
    useful to the model) instead of failing the summarization pass.
    """
    try:
        return schema_outline(schema)
    except Exception:  # noqa: BLE001 - prompt formatting must never break a crawl
        try:
            return json.dumps(schema.model_json_schema(), sort_keys=True)
        except Exception:  # noqa: BLE001
            return ""
