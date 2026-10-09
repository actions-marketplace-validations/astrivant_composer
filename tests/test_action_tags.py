"""
Check pushed tag objects against their peeled source commits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TagEvents(unittest.TestCase):
    """
    Exercise lightweight, annotated, and mismatched tags through the Action entry point.
    """

    def test_tag_objects_preserve_checkout_verification(self) -> None:
        """
        Accept both tag forms while continuing to reject a checkout of another commit.

        Returns:
            None: Correct tags compile without committing; a wrong target still fails.
        """
        project = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                """
                Execute a synthetic Git fixture operation.

                Args:
                    *args (str): Literal Git arguments.

                Returns:
                    str: Captured standard output.
                """
                return subprocess.run(
                    ["git", *args], cwd=root, capture_output=True, text=True, check=True
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            manifest = root / "input.yaml"
            manifest.write_text(
                "apiVersion: v1\nkind: Pod\nmetadata:\n  name: app\nspec:\n"
                "  containers:\n    - name: app\n      image: example:1\n"
            )
            git("add", "input.yaml")
            git("commit", "-qm", "Fixture")
            git("tag", "-a", "annotated", "-m", "Fixture release")
            commit = git("rev-parse", "HEAD")
            tag = git("rev-parse", "annotated")
            self.assertNotEqual(commit, tag)
            event = root / "event.json"
            env = {
                **os.environ,
                "PYTHONPATH": str(project / "pkg"),
                "COMPOSER_MANIFESTS": "input.yaml",
                "COMPOSER_WATCH_DIRECTORY": ".",
                "COMPOSER_OUTPUT": "compose.yaml",
                "COMPOSER_CHECK": "false",
                "COMPOSER_COMMIT": "false",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REF": "refs/tags/fixture",
                "GITHUB_SHA": commit,
                "GITHUB_OUTPUT": str(root / "outputs"),
            }

            for after in (commit, tag):
                event.write_text(json.dumps({"before": "0" * 40, "after": after}))
                result = subprocess.run(
                    [sys.executable, str(project / "scripts/action.py")],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(git("rev-parse", "HEAD"), commit)

            # The event still targets the old tag after the checkout advances.
            manifest.write_text(manifest.read_text().replace("example:1", "example:2"))
            git("add", "input.yaml")
            git("commit", "-qm", "Other revision")
            result = subprocess.run(
                [sys.executable, str(project / "scripts/action.py")],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Checkout must match", result.stderr)
