"""
Pass GitHub Action inputs to the CLI without shell interpolation or word splitting.
"""

from __future__ import annotations

import os
from pathlib import Path

from action_git import commit_branch, commit_outputs, repository_path, watched_changes

from composer.entrypoint import main


def run() -> int:
    """
    Generate Compose and publish its path only after a successful compilation.

    Returns:
        int: Compiler exit status, including drift and input errors.
    """
    check = os.environ.get("COMPOSER_CHECK", "false")
    commit = os.environ.get("COMPOSER_COMMIT", "true")
    if check not in {"true", "false"} or commit not in {"true", "false"}:
        raise SystemExit("check and commit must be 'true' or 'false'")
    watch = os.environ.get("COMPOSER_WATCH_DIRECTORY") or "helm/"
    if not watched_changes(watch):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("skipped=true\ncommitted=false\n")
        return 0
    branch = commit_branch() if commit == "true" and check == "false" else ""
    output = Path(os.environ.get("COMPOSER_OUTPUT") or "compose.yaml").resolve()
    if "\n" in str(output) or "\r" in str(output):
        raise SystemExit("output path must not contain line breaks")
    if branch:
        repository_path(output)
        repository_path(output.parent / ".compose-generated")
        if os.environ.get("COMPOSER_REPORT"):
            repository_path(Path(os.environ["COMPOSER_REPORT"]))
    args: list[str] = []
    if (
        not os.environ.get("COMPOSER_CHART")
        and not os.environ.get("COMPOSER_MANIFESTS", "").strip()
    ):
        args.extend(["--chart", watch])
    for key in ("chart", "profile", "output", "report", "release", "namespace"):
        value = os.environ.get("COMPOSER_" + key.upper(), "")
        if value:
            args.extend(["--" + key, value])
    for key, flag in (("MANIFESTS", "manifest"), ("VALUES", "values")):
        for value in os.environ.get("COMPOSER_" + key, "").splitlines():
            if value.strip():
                args.extend(["--" + flag, value.strip()])
    if check == "true":
        args.append("--check")
    result = main(args)
    if result == 0:
        committed = (
            commit_outputs(output, os.environ.get("COMPOSER_REPORT", ""), branch)
            if branch
            else False
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(
                f"compose-file={output}\nskipped=false\ncommitted={str(committed).lower()}\n"
            )
    return result


if __name__ == "__main__":
    raise SystemExit(run())
