"""
Verify the Action wrapper against real CLI generation and drift detection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ActionTests(unittest.TestCase):
    """
    Exercise paths with spaces and shell syntax without executing input strings.
    """

    def test_generate_and_check(self) -> None:
        """Generate valid output, detect drift, and leave the checked file untouched."""
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = directory / "input $(false).yaml"
            manifest.write_text(
                "apiVersion: v1\nkind: Pod\nmetadata:\n  name: app\n"
                "spec:\n  containers:\n    - name: app\n      image: example:1\n"
            )
            output = directory / "compose file.yaml"
            github_output = directory / "outputs"
            env = {
                **os.environ,
                "PYTHONPATH": str(root / "pkg"),
                "COMPOSER_MANIFESTS": str(manifest),
                "COMPOSER_OUTPUT": str(output),
                "GITHUB_OUTPUT": str(github_output),
                "COMPOSER_CHECK": "false",
                "COMPOSER_COMMIT": "false",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
            }
            command = [sys.executable, str(root / "scripts/action.py")]
            result = subprocess.run(command, env=env, cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"compose-file={output.resolve()}", github_output.read_text())
            env["COMPOSER_CHECK"] = "true"
            result = subprocess.run(command, env=env, cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output.write_text("services: {}\n")
            previous = github_output.read_text()
            result = subprocess.run(command, env=env, cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(output.read_text(), "services: {}\n")
            self.assertEqual(github_output.read_text(), previous)

    def test_watched_changes_commit_and_push(self) -> None:
        """Push generated changes only, skip unrelated pushes, and avoid empty commits."""
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            remote = directory / "remote.git"
            checkout = directory / "checkout"
            checkout.mkdir()

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main")
            git("config", "commit.gpgsign", "false")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            git("remote", "add", "origin", str(remote))
            (checkout / "README.md").write_text("Initial\n")
            git("add", "README.md")
            git("commit", "-m", "Initial")
            before = git("rev-parse", "HEAD")
            (checkout / "helm").mkdir()
            manifest = checkout / "helm" / "input.yaml"
            manifest.write_text(
                "apiVersion: v1\nkind: Pod\nmetadata:\n  name: app\n"
                "spec:\n  containers:\n    - name: app\n      image: example:1\n"
            )
            git("add", "helm")
            git("commit", "-m", "Add manifests")
            after = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            (checkout / "unrelated.txt").write_text("Do not commit this\n")
            git("add", "unrelated.txt")
            event = directory / "event.json"
            outputs = directory / "outputs"
            env = {
                **os.environ,
                "PYTHONPATH": str(root / "pkg"),
                "COMPOSER_MANIFESTS": "helm/input.yaml",
                "COMPOSER_COMMIT": "true",
                "COMPOSER_CHECK": "false",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_OUTPUT": str(outputs),
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": after,
            }

            def invoke(previous: str, current: str) -> subprocess.CompletedProcess[str]:
                event.write_text(json.dumps({"before": previous, "after": current}))
                env["GITHUB_SHA"] = current
                outputs.write_text("")
                return subprocess.run(
                    [sys.executable, str(root / "scripts/action.py")],
                    cwd=checkout,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            result = invoke(before, after)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("committed=true", outputs.read_text())
            generated = git("rev-parse", "HEAD")
            self.assertNotEqual(generated, after)
            self.assertEqual(
                git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "compose.yaml"
            )
            self.assertEqual(git("diff", "--cached", "--name-only"), "unrelated.txt")
            self.assertEqual(git("ls-remote", "origin", "refs/heads/main").split()[0], generated)
            result = invoke(after, generated)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped=true", outputs.read_text())
            # A watched documentation change recompiles but produces no empty commit.
            (checkout / "helm" / "README.md").write_text("Chart documentation\n")
            git("add", "helm/README.md")
            git("commit", "--only", "-m", "Chart docs", "--", "helm/README.md")
            same = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            result = invoke(generated, same)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("committed=false", outputs.read_text())
            self.assertEqual(git("rev-parse", "HEAD"), same)
            # A new branch (zero before SHA) still compiles its watched inputs.
            result = invoke("0" * 40, same)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped=false", outputs.read_text())
            # A literal custom directory can include pathspec metacharacters.
            git("mv", "helm", "deploy[local]")
            git("commit", "--only", "-m", "Move manifests", "--", "helm", "deploy[local]")
            moved = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            env["COMPOSER_WATCH_DIRECTORY"] = "deploy[local]/"
            env["COMPOSER_MANIFESTS"] = "deploy[local]/input.yaml"
            result = invoke(same, moved)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped=false", outputs.read_text())
            git("rm", "--", "deploy[local]/README.md")
            git("commit", "--only", "-m", "Remove docs", "--", "deploy[local]/README.md")
            deleted = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            result = invoke(moved, deleted)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped=false", outputs.read_text())
            self.assertIn("committed=false", outputs.read_text())
            # Changing generated content replaces the existing file and creates one commit.
            manifest = checkout / "deploy[local]" / "input.yaml"
            manifest.write_text(manifest.read_text().replace("example:1", "example:2"))
            git("add", "--", "deploy[local]/input.yaml")
            git("commit", "--only", "-m", "Update image", "--", "deploy[local]/input.yaml")
            changed = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            result = invoke(deleted, changed)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("committed=true", outputs.read_text())
            updated = git("rev-parse", "HEAD")
            self.assertEqual(git("rev-parse", "HEAD^"), changed)
            self.assertIn("example:2", git("show", "HEAD:compose.yaml"))
            self.assertEqual(git("ls-remote", "origin", "refs/heads/main").split()[0], updated)
            # Rewriting identical bytes (even with a different mtime) must not commit.
            env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
            compose = checkout / "compose.yaml"
            previous_content = compose.read_bytes()
            os.utime(compose, (1, 1))
            result = invoke(changed, updated)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("committed=false", outputs.read_text())
            self.assertEqual(compose.read_bytes(), previous_content)
            self.assertEqual(git("rev-parse", "HEAD"), updated)
            self.assertEqual(git("ls-remote", "origin", "refs/heads/main").split()[0], updated)
            # Refuse to commit on pull requests, including fork and merge refs.
            env["GITHUB_EVENT_NAME"] = "pull_request"
            result = invoke(moved, deleted)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("commit: false", result.stderr)
