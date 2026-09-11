"""
Verify real Helm rendering, input bindings, and repeatable CLI drift checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from composer.entrypoint import load
from composer.profile import mapping


@unittest.skipUnless(shutil.which("helm"), "Helm is required for rendering tests")
class HelmTests(unittest.TestCase):
    """
    Render a disposable chart without a Kubernetes cluster or Docker daemon.
    """

    def test_coalesced_values_changes_propagate_and_check_is_read_only(self) -> None:
        """
        Prove generated output depends on Helm input rather than the reference Compose file.

        Returns:
            None: Changed values cause drift and a subsequent regeneration reflects them.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "chart"
            (chart / "templates").mkdir(parents=True)
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: fixture\nversion: 0.1.0\n")
            values = chart / "values.yaml"
            values.write_text("message: initial\ncount: 3\n")
            (chart / "templates/workload.yaml").write_text(
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\nspec:\n"
                "  template:\n    spec:\n      containers:\n        - name: worker\n"
                "          image: busybox:1.36\n          env:\n"
                "            - name: MESSAGE\n              value: {{ .Values.message | quote }}\n"
            )
            profile = root / "profile.yaml"
            profile.write_text(
                "schemaVersion: 1\nname: fixture\ndefaults:\n"
                "  remove: [/container_name, /networks]\n"
                "services:\n  worker:\n    source: {kind: Deployment, name: worker}\n"
                "    set:\n      environment:\n        COUNT: {$string: {$values: /count}}\n"
            )
            output = root / "compose.yaml"
            command = [
                sys.executable,
                "-m",
                "composer.entrypoint",
                "--chart",
                str(chart),
                "--profile",
                str(profile),
                "--output",
                str(output),
            ]
            generated = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            original = output.read_bytes()
            original_mtime = output.stat().st_mtime_ns
            clean = subprocess.run(
                [*command, "--check"], capture_output=True, text=True, check=False
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertEqual(output.stat().st_mtime_ns, original_mtime)
            values.write_text("message: changed\ncount: 7\n")
            drift = subprocess.run(
                [*command, "--check"], capture_output=True, text=True, check=False
            )
            self.assertEqual(drift.returncode, 1, drift.stderr)
            self.assertEqual(output.read_bytes(), original)
            regenerated = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
            environment = mapping(
                mapping(mapping(load(output)["services"])["worker"])["environment"]
            )
            self.assertEqual(environment, {"MESSAGE": "changed", "COUNT": "7"})
            self.assertEqual(
                sorted(path.name for path in (chart / "templates").iterdir()), ["workload.yaml"]
            )

    @unittest.skipUnless(os.environ.get("ASTRIVANT_ROOT"), "Set ASTRIVANT_ROOT for platform parity")
    def test_astrivant_matches_its_committed_runtime_contract(self) -> None:
        """
        Run the real platform profile against an independently loaded Compose reference.

        Returns:
            None: Helm-derived runtime output agrees with the platform Compose file.
        """
        root = Path(os.environ["ASTRIVANT_ROOT"]).resolve()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "composer.entrypoint",
                "--chart",
                str(root / "helm/astrivant"),
                "--release",
                "astrivant",
                "--namespace",
                "platform",
                "--values",
                str(root / "deploy/composer/values.yaml"),
                "--profile",
                str(root / "deploy/composer/profile.yaml"),
                "--output",
                str(root / "compose.yaml"),
                "--check",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
