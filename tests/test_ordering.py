"""
Exercise native GitOps ordering across controllers and selected runtime services.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from composer.ast.kubernetes import KUBERNETES_CONVERTER, KubernetesObject, Resource, SourceLocation
from composer.compiler import compile_resources
from composer.entrypoint import validate
from composer.exceptions import CompilationError
from composer.profile import mapping


def resource(
    name: str,
    kind: str = "Deployment",
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    spec: dict[str, object] | None = None,
    namespace: str = "default",
) -> Resource:
    """
    Build a workload or controller fixture.

    Args:
        name (str): Resource name.
        kind (str): Kubernetes kind.
        annotations (dict[str, str] | None): Ordering annotations.
        labels (dict[str, str] | None): Ownership labels.
        spec (dict[str, object] | None): Controller specification.
        namespace (str): Resource namespace.

    Returns:
        Resource: Parsed resource with a single application container for workloads.
    """
    api = {
        "Kustomization": "kustomize.toolkit.fluxcd.io/v1",
        "HelmRelease": "helm.toolkit.fluxcd.io/v2",
        "Application": "argoproj.io/v1alpha1",
        "Job": "batch/v1",
    }.get(kind, "apps/v1")
    raw: dict[str, object] = {
        "apiVersion": api,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": annotations or {},
            "labels": labels or {},
        },
        "spec": spec
        if spec is not None
        else {"template": {"spec": {"containers": [{"name": name, "image": "example:1"}]}}},
    }
    return Resource(
        KUBERNETES_CONVERTER.structure(raw, KubernetesObject),
        SourceLocation(Path("fixture.yaml"), 0),
    )


class OrderingTests(unittest.TestCase):
    """
    Verify conditions, grouping, exclusions, and errors in generated Compose.
    """

    def test_phases_precede_waves_and_jobs_complete(self) -> None:
        """Order PreSync before Sync regardless of wave and preserve parallel peers."""
        result = compile_resources(
            [
                resource(
                    "migrate",
                    "Job",
                    {"argocd.argoproj.io/hook": "PreSync", "argocd.argoproj.io/sync-wave": "10"},
                ),
                resource("api", annotations={"argocd.argoproj.io/sync-wave": "-2"}),
                resource("peer", annotations={"argocd.argoproj.io/sync-wave": "-2"}),
                resource("smoke", "Job", {"argocd.argoproj.io/hook": "PostSync"}),
                resource("cleanup", "Job", {"argocd.argoproj.io/hook": "SyncFail"}),
            ],
            {},
            {},
        )
        services = mapping(result.document["services"])
        self.assertNotIn("cleanup", services)
        self.assertEqual(
            mapping(services["api"])["depends_on"],
            {"migrate": {"condition": "service_completed_successfully"}},
        )
        self.assertIn("api", mapping(mapping(services["smoke"])["depends_on"]))
        validate(result.document, "compose-spec.json")

    def test_flux_cross_namespace_and_profile_names(self) -> None:
        """Expand namespaced Flux groups into renamed profile service dependencies."""
        for kind, group in (("Kustomization", "kustomize"), ("HelmRelease", "helm")):
            with self.subTest(kind=kind):
                prefix = group + ".toolkit.fluxcd.io/"
                resources = [
                    resource("base", kind, spec={}, namespace="infra"),
                    resource(
                        "app", kind, spec={"dependsOn": [{"name": "base", "namespace": "infra"}]}
                    ),
                    resource("db", labels={prefix + "name": "base", prefix + "namespace": "infra"}),
                    resource(
                        "api", labels={prefix + "name": "app", prefix + "namespace": "default"}
                    ),
                ]
                profile: dict[str, object] = {
                    "services": {
                        "database": {
                            "source": {"kind": "Deployment", "name": "db"},
                            "set": {"healthcheck": {"test": ["CMD", "true"]}},
                        },
                        "server": {"source": {"kind": "Deployment", "name": "api"}},
                    }
                }
                services = mapping(compile_resources(resources, profile, {}).document["services"])
                self.assertEqual(
                    mapping(services["server"])["depends_on"],
                    {"database": {"condition": "service_healthy"}},
                )

    def test_application_waves(self) -> None:
        """Expand application waves while keeping child waves scoped to each app."""
        resources = [
            resource("base", "Application", {"argocd.argoproj.io/sync-wave": "-1"}, spec={}),
            resource("app", "Application", spec={}),
            resource("db", labels={"argocd.argoproj.io/instance": "base"}),
            resource("api", labels={"argocd.argoproj.io/instance": "app"}),
        ]
        services = mapping(compile_resources(resources, {}, {}).document["services"])
        self.assertEqual(
            mapping(services["api"])["depends_on"], {"db": {"condition": "service_started"}}
        )

    def test_nested_flux_ownership(self) -> None:
        """Carry Kustomization ordering through HelmRelease ownership to workloads."""
        resources = [
            resource("base", "Kustomization", spec={}),
            resource("app", "Kustomization", spec={"dependsOn": [{"name": "base"}]}),
            resource(
                "chart", "HelmRelease", spec={}, labels={"kustomize.toolkit.fluxcd.io/name": "base"}
            ),
            resource("db", labels={"helm.toolkit.fluxcd.io/name": "chart"}),
            resource("api", labels={"kustomize.toolkit.fluxcd.io/name": "app"}),
        ]
        services = mapping(compile_resources(resources, {}, {}).document["services"])
        self.assertEqual(
            mapping(services["api"])["depends_on"], {"db": {"condition": "service_started"}}
        )

    def test_separate_applications_and_disabled_health(self) -> None:
        """Keep waves local to applications and avoid disabled health gates."""
        resources = [
            resource("one", "Application", spec={}),
            resource("two", "Application", spec={}),
            resource(
                "db",
                annotations={"argocd.argoproj.io/sync-wave": "-1"},
                labels={"app.kubernetes.io/instance": "one"},
            ),
            resource("api", labels={"app.kubernetes.io/instance": "one"}),
            resource("other", labels={"app.kubernetes.io/instance": "two"}),
        ]
        profile: dict[str, object] = {
            "services": {
                name: {
                    "source": {"kind": "Deployment", "name": name},
                    "set": {"healthcheck": {"disable": True}},
                }
                for name in ("db", "api", "other")
            }
        }
        services = mapping(compile_resources(resources, profile, {}).document["services"])
        self.assertEqual(
            mapping(services["api"])["depends_on"], {"db": {"condition": "service_started"}}
        )
        self.assertNotIn("depends_on", mapping(services["other"]))

    def test_invalid_ordering(self) -> None:
        """Reject malformed waves, missing dependencies, controller cycles and CEL."""
        fixtures = [
            [resource("bad", annotations={"argocd.argoproj.io/sync-wave": "one"})],
            [
                resource("api"),
                resource("app", "Kustomization", spec={"dependsOn": [{"name": "missing"}]}),
            ],
            [
                resource("api"),
                resource("app", "HelmRelease", spec={"dependsOn": [{"name": "app"}]}),
            ],
            [
                resource("api"),
                resource("base", "HelmRelease", spec={}),
                resource(
                    "app",
                    "HelmRelease",
                    spec={"dependsOn": [{"name": "base", "readyExpr": "true"}]},
                ),
            ],
        ]
        for resources in fixtures:
            with self.subTest(resources=resources), self.assertRaises(CompilationError):
                compile_resources(resources, {}, {})

    def test_profile_cycle_is_rejected(self) -> None:
        """Keep explicit dependencies subject to native ordering cycle checks."""
        resources = [
            resource("first", annotations={"argocd.argoproj.io/sync-wave": "-1"}),
            resource("last"),
        ]
        profile: dict[str, object] = {
            "services": {
                "first": {
                    "source": {"kind": "Deployment", "name": "first"},
                    "set": {"depends_on": {"last": {"condition": "service_started"}}},
                },
                "last": {"source": {"kind": "Deployment", "name": "last"}},
            }
        }
        with self.assertRaisesRegex(CompilationError, "cycle"):
            compile_resources(resources, profile, {})
