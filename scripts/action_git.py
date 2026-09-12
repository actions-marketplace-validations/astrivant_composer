"""
Detect watched changes and commit generated output to a GitHub branch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def git(*args: str) -> str:
    """
    Run Git with literal arguments and propagate failures.

    Args:
        *args (str): Git command arguments.

    Returns:
        str: Captured standard output.
    """
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def repository_path(path: Path) -> str:
    """
    Require a concrete output or watch path inside the checkout.

    Args:
        path (Path): Configured path.

    Returns:
        str: Repository-relative path.
    """
    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root or ".git" in resolved.parts:
        raise SystemExit(f"Path must be inside the checkout and outside .git: {path}")
    return resolved.relative_to(root).as_posix()


def watched_changes(directory: str) -> bool:
    """
    Compare the full pushed range, including deletions and new branches.

    Args:
        directory (str): Literal directory to watch.

    Returns:
        bool: Whether compilation should run; non-push events run explicitly.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        return True
    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    watched = "" if Path(directory).resolve() == root else repository_path(Path(directory))
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    before, after = str(event.get("before", "")), str(event.get("after", ""))
    if not all(re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in (before, after)):
        raise SystemExit("Push event must contain valid before and after commit IDs")
    if set(after) == {"0"}:
        return False
    if git("rev-parse", "HEAD") != after:
        raise SystemExit("Checkout must match the triggering commit")
    pathspec = ":(top,literal)" + watched
    if set(before) == {"0"}:
        return bool(git("ls-tree", "-r", "--name-only", after, "--", pathspec))
    # fetch-depth: 0 is required; missing history must not silently skip generation.
    return bool(git("diff", "--name-only", "--no-renames", before, after, "--", pathspec))


def commit_branch() -> str:
    """
    Resolve an authorized branch event and verify its checked-out revision.

    Returns:
        str: Full destination branch ref.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    ref = os.environ.get("GITHUB_REF", "")
    if event not in {"push", "workflow_dispatch"} or not ref.startswith("refs/heads/"):
        raise SystemExit(
            "commit: true requires a branch push or workflow_dispatch; use commit: false"
        )
    git("check-ref-format", ref)
    if git("rev-parse", "HEAD") != os.environ.get("GITHUB_SHA"):
        raise SystemExit("Checkout must match GITHUB_SHA before committing generated output")
    return ref


def commit_outputs(output: Path, report: str, branch: str) -> bool:
    """
    Commit only generated paths and push without overwriting concurrent changes.

    Args:
        output (Path): Generated Compose file.
        report (str): Optional generated report path.
        branch (str): Destination branch ref.

    Returns:
        bool: Whether a changed output was committed and pushed.
    """
    paths = [repository_path(output)]
    support = output.parent / ".compose-generated"
    if support.exists():
        paths.append(repository_path(support))
    if report:
        paths.append(repository_path(Path(report)))
    pathspecs = [":(top,literal)" + path for path in paths]
    git("add", "--", *pathspecs)
    # Compare generated content with the committed version, not timestamps or input changes.
    if not git("diff", "--cached", "--name-only", "HEAD", "--", *pathspecs):
        return False
    git(
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "--only",
        "-m",
        "chore: regenerate Docker Compose",
        "--",
        *pathspecs,
    )
    git("push", "origin", f"HEAD:{branch}")
    return True
