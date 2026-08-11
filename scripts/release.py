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
"""

import os
import re
import subprocess
import sys
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
# uv records the project version in its lockfile too; if we bump pyproject
# without refreshing this, any `uv sync --locked` rejects the lockfile.
LOCKFILE = Path("uv.lock")
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
        write_version(new_version)

        # Keep uv.lock's recorded project version in sync with pyproject, or a
        # `uv sync --locked` (CI, or a consumer pinning this repo) fails.
        console.print("🔒 [blue]Refreshing uv.lock...[/blue]")
        run(["uv", "lock"])

        # Phase 3: Commit and Tag
        console.print(f"📦 [blue]Creating tag {tag_name}...[/blue]")
        run(["git", "add", str(PYPROJECT), str(LOCKFILE)])
        run(["git", "commit", "-m", f"chore: release {tag_name}"], capture=False)
        run(["git", "tag", "-a", tag_name, "-m", tag_name], capture=False)

        # Phase 4: Push (atomic — branch and tag succeed or fail together)
        console.print(f"⬆️  [blue]Pushing to {remote}/{branch}...[/blue]")
        try:
            run(["git", "push", "--atomic", remote, branch, tag_name], capture=False)
        except ReleaseError:
            console.print(
                "[yellow]⚠️ Push failed — rolling back local commit and tag...[/yellow]"
            )
            best_effort(["git", "tag", "-d", tag_name], "Deleting local tag")
            best_effort(
                ["git", "reset", "--mixed", "HEAD~1"], "Reverting release commit"
            )
            raise

        console.print(
            f"\n[bold green]✨ Successfully released {tag_name}![/bold green]"
        )
        console.print(
            f"[green]Install this release with: "
            f"uv add 'agentic-web-extraction @ "
            f"git+https://github.com/UW-Madison-DSI/agentic-web-extraction@{tag_name}'[/green]"
        )

    except ReleaseError:
        sys.exit(1)


if __name__ == "__main__":
    app()
