# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "typer>=0.25",
#     "rich>=14",
# ]
# ///
"""Safe release manager for agentic-web-extraction.

Usage: uv run scripts/release.py [major|minor|patch]

Uses the version in pyproject.toml as the base, bumps the chosen component
(major/minor/patch), writes it back, then commits and pushes a matching
`vX.Y.Z` git tag. The pyproject version and the git tag are always kept in
sync — the tag is the release, so nothing else may set the version.

Release notes come from CHANGELOG.md: write them under `## Unreleased` as you
work, and this script renames that heading to `## vX.Y.Z — <date>` inside the
same commit as the version bump (so the tag carries its own notes), then attaches
the section to the pushed tag as a GitHub Release. An empty or missing
`## Unreleased` section aborts before anything is touched — a release with no
notes is a release nobody can read later.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

# rich is a PEP 723 script-level dep (see header), not a project dep — this
# script runs under `uv run`, which builds its own env from that header.
import typer
from rich.console import Console

console = Console()
app = typer.Typer(help="Safe release manager for agentic-web-extraction.")

DEFAULT_BRANCH = "main"
PYPROJECT = Path("pyproject.toml")
# uv records the project version in its lockfile too, so a tracked lockfile has
# to be refreshed alongside the bump or every `uv sync --locked` rejects it.
# This repo currently gitignores uv.lock, hence `lockfile_is_tracked` — the
# release must not depend on which choice the repo made.
LOCKFILE = Path("uv.lock")
# Release notes. Optional in the same sense as the lockfile -- a repo without a
# tracked CHANGELOG.md still releases -- but when it exists it must have something
# to say, or the release is silently undocumented.
CHANGELOG = Path("CHANGELOG.md")
UNRELEASED_HEADING = "## Unreleased"
# The `## Unreleased` heading plus its body, up to the next `## ` heading or EOF.
UNRELEASED_RE = re.compile(
    r"^##[ \t]+Unreleased[ \t]*$(?P<body>.*?)(?=^##[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)
# Used only when pyproject.toml has no version at all (no previous release).
INITIAL_VERSION = (0, 1, 0)
VERSION_RE = re.compile(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.MULTILINE)


class Increment(StrEnum):
    major = "major"
    minor = "minor"
    patch = "patch"


class ReleaseError(Exception):
    pass


def run(cmd: list[str], capture: bool = True) -> str:
    """Run a command and return output, raising ReleaseError on failure."""
    try:
        result = subprocess.run(cmd, capture_output=capture, text=True, check=True)
        return result.stdout.strip() if capture else ""
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error running {' '.join(cmd)}[/bold red]")
        if e.stderr:
            console.print(f"[red]{e.stderr.strip()}[/red]")
        raise ReleaseError(f"Command failed: {' '.join(cmd)}") from e


def best_effort(cmd: list[str], description: str) -> None:
    """Run a rollback command, surfacing stderr on failure but never raising."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        console.print(f"[yellow]⚠️ {description} failed:[/yellow]")
        if result.stderr:
            console.print(f"[yellow]{result.stderr.strip()}[/yellow]")


def is_tracked(path: Path) -> bool:
    """True when `path` is under version control (not gitignored/absent).

    An untracked file can't be part of the release commit, and `git add` on an
    ignored path is a hard error — so anything untracked is skipped entirely
    rather than gambling on the repo's choice. (`uv.lock` is gitignored here;
    other repos using this script track it.)
    """
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def read_unreleased(text: str) -> str:
    """Return the body of CHANGELOG.md's `## Unreleased` section.

    Raises when the section is missing or empty. Deliberately a hard failure, and
    checked before anything is written: the whole point of stamping notes into the
    release commit is that vX.Y.Z carries its own, and a release cut with an empty
    section would leave the tag undocumented with nothing to point at afterwards.
    """
    match = UNRELEASED_RE.search(text)
    if match is None:
        raise ReleaseError(
            f"{CHANGELOG} has no '{UNRELEASED_HEADING}' section — add one and write "
            f"this release's notes under it."
        )
    body = match.group("body").strip()
    if not body:
        raise ReleaseError(
            f"{CHANGELOG}'s '{UNRELEASED_HEADING}' section is empty — write this "
            f"release's notes under it before releasing."
        )
    return body


def stamp_unreleased(text: str, version_str: str, today: str) -> str:
    """Rename `## Unreleased` to `## vX.Y.Z — <date>`, leaving a fresh empty one.

    The empty heading stays at the top so the next author has somewhere obvious to
    write, and so `read_unreleased` fails on emptiness rather than on absence.
    """
    match = UNRELEASED_RE.search(text)
    if match is None:  # pragma: no cover - read_unreleased runs first
        raise ReleaseError(f"{CHANGELOG} has no '{UNRELEASED_HEADING}' section")
    stamped = (
        f"{UNRELEASED_HEADING}\n\n"
        f"## v{version_str} — {today}\n\n"
        f"{match.group('body').strip()}\n\n"
    )
    return text[: match.start()] + stamped + text[match.end() :]


def gh_ready() -> bool:
    """Whether `gh` is installed and authenticated.

    A machine without `gh` is not a broken release — the tag is the release — so
    this reports a skip rather than a failure.
    """
    if shutil.which("gh") is None:
        console.print(
            "[yellow]ℹ️ `gh` is not installed — skipping the GitHub Release.[/yellow]"
        )
        return False
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        console.print(
            "[yellow]ℹ️ `gh` is not authenticated — skipping the GitHub Release.[/yellow]"
        )
        return False
    return True


def publish_github_release(tag_name: str, notes: str) -> bool:
    """Attach `notes` to the pushed tag as a GitHub Release.

    Runs only AFTER the atomic push, and rolls nothing back on failure: the tag is
    public by then, so it cannot be un-released, and re-running the script would cut
    a whole new version rather than retry this step. So a failure here reports the
    one-line manual fix instead.
    """
    if not gh_ready():
        return True
    console.print(f"📝 [blue]Publishing release notes for {tag_name}...[/blue]")
    result = subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag_name,
            "--title",
            tag_name,
            # Refuse to invent a tag: this runs after the atomic push, so the tag
            # must already be on the remote. Without it a mistake here would create
            # a release pointing at a tag nobody can check out.
            "--verify-tag",
            "--notes-file",
            "-",
        ],
        input=notes,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        console.print(
            f"[bold yellow]⚠️ {tag_name} is pushed and IS released, but publishing "
            f"its notes failed:[/bold yellow]"
        )
        if result.stderr:
            console.print(f"[yellow]{result.stderr.strip()}[/yellow]")
        console.print(
            "[bold yellow]Do NOT re-run this script — it would cut another "
            "version.[/bold yellow]"
        )
        console.print(
            f"[yellow]Publish the notes by hand instead: copy the "
            f"'## {tag_name}' section out of {CHANGELOG} and run\n"
            f"  gh release create {tag_name} --title {tag_name} "
            f"--notes-file -[/yellow]"
        )
        return False
    console.print(f"[green]✅ Published release notes for {tag_name}.[/green]")
    return True


def get_repo_root() -> Path:
    """Return the absolute path of the git repo root."""
    return Path(run(["git", "rev-parse", "--show-toplevel"]))


def get_push_target() -> tuple[str, str]:
    """Return (remote, branch) for pushing, derived from git config."""
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    try:
        remote = run(["git", "config", f"branch.{branch}.remote"])
    except ReleaseError:
        remote = "origin"
        console.print(
            "[yellow]⚠️ No tracking remote configured, defaulting to 'origin'.[/yellow]"
        )
    return remote, branch


def verify_git_state(remote: str) -> None:
    """Ensure the repo is clean and synced with remote."""
    console.print("🔍 [blue]Checking git status...[/blue]")

    # 1. Check for uncommitted changes (staged or unstaged)
    status = run(["git", "status", "--porcelain"])
    if status:
        console.print("[bold red]❌ Working directory is not clean![/bold red]")
        console.print("Please commit or stash your changes before releasing:")
        console.print(f"[yellow]{status}[/yellow]")
        raise ReleaseError("Working directory is not clean")

    # 2. Check if local is synced with remote
    run(["git", "fetch", remote])
    local_hash = run(["git", "rev-parse", "HEAD"])
    try:
        remote_hash = run(["git", "rev-parse", "@{u}"])
    except ReleaseError:
        console.print(
            "[yellow]⚠️ No upstream branch found. Skipping remote sync check.[/yellow]"
        )
        return

    if local_hash != remote_hash:
        ahead = run(["git", "rev-list", "HEAD", "--not", "@{u}", "--count"])
        behind = run(["git", "rev-list", "@{u}", "--not", "HEAD", "--count"])

        if int(behind) > 0:
            console.print(
                f"[bold red]❌ You are behind the remote by {behind} commits.[/bold red] Pull first."
            )
            raise ReleaseError(f"Behind remote by {behind} commits")
        if int(ahead) > 0:
            console.print(
                f"[bold red]❌ You have {ahead} unpushed commits.[/bold red] Push them first."
            )
            raise ReleaseError(f"Ahead of remote by {ahead} commits")

    console.print("[green]✅ Git state is clean and synced.[/green]")


def read_version() -> tuple[int, int, int]:
    """Return the current (major, minor, patch) from pyproject.toml.

    Falls back to INITIAL_VERSION when no version line is present, so a first
    release with no previous version starts at v0.1.0.
    """
    if not PYPROJECT.exists():
        raise ReleaseError(f"{PYPROJECT} not found")
    match = VERSION_RE.search(PYPROJECT.read_text())
    if not match:
        console.print(
            f"[yellow]⚠️ No version found in {PYPROJECT}, starting at "
            f"v{'.'.join(map(str, INITIAL_VERSION))}.[/yellow]"
        )
        return INITIAL_VERSION
    return int(match.group(2)), int(match.group(3)), int(match.group(4))


def bump(version: tuple[int, int, int], increment: Increment) -> tuple[int, int, int]:
    """Return the next version after bumping the chosen component."""
    major, minor, patch = version
    if increment is Increment.major:
        return major + 1, 0, 0
    if increment is Increment.minor:
        return major, minor + 1, 0
    return major, minor, patch + 1


def write_version(version: tuple[int, int, int]) -> None:
    """Write the new version back into pyproject.toml, preserving formatting."""
    text = PYPROJECT.read_text()
    new_line = rf"\g<1>{version[0]}.{version[1]}.{version[2]}\g<5>"
    if VERSION_RE.search(text):
        text = VERSION_RE.sub(new_line, text, count=1)
    else:
        # No existing version line: insert one right after the [project] header.
        text = re.sub(
            r"^\[project\]\s*$",
            f'[project]\nversion = "{version[0]}.{version[1]}.{version[2]}"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    PYPROJECT.write_text(text)


@app.command()
def main(
    increment: Annotated[
        Increment, typer.Argument(help="Version component to bump")
    ] = Increment.patch,
) -> None:
    try:
        # Phase 1: Guards
        remote, branch = get_push_target()
        if branch != DEFAULT_BRANCH:
            console.print(
                f"[bold red]❌ Refusing to release from non-{DEFAULT_BRANCH} branch: {branch}[/bold red]"
            )
            raise ReleaseError(f"Releases must run from {DEFAULT_BRANCH}, not {branch}")

        # Operate from the repo root so all relative paths behave the same
        # regardless of where the user invoked this script.
        os.chdir(get_repo_root())

        verify_git_state(remote)

        # Phase 2: Bump (pyproject version is the base)
        current = read_version()
        new_version = bump(current, increment)
        version_str = ".".join(map(str, new_version))
        tag_name = f"v{version_str}"
        console.print(
            f"🚀 [blue]Bumping {increment.value}: "
            f"{'.'.join(map(str, current))} → {version_str}[/blue]"
        )
        # Read and validate the release notes BEFORE touching anything: a missing or
        # empty `## Unreleased` section must abort with the tree exactly as it was.
        original_changelog: str | None = None
        release_notes = ""
        if CHANGELOG.exists() and is_tracked(CHANGELOG):
            original_changelog = CHANGELOG.read_text()
            release_notes = read_unreleased(original_changelog)
        else:
            console.print(
                f"[yellow]ℹ️ No tracked {CHANGELOG} — releasing without notes.[/yellow]"
            )

        # Everything from here on is undone if any step fails: a bumped version
        # left on disk would silently become the base of the next run, so the
        # skipped number could never be released.
        original_pyproject = PYPROJECT.read_text()
        tracked_lock = is_tracked(LOCKFILE)
        committed = False
        tagged = False
        write_version(new_version)
        if original_changelog is not None:
            CHANGELOG.write_text(
                stamp_unreleased(
                    original_changelog, version_str, date.today().isoformat()
                )
            )

        try:
            paths = [str(PYPROJECT)]
            if original_changelog is not None:
                # Same commit as the bump, so the tag carries its own notes.
                paths.append(str(CHANGELOG))
            if tracked_lock:
                # Keep the lockfile's recorded project version in sync with
                # pyproject, or a `uv sync --locked` fails on the release.
                console.print("🔒 [blue]Refreshing uv.lock...[/blue]")
                run(["uv", "lock"])
                paths.append(str(LOCKFILE))
            else:
                console.print(
                    f"[yellow]ℹ️ {LOCKFILE} is not tracked — "
                    f"leaving it out of the release.[/yellow]"
                )

            # Phase 3: Commit and Tag
            console.print(f"📦 [blue]Creating tag {tag_name}...[/blue]")
            run(["git", "add", *paths])
            run(["git", "commit", "-m", f"chore: release {tag_name}"], capture=False)
            committed = True
            run(["git", "tag", "-a", tag_name, "-m", tag_name], capture=False)
            tagged = True

            # Phase 4: Push (atomic — branch and tag succeed or fail together)
            console.print(f"⬆️  [blue]Pushing to {remote}/{branch}...[/blue]")
            run(["git", "push", "--atomic", remote, branch, tag_name], capture=False)
        except ReleaseError:
            console.print("[yellow]⚠️ Release failed — rolling back...[/yellow]")
            if tagged:
                best_effort(["git", "tag", "-d", tag_name], "Deleting local tag")
            if committed:
                best_effort(
                    ["git", "reset", "--mixed", "HEAD~1"], "Reverting release commit"
                )
            PYPROJECT.write_text(original_pyproject)
            if original_changelog is not None:
                CHANGELOG.write_text(original_changelog)
            if tracked_lock:
                best_effort(["git", "restore", str(LOCKFILE)], "Restoring uv.lock")
            raise

        console.print(
            f"\n[bold green]✨ Successfully released {tag_name}![/bold green]"
        )
        console.print(
            f"[green]Install this release with: "
            f"uv add 'agentic-web-extraction @ "
            f"git+https://github.com/UW-Madison-DSI/agentic-web-extraction@{tag_name}'[/green]"
        )

        # Last, and outside the rollback: the release already exists.
        if original_changelog is not None and not publish_github_release(
            tag_name, release_notes
        ):
            sys.exit(1)

    except ReleaseError:
        sys.exit(1)


if __name__ == "__main__":
    app()
