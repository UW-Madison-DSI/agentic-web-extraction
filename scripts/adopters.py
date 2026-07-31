# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Scan the org for repos that actually depend on this package, render badges.

GHCR / GitHub Packages exposes no pull or download metric, and PyPI download
stats say nothing about *which* org repos adopted us. The only available
adoption signal is the org's own source, via the Code Search API.

Counting rule (see the invariants below): a repo counts as an adopter only when a
*dependency manifest* declares this distribution on a non-comment line. An import
of the module, or an `awe extract` invocation, is necessary but not sufficient —
a vendored copy, a sibling checkout, or a notebook that pip-installs from a
branch all produce it, and none of them pin a version. Those are reported in the
job summary, never counted.

Hard-fails on any API error or under-scoped token: a silent zero is
indistinguishable from real disadoption, so a wrong badge is worse than a failed
run. Nothing degrades to a public-only scan.

    GH_TOKEN=$(gh auth token) uv run --no-project scripts/adopters.py
    GH_TOKEN=...              uv run --no-project scripts/adopters.py --write
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ORG = "UW-Madison-DSI"
PACKAGE = "agentic-web-extraction"  # the distribution name a manifest declares
MODULE = "agentic_web_extraction"  # the import name (weak signal only)
SELF = "UW-Madison-DSI/agentic-web-extraction"  # excluded; compared casefolded

MARKER_START = "<!-- adopters:start -->"
MARKER_END = "<!-- adopters:end -->"

README = Path(__file__).resolve().parent.parent / "README.md"

API_ROOT = "https://api.github.com"
MAX_PAGES = 10  # 1000 hits; a backstop, not an expected limit
MAX_ATTEMPTS = 3
SEARCH_PAUSE = 2.0  # Code Search allows 30 req/min

# PEP 503 name normalization: `agentic_web_extraction>=0.1` in a requirements
# file is the same distribution as `agentic-web-extraction>=0.1`.
_PKG = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", PACKAGE))
_MOD = re.escape(MODULE)

QUERIES = (f'org:{ORG} "{PACKAGE}"', f'org:{ORG} "{MODULE}"')

# Names the package under a dependency-ish token boundary.
DEP_RE = re.compile(rf"(?<![\w.-]){_PKG}(?![\w.-])", re.IGNORECASE)
# `name = "agentic-web-extraction"` in a *forked* pyproject is a self-declaration,
# not a dependency.
NAME_DECL_RE = re.compile(r"^\s*name\s*[=:]")
# `agentic-web-extraction[extra] >= 0.1.0`
SPEC_VERSION_RE = re.compile(
    rf"{_PKG}\s*(?:\[[^\]]*\])?\s*(?:===|==|~=|>=|<=|>|<|=)\s*v?([0-9][A-Za-z0-9._]*)",
    re.IGNORECASE,
)
# `... @ git+https://github.com/ORG/agentic-web-extraction@v0.1.0`
GITREF_RE = re.compile(
    rf"github\.com[:/]{re.escape(ORG)}/{_PKG}(?:\.git)?@v?([A-Za-z0-9._]+)",
    re.IGNORECASE,
)
# `agentic-web-extraction = { git = "…", rev = "8e8f674…" }` — the uv / poetry
# source-table form. Reported as the ref, not as "unpinned": a `rev` is
# immutable, it just isn't a release, so it still renders orange.
SOURCE_REF_RE = re.compile(
    rf"{_PKG}\s*=\s*\{{[^}}]*?\b(?:rev|tag|branch)\s*=\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
# uv.lock / poetry.lock pin the exact resolved version on the next line.
LOCK_VERSION_RE = re.compile(
    rf'name\s*=\s*"{_PKG}"\s*\n\s*version\s*=\s*"([^"]+)"', re.IGNORECASE
)
# Necessary but not sufficient: reported, never counted.
WEAK_RE = re.compile(rf"(?:\bimport\s+{_MOD}\b|\bfrom\s+{_MOD}[\s.]|\bawe\s+extract\b)")
PINNED_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        "uv.lock",
        "poetry.lock",
        "environment.yml",
        "environment.yaml",
        "pixi.toml",
        "constraints.txt",
    }
)


class ScanError(Exception):
    """Any condition that must abort the run and leave the README untouched."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    print(message, file=sys.stderr)


def api(path: str, token: str) -> dict | list:
    """GET one GitHub API path. Raises ScanError on anything but success."""
    url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{PACKAGE}-adopter-scan",
        },
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            retryable = error.code in (403, 429) and attempt < MAX_ATTEMPTS
            if not retryable:
                body = error.read().decode(errors="replace")[:400]
                raise ScanError(f"GET {url} -> HTTP {error.code}: {body}") from error
            delay = float(error.headers.get("Retry-After") or 2**attempt)
            log(f"  rate limited on {url} (attempt {attempt}); sleeping {delay:.0f}s")
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt >= MAX_ATTEMPTS:
                raise ScanError(f"GET {url} failed: {error.reason}") from error
            time.sleep(2**attempt)
    raise ScanError(f"GET {url} exhausted {MAX_ATTEMPTS} attempts")


# --------------------------------------------------------------------------- #
# Preflight — prove visibility before searching
# --------------------------------------------------------------------------- #


def preflight(token: str) -> None:
    """Refuse a blind token *before* any search.

    An under-scoped token (e.g. a fine-grained PAT whose resource owner is a
    personal account) authenticates fine, answers HTTP 200 to everything, and
    returns an empty result set from every code search. Nothing raises, so the
    run would render "none yet" over real adopters. The search itself cannot
    tell blindness from absence — the org object can.
    """
    org = api(f"orgs/{ORG}", token)
    if not isinstance(org, dict):
        raise ScanError(f"orgs/{ORG} returned {type(org).__name__}, expected object")

    total = org.get("total_private_repos")
    if total is None:
        raise ScanError(
            f"token cannot see private repos in {ORG} (no `total_private_repos` on the "
            "org object). Use a classic PAT with `repo` scope, or a fine-grained PAT "
            "whose resource owner is the org — not a personal account."
        )
    if total and not api(f"orgs/{ORG}/repos?type=private&per_page=1", token):
        raise ScanError(
            f"token counts {total} private repos in {ORG} but cannot list them; "
            "a code search would silently return nothing."
        )
    log(f"preflight ok: {ORG} has {total} private repos, token can list them")


# --------------------------------------------------------------------------- #
# Search + fetch
# --------------------------------------------------------------------------- #


def search_code(query: str, token: str) -> dict[str, set[str]]:
    """Code Search for one query -> {repo full_name: {paths}}, minus SELF.

    Code Search indexes default branches only, so an adopter wired up on an
    unmerged branch is invisible by design.
    """
    hits: dict[str, set[str]] = {}
    for page in range(1, MAX_PAGES + 1):
        params = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
        payload = api(f"search/code?{params}", token)
        if not isinstance(payload, dict):
            raise ScanError("search/code returned a non-object payload")
        items = payload.get("items") or []
        for item in items:
            repo = item["repository"]["full_name"]
            if repo.casefold() == SELF.casefold():
                continue
            hits.setdefault(repo, set()).add(item["path"])
        if len(items) < 100 or page * 100 >= int(payload.get("total_count") or 0):
            break
        time.sleep(SEARCH_PAUSE)
    log(f"search {query!r}: {len(hits)} candidate repos")
    return hits


def fetch_text(repo: str, path: str, token: str) -> str:
    """Read one file from a repo's default branch as text."""
    encoded = urllib.parse.quote(path)
    payload = api(f"repos/{repo}/contents/{encoded}", token)
    if not isinstance(payload, dict):
        raise ScanError(f"contents/{path} in {repo} is not a file")
    if payload.get("encoding") != "base64" or not payload.get("content"):
        # Files over 1 MB come back with an empty body; the blob API still serves
        # them. uv.lock in a large repo hits this routinely.
        blob = api(f"repos/{repo}/git/blobs/{payload['sha']}", token)
        if not isinstance(blob, dict) or blob.get("encoding") != "base64":
            raise ScanError(f"cannot decode {repo}/{path}")
        payload = blob
    return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")


def strip_comments(text: str) -> str:
    """Drop whole-line `#` comments and trailing ` #` comments, keeping lines.

    A heuristic for TOML / requirements / Python, not a parser — which is fine,
    because dependency specifiers don't carry `#` inside quoted values in
    practice. It exists because a repo that names this package in a *comment*
    ("we evaluated agentic-web-extraction, went with X") would otherwise be
    reported as a deployment that isn't there.
    """
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            out.append("")
            continue
        head, sep, _ = line.partition(" #")
        out.append(head if sep else line)
    return "\n".join(out)


def is_manifest(path: str) -> bool:
    """Only a dependency manifest can prove adoption."""
    base = path.rsplit("/", 1)[-1].casefold()
    if base in MANIFEST_NAMES:
        return True
    return base.endswith(".txt") and "requirement" in base


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Adopter:
    repo: str
    versions: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[-1]

    @property
    def version_label(self) -> str:
        return ", ".join(self.versions) if self.versions else "unpinned"

    @property
    def pinned(self) -> bool:
        return bool(self.versions) and all(
            PINNED_RE.match(v.lstrip("vV")) for v in self.versions
        )

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}"


def short_ref(ref: str) -> str:
    """A full commit SHA is 40 badge characters of noise; 7 is the git default."""
    return ref[:7] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref


def classify(
    repo: str, paths: set[str], token: str
) -> tuple[Adopter | None, tuple[str, ...]]:
    """Decide whether `repo` counts, and collect its weak (config-only) paths."""
    versions: set[str] = set()
    evidence: set[str] = set()
    weak: set[str] = set()

    for path in sorted(paths):
        text = strip_comments(fetch_text(repo, path, token))

        if is_manifest(path):
            declared = False
            for line in text.splitlines():
                if not DEP_RE.search(line) or NAME_DECL_RE.match(line):
                    continue
                declared = True
                match = (
                    SPEC_VERSION_RE.search(line)
                    or GITREF_RE.search(line)
                    or SOURCE_REF_RE.search(line)
                )
                if match:
                    versions.add(short_ref(match.group(1)))
            for match in LOCK_VERSION_RE.finditer(text):
                declared = True
                versions.add(match.group(1))
            if declared:
                evidence.add(path)
                continue

        if WEAK_RE.search(text):
            weak.add(path)

    adopter = (
        Adopter(repo, tuple(sorted(versions)), tuple(sorted(evidence)))
        if evidence
        else None
    )
    return adopter, tuple(sorted(weak))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def shield_escape(text: str) -> str:
    """`-` and `_` are shields.io markup, so every literal one must be doubled.

    `-` separates label from message and `_` renders as a space, so `my-repo`
    must be written `my--repo` or the badge renders mangled and truncated.
    """
    return text.replace("_", "__").replace("-", "--").replace(" ", "_")


def render_badges(adopters: list[Adopter]) -> str:
    """Static shields URLs — the adopters are private and shields is
    unauthenticated, so its dynamic-JSON endpoint cannot read anything we commit.
    """
    if not adopters:
        return "![used by](https://img.shields.io/badge/used__by-none_yet-lightgrey)"
    lines = []
    for adopter in adopters:
        label = shield_escape(adopter.name)
        message = shield_escape(adopter.version_label)
        color = "brightgreen" if adopter.pinned else "orange"
        lines.append(
            f"[![{adopter.repo}](https://img.shields.io/badge/{label}-{message}-{color})]"
            f"({adopter.url})"
        )
    return "\n".join(lines)


def replace_block(readme: str, body: str) -> str:
    start = readme.find(MARKER_START)
    end = readme.find(MARKER_END)
    if start < 0 or end < 0 or end < start:
        raise ScanError(
            f"README.md is missing the {MARKER_START} / {MARKER_END} marker block"
        )
    return readme[: start + len(MARKER_START)] + "\n" + body + "\n" + readme[end:]


def render_table(adopters: list[Adopter]) -> str:
    if not adopters:
        return "_No org repo declares a dependency on this package yet._"
    rows = ["| Repo | Version | Pinned | Evidence |", "| --- | --- | --- | --- |"]
    for adopter in adopters:
        rows.append(
            f"| [{adopter.repo}]({adopter.url}) | {adopter.version_label} "
            f"| {'yes' if adopter.pinned else 'no'} | {', '.join(f'`{p}`' for p in adopter.evidence)} |"
        )
    return "\n".join(rows)


def render_config_only(config_only: dict[str, tuple[str, ...]]) -> str:
    if not config_only:
        return ""
    heading = (
        f"Uses the module but declares no dependency "
        f"({len(config_only)} repos — not counted)"
    )
    lines = [f"<details><summary>{heading}</summary>", ""]
    for repo in sorted(config_only, key=str.casefold):
        paths = ", ".join(f"`{p}`" for p in config_only[repo])
        lines.append(f"- [{repo}](https://github.com/{repo}) — {paths}")
    lines += ["", "</details>"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def emit_outputs(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in values.items())


def emit_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def scan(token: str) -> tuple[list[Adopter], dict[str, tuple[str, ...]]]:
    preflight(token)

    candidates: dict[str, set[str]] = {}
    for query in QUERIES:
        for repo, paths in search_code(query, token).items():
            candidates.setdefault(repo, set()).update(paths)

    adopters: list[Adopter] = []
    config_only: dict[str, tuple[str, ...]] = {}
    for repo in sorted(candidates, key=str.casefold):
        adopter, weak = classify(repo, candidates[repo], token)
        if adopter:
            adopters.append(adopter)
            log(f"  {repo}: adopter ({adopter.version_label})")
        elif weak:
            config_only[repo] = weak
            log(f"  {repo}: module use only, not counted")
        else:
            log(f"  {repo}: mention only, not counted")

    # Pinned first, then repo name casefolded. Stable order => unchanged org
    # produces a byte-identical block => no weekly commit noise.
    adopters.sort(key=lambda a: (not a.pinned, a.repo.casefold()))
    return adopters, config_only


def main(argv: list[str]) -> int:
    write = "--write" in argv[1:]
    unknown = [a for a in argv[1:] if a != "--write"]
    if unknown:
        log(f"unknown arguments: {' '.join(unknown)}")
        return 2

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        log(
            "GH_TOKEN is not set. This scan must read private org repos; there is no "
            "public-only fallback, because a silent zero is indistinguishable from "
            "real disadoption."
        )
        return 1

    try:
        adopters, config_only = scan(token)
        block = render_badges(adopters)
        original = README.read_text(encoding="utf-8")
        updated = replace_block(original, block)
    except ScanError as error:
        log(f"error: {error}")
        return 1

    changed = updated != original
    table = render_table(adopters)
    details = render_config_only(config_only)

    print(table)
    print()
    print(block)
    print()
    if details:
        print(details)
        print()
    if write and changed:
        README.write_text(updated, encoding="utf-8")
        print(f"README.md updated ({len(adopters)} adopters).")
    elif write:
        print("README.md already current; nothing written.")
    else:
        print(
            f"dry run: README.md would {'change' if changed else 'not change'} "
            f"({len(adopters)} adopters). Pass --write to apply."
        )

    emit_outputs(changed="true" if changed else "false", count=str(len(adopters)))
    emit_summary(
        "\n".join(
            part
            for part in (
                f"## Adoption scan — {len(adopters)} deploying repos",
                "",
                table,
                "",
                details,
            )
            if part is not None
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
