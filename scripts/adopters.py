# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Scan the org for repos that actually depend on this package, render badges.

GitHub Packages exposes no pull or download metric, and PyPI download stats say
nothing about *which* org repos adopted us, so the signal has to come from the
org's own source.

It does NOT come from the Code Search API. Code search only had 23 of this org's
55 Python repos indexed — repos containing `requires-python` returned zero hits
for it while the token had full private visibility — so a search-driven count is
a floor wearing the costume of a total. Instead, every repo is enumerated from
`GET /orgs/{org}/repos` and its file tree read directly, which is complete by
construction and indifferent to indexing.

Counting rule: a repo counts as an adopter only when a *dependency manifest*
declares this distribution on a non-comment line. An import of the module, or an
`awe extract` invocation, is necessary but not sufficient — a vendored copy, a
sibling checkout, or a notebook that pip-installs from a branch all produce it,
and none of them pin a version. Those are still found via code search and
reported in the job summary, never counted, so the index gap only understates a
gap that was already labelled as one.

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
from dataclasses import dataclass, field
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
MAX_ATTEMPTS = 4  # tree calls on multi-gigabyte repos need more than one go
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
# uv.lock / poetry.lock record the resolved version on the next line, and often a
# `source` after it. The source matters: for a git source the `version` is just
# whatever our own pyproject said at that commit, so treating it as a release pin
# would paint a branch-tracking repo green.
LOCK_BLOCK_RE = re.compile(
    rf'name\s*=\s*"{_PKG}"\s*\nversion\s*=\s*"([^"]+)"'
    rf"(?:\s*\nsource\s*=\s*\{{([^}}]*)\}})?",
    re.IGNORECASE,
)
# `{ git = "https://…/repo?branch=batch-extraction#8246a1e…" }`
GIT_SOURCE_REF_RE = re.compile(
    r"[?&]tag=([^#&\"\s]+)|[?&]branch=([^#&\"\s]+)|#([0-9a-f]{7,40})", re.IGNORECASE
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

VENDOR_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "dist-packages",
        "node_modules",
        "site-packages",
        "venv",
        "vendor",
    }
)


class ScanError(Exception):
    """Any condition that must abort the run and leave the README untouched."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    print(message, file=sys.stderr)


def api(path: str, token: str, tolerate: tuple[int, ...] = ()) -> dict | list | None:
    """GET one GitHub API path.

    Raises ScanError on anything but success, except for status codes in
    `tolerate`, which return None — used for the handful of conditions that are
    genuinely "nothing here" rather than "the scan is broken" (an empty repo has
    no tree).
    """
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
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            if error.code in tolerate:
                return None
            # A 403 is both "rate limited" and "forbidden"; only the former is
            # worth retrying, and only the body distinguishes them. A 5xx is
            # transient — a recursive tree call on a multi-gigabyte repo flakes
            # and then succeeds on the next attempt.
            rate_limited = error.code == 429 or (
                error.code == 403 and "rate limit" in body.lower()
            )
            if not (rate_limited or error.code >= 500) or attempt >= MAX_ATTEMPTS:
                raise ScanError(
                    f"GET {url} -> HTTP {error.code}: {body[:400]}"
                ) from error
            delay = float(error.headers.get("Retry-After") or 2**attempt)
            log(
                f"  HTTP {error.code} on {url} (attempt {attempt}); sleeping {delay:.0f}s"
            )
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt >= MAX_ATTEMPTS:
                raise ScanError(f"GET {url} failed: {error.reason}") from error
            time.sleep(2**attempt)
        except json.JSONDecodeError as error:
            # A response body cut off mid-stream; same flakiness as the 5xx above.
            if attempt >= MAX_ATTEMPTS:
                raise ScanError(
                    f"GET {url} returned malformed JSON: {error}"
                ) from error
            log(f"  truncated body from {url} (attempt {attempt}); retrying")
            time.sleep(2**attempt)
    raise ScanError(f"GET {url} exhausted {MAX_ATTEMPTS} attempts")


def api_paged(path: str, token: str) -> list:
    """GET every page of a list endpoint."""
    joiner = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, MAX_PAGES + 1):
        payload = api(f"{path}{joiner}per_page=100&page={page}", token)
        if not isinstance(payload, list):
            raise ScanError(f"{path} page {page} did not return a list")
        items.extend(payload)
        if len(payload) < 100:
            return items
    # Silently keeping the first MAX_PAGES pages would drop repos from the scan
    # and report the remainder as the whole org.
    raise ScanError(
        f"{path} still had pages after {MAX_PAGES}; raise MAX_PAGES rather than "
        "scan part of the org"
    )


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
# Discovery — enumerate repos and their manifests without the search index
# --------------------------------------------------------------------------- #


def list_repos(token: str) -> list[dict]:
    """Every repo in the org. Complete by construction, unlike a code search."""
    repos = api_paged(f"orgs/{ORG}/repos?type=all", token)
    keep = [r for r in repos if r["full_name"].casefold() != SELF.casefold()]
    log(f"org has {len(repos)} repos ({len(keep)} after excluding self)")
    return sorted(keep, key=lambda r: r["full_name"].casefold())


def list_manifests(repo: dict, token: str) -> tuple[list[str], bool]:
    """Manifest paths in one repo's default branch, via a single tree call.

    Returns (paths, complete). This is the fix for the code-search index: the
    tree is authoritative for what the repo actually contains, and it sees
    manifests in subdirectories, which is how a monorepo declares dependencies.
    """
    branch = repo.get("default_branch")
    if not branch:
        return [], True  # no commits yet
    name = repo["full_name"]
    # 404/409: empty repo or a branch that vanished mid-run — genuinely nothing,
    # not a broken scan.
    payload = api(
        f"repos/{name}/git/trees/{urllib.parse.quote(branch)}?recursive=1",
        token,
        tolerate=(404, 409),
    )
    if payload is None:
        return [], True
    if not isinstance(payload, dict):
        raise ScanError(f"tree for {name} is not an object")
    paths = [
        entry["path"]
        for entry in payload.get("tree", [])
        if entry.get("type") == "blob" and is_manifest(entry["path"])
    ]
    # A truncated tree means files were omitted, so a manifest may have been
    # missed. That is exactly the silent undercount this rewrite exists to kill,
    # so it is not tolerated — see `Coverage`.
    return paths, not payload.get("truncated", False)


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
    """Only a dependency manifest can prove adoption.

    Vendored trees are excluded: a committed `.venv` or `site-packages` contains
    *our own* metadata, which would badge a repo for checking in a virtualenv.
    """
    lowered = path.casefold()
    if any(part in VENDOR_DIRS for part in lowered.split("/")[:-1]):
        return False
    base = lowered.rsplit("/", 1)[-1]
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


@dataclass
class Coverage:
    """What the scan actually looked at.

    `preflight` proves the *token* can see the org; it cannot prove GitHub has
    *indexed* it. Coverage is the same idea one layer down: a number is only
    trustworthy if the scan can say what it examined.
    """

    total: int = 0
    examined: int = 0
    empty: int = 0
    manifests: int = 0
    truncated: list[str] = field(default_factory=list)


def short_ref(ref: str) -> str:
    """A full commit SHA is 40 badge characters of noise; 7 is the git default."""
    return ref[:7] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref


def declares(text: str) -> tuple[bool, set[str]]:
    """Does this comment-stripped manifest declare the package, and at what version?"""
    declared = False
    versions: set[str] = set()
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
    for match in LOCK_BLOCK_RE.finditer(text):
        declared = True
        version, source = match.group(1), match.group(2) or ""
        if "git" in source.lower():
            # Label what it actually tracks. If no ref is discernible, add
            # nothing: an unlabelled adopter renders "unpinned" / orange, which
            # is the honest answer for a floating git source.
            ref = GIT_SOURCE_REF_RE.search(source)
            if ref:
                versions.add(short_ref(next(g for g in ref.groups() if g)))
        else:
            versions.add(short_ref(version))
    return declared, versions


def classify(repo: str, manifests: list[str], token: str) -> Adopter | None:
    """Count `repo` iff one of its manifests declares the package."""
    versions: set[str] = set()
    evidence: set[str] = set()
    for path in sorted(manifests):
        declared, found = declares(strip_comments(fetch_text(repo, path, token)))
        if declared:
            evidence.add(path)
            versions |= found
    if not evidence:
        return None
    return Adopter(repo, tuple(sorted(versions)), tuple(sorted(evidence)))


def find_module_users(
    token: str, counted: set[str]
) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """Weak signal, via code search: repos that use the module but declare nothing.

    Still search-driven, and therefore still limited by the index — but this
    signal is only ever *reported*, never counted, so an index gap here
    understates a known gap rather than the badge count.

    Also cross-checks the tree walk: a manifest hit here for a repo the walk did
    not count would mean the walk has a bug, so it is surfaced, not swallowed.
    """
    hits: dict[str, set[str]] = {}
    for query in QUERIES:
        for repo, paths in search_code(query, token).items():
            hits.setdefault(repo, set()).update(paths)

    weak: dict[str, tuple[str, ...]] = {}
    discrepancies: list[str] = []
    for repo in sorted(hits, key=str.casefold):
        found: set[str] = set()
        for path in sorted(hits[repo]):
            text = strip_comments(fetch_text(repo, path, token))
            if is_manifest(path) and declares(text)[0]:
                if repo.casefold() not in counted:
                    discrepancies.append(
                        f"{repo} declares in `{path}` but was not counted"
                    )
                continue
            if WEAK_RE.search(text):
                found.add(path)
        if found and repo.casefold() not in counted:
            weak[repo] = tuple(sorted(found))
    return weak, discrepancies


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
    """Rewrite only the text between the markers, leaving the rest byte-identical.

    Position is therefore a property of the README, never of this script: the
    block stays wherever the markers are put, and everything outside them —
    including the section heading and prose around it — is preserved exactly.
    Move the markers to move the badges.
    """
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


def render_coverage(coverage: Coverage) -> str:
    return (
        f"Examined **{coverage.examined}/{coverage.total}** org repos "
        f"({coverage.empty} empty, {coverage.manifests} manifests read). "
        "Counting is index-independent — every repo's file tree is read directly, "
        "so an unindexed repo can still be counted."
    )


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


def scan(
    token: str,
) -> tuple[list[Adopter], dict[str, tuple[str, ...]], Coverage, list[str]]:
    preflight(token)

    repos = list_repos(token)
    coverage = Coverage(total=len(repos))
    adopters: list[Adopter] = []

    for repo in repos:
        name = repo["full_name"]
        manifests, complete = list_manifests(repo, token)
        coverage.examined += 1
        if not complete:
            coverage.truncated.append(name)
        if not manifests:
            coverage.empty += 1
            continue
        coverage.manifests += len(manifests)
        adopter = classify(name, manifests, token)
        if adopter:
            adopters.append(adopter)
            log(f"  {name}: adopter ({adopter.version_label})")

    # A truncated tree means manifests were omitted, so the count would be a
    # floor presented as a total — the exact failure this replaced.
    if coverage.truncated:
        raise ScanError(
            "file tree truncated for "
            + ", ".join(coverage.truncated)
            + "; a manifest may have been missed, so the count cannot be trusted"
        )

    counted = {a.repo.casefold() for a in adopters}
    config_only, discrepancies = find_module_users(token, counted)
    for repo in config_only:
        log(f"  {repo}: module use only, not counted")
    for note in discrepancies:
        log(f"  DISCREPANCY: {note}")

    # Pinned first, then repo name casefolded. Stable order => unchanged org
    # produces a byte-identical block => no weekly commit noise.
    adopters.sort(key=lambda a: (not a.pinned, a.repo.casefold()))
    return adopters, config_only, coverage, discrepancies


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
        adopters, config_only, coverage, discrepancies = scan(token)
        block = render_badges(adopters)
        original = README.read_text(encoding="utf-8")
        updated = replace_block(original, block)
    except ScanError as error:
        log(f"error: {error}")
        return 1

    changed = updated != original
    table = render_table(adopters)
    details = render_config_only(config_only)
    coverage_note = render_coverage(coverage)

    print(table)
    print()
    print(coverage_note)
    print()
    print(block)
    print()
    if details:
        print(details)
        print()
    for note in discrepancies:
        print(f"DISCREPANCY: {note}")
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
            (
                f"## Adoption scan — {len(adopters)} deploying repos",
                "",
                coverage_note,
                "",
                table,
                "",
                details,
                "",
                *(f"> **Discrepancy:** {n}" for n in discrepancies),
            )
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
