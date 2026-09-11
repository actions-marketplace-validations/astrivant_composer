"""
Verify manifest translation, profile bindings, and runtime dependency contracts.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from composer.ast.kubernetes import KUBERNETES_CONVERTER, KubernetesObject, Resource, SourceLocation
from composer.compiler import compile_resources
from composer.entrypoint import canonical, differences, validate
from composer.exceptions import CompilationError
from composer.profile import Context, bind, mapping, pointer, select_resource, validate_dependencies
from composer.utils import load_documents


def workload(name: str = "worker", namespace: str = "default") -> Resource:
    """
    Supply a small workload with resource caps, ports, environment, and initialization.

    Args:
        name (str): Workload name.
        namespace (str): Kubernetes namespace.

    Returns:
        Resource: Independent Deployment fixture.
    """
    raw: dict[str, object] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [
                        {"name": "migrate", "image": "example:1", "command": ["migrate"]}
                    ],
                    "containers": [
                        {
                            "name": "worker",
                            "image": "example:1",
                            "args": ["--serve"],
                            "env": [{"name": "MESSAGE", "value": "from-chart"}],
                            "ports": [{"containerPort": 8080}],
                            "resources": {"limits": {"cpu": "500m", "memory": "128Mi"}},
                            "readinessProbe": {"exec": {"command": ["true"]}},
                        }
                    ],
                }
            }
        },
    }
    return Resource(
        KUBERNETES_CONVERTER.structure(raw, KubernetesObject),
        SourceLocation(Path("fixture.yaml"), 0),
    )


def profile() -> dict[str, object]:
    """
    Supply runtime changes that preserve unmodified chart fields.

    Returns:
        dict[str, object]: Generic runtime profile with no service-name catalogs.
    """
    return {
        "schemaVersion": 1,
        "name": "fixture",
        "defaults": {
            "remove": ["/container_name", "/networks"],
            "set": {"restart": "unless-stopped"},
        },
        "services": {
            "worker": {
                "source": {"kind": "Deployment", "name": "worker"},
                "container": "worker",
                "set": {
                    "environment": {
                        "LITERAL": "${TOKEN:-local}",
                        "COUNT": {"$string": {"$values": "/count"}},
                    }
                },
            }
        },
    }


class CompilerTests(unittest.TestCase):
    """
    Protect inheritance and exact runtime semantics independently of Astrivant's profile.
    """

    def test_native_fields_and_values_bindings_remain_live(self) -> None:
        """
        Preserve native environment, images, quantities, and private container ports.

        Returns:
            None: Source changes propagate through declarative overrides.
        """
        result = compile_resources([workload()], profile(), {"count": 3})
        service = mapping(mapping(result.document["services"])["worker"])
        self.assertEqual(service["image"], "example:1")
        self.assertEqual(service["cpus"], 0.5)
        self.assertEqual(service["mem_limit"], 128 * 1024**2)
        self.assertEqual(service["expose"], ["8080"])
        self.assertNotIn("ports", service)
        self.assertEqual(
            service["environment"],
            {"MESSAGE": "from-chart", "LITERAL": "${TOKEN:-local}", "COUNT": "3"},
        )
        changed = workload()
        pod = mapping(mapping(changed.obj.spec["template"])["spec"])
        containers = pod["containers"]
        assert isinstance(containers, list)
        # Update the original nested mapping rather than relying on copied wrapper mappings.
        container = containers[0]
        assert isinstance(container, dict)
        container["image"] = "example:2"
        environment = container["env"]
        assert isinstance(environment, list)
        entry = environment[0]
        assert isinstance(entry, dict)
        entry["value"] = "updated"
        changed_result = compile_resources([changed], profile(), {"count": 4})
        current = mapping(mapping(changed_result.document["services"])["worker"])
        self.assertEqual(current["image"], "example:2")
        self.assertEqual(mapping(current["environment"])["COUNT"], "4")
        self.assertEqual(mapping(current["environment"])["MESSAGE"], "updated")
        validate(result.document, "compose-spec.json")

    def test_service_removal_overrides_shared_defaults(self) -> None:
        """
        Allow one-shot jobs to remove defaults inherited by long-running workloads.

        Returns:
            None: Restart and resource defaults are removed after inheritance.
        """
        rules = profile()
        worker = mapping(mapping(rules["services"])["worker"])
        worker.update({"init": True, "container": "migrate", "remove": ["/restart"]})
        rules["services"] = {"worker": worker}
        result = compile_resources([workload()], rules, {"count": 1})
        service = mapping(mapping(result.document["services"])["worker"])
        self.assertEqual(service["entrypoint"], ["migrate"])
        self.assertNotIn("restart", service)

    def test_ambiguous_missing_resources_and_containers_fail(self) -> None:
        """
        Refuse guessed sources when workload names or container selections change.

        Returns:
            None: Missing and ambiguous inputs surface as compilation failures.
        """
        selector: dict[str, object] = {"kind": "Deployment", "name": "worker"}
        for inputs in ([], [workload(), workload(namespace="other")]):
            with self.subTest(inputs=len(inputs)), self.assertRaises(CompilationError):
                select_resource(inputs, selector)
        with self.assertRaises(CompilationError):
            compile_resources([workload("renamed")], profile(), {"count": 1})
        rules = profile()
        worker = mapping(mapping(rules["services"])["worker"])
        worker["container"] = "missing"
        rules["services"] = {"worker": worker}
        with self.assertRaises(CompilationError):
            compile_resources([workload()], rules, {"count": 1})

    def test_binding_pointers_are_strict_and_preserve_interpolation(self) -> None:
        """
        Resolve escaped input paths and reject malformed binding expressions.

        Returns:
            None: Literal shell syntax stays literal; invalid references fail.
        """
        context = Context({"spec": {"version": "4.1.1"}}, {"image": "app:1"}, {"a/b": {"~key": 5}})
        self.assertEqual(
            bind({"$concat": ["broker:", {"$resource": "/spec/version"}]}, context), "broker:4.1.1"
        )
        self.assertEqual(bind({"$values": "/a~1b/~0key"}, context), 5)
        self.assertEqual(bind("sleep 60 & wait $$!", context), "sleep 60 & wait $$!")
        for value in (
            {"$values": "/missing"},
            {"$values": 1},
            {"$unknown": "/"},
            {"$concat": "bad"},
        ):
            with self.subTest(value=value), self.assertRaises(CompilationError):
                bind(value, context)
        with self.assertRaises(CompilationError):
            pointer([1], "/-1")

    def test_dependency_cycles_missing_nodes_and_health_gates_fail(self) -> None:
        """
        Ensure generated startup gates can be satisfied by the declared services.

        Returns:
            None: Invalid graphs fail and a valid healthy dependency succeeds.
        """
        for services in (
            {"a": {"depends_on": {"missing": {"condition": "service_started"}}}},
            {"a": {"depends_on": {"b": {"condition": "service_healthy"}}}, "b": {}},
            {
                "a": {"depends_on": {"b": {"condition": "service_started"}}},
                "b": {"depends_on": {"a": {"condition": "service_started"}}},
            },
        ):
            with self.subTest(services=services), self.assertRaises(CompilationError):
                validate_dependencies({"services": services})
        validate_dependencies(
            {
                "services": {
                    "a": {"depends_on": {"b": {"condition": "service_healthy"}}},
                    "b": {"healthcheck": {"test": ["CMD", "true"]}},
                }
            }
        )

    def test_profile_schema_rejects_ignored_or_misspelled_options(self) -> None:
        """
        Reject configuration that would silently bypass intended conversion rules.

        Returns:
            None: Valid profiles pass and unknown fields fail.
        """
        original = profile()
        validate(original, "profile.schema.json")
        invalid = copy.deepcopy(original)
        invalid["servcies"] = invalid.pop("services")
        with self.assertRaises(CompilationError):
            validate(invalid, "profile.schema.json")

    def test_reference_comparison_ignores_only_representation(self) -> None:
        """
        Normalize extension blocks and units while retaining commands and dependencies.

        Returns:
            None: Real runtime drift is reported without exposing values.
        """
        left: dict[str, object] = {
            "services": {
                "a": {"environment": {"COUNT": 1}, "mem_limit": "1g", "command": ["worker"]}
            }
        }
        right: dict[str, object] = {
            "x-shared": {},
            "services": {
                "a": {"environment": {"COUNT": "1"}, "mem_limit": 1073741824, "command": ["worker"]}
            },
        }
        self.assertEqual(differences(canonical(left), canonical(right)), [])
        different: dict[str, object] = {"services": {"a": {"command": ["unexpected"]}}}
        self.assertIn("/services/a/command", differences(canonical(left), canonical(different)))

    def test_generated_files_are_not_written_for_removed_mounts(self) -> None:
        """
        Keep unused source ConfigMaps and Secrets out of generated support files.

        Returns:
            None: A profile without generated mounts materializes no files.
        """
        resource = workload()
        template = resource.obj.spec["template"]
        assert isinstance(template, dict)
        pod = template["spec"]
        assert isinstance(pod, dict)
        pod["volumes"] = [{"name": "configuration", "configMap": {"name": "settings"}}]
        pod["containers"][0]["volumeMounts"] = [{"name": "configuration", "mountPath": "/config"}]
        config = Resource(
            KUBERNETES_CONVERTER.structure(
                {
                    "kind": "ConfigMap",
                    "apiVersion": "v1",
                    "metadata": {"name": "settings", "namespace": "default"},
                    "data": {"config.txt": "example"},
                },
                KubernetesObject,
            ),
            SourceLocation(Path("fixture.yaml"), 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            included = compile_resources([resource, config], profile(), {"count": 1}, directory)
            self.assertEqual(len(included.files), 1)
            rules = profile()
            worker = mapping(mapping(rules["services"])["worker"])
            worker["remove"] = ["/volumes"]
            rules["services"] = {"worker": worker}
            result = compile_resources([resource, config], rules, {"count": 1}, directory)
            self.assertEqual(result.files, {})

    def test_yaml_rejects_duplicates_and_accepts_merge_overrides(self) -> None:
        """
        Reject duplicate keys without breaking intentional YAML anchor overrides.

        Returns:
            None: Input ambiguity fails while explicit merge overrides remain valid.
        """
        with self.assertRaises(CompilationError):
            load_documents("services: {}\nservices: {}\n")
        documents = load_documents(
            "defaults: &defaults {restart: always}\n"
            "service: {<<: *defaults, restart: unless-stopped}\n"
        )
        self.assertEqual(mapping(mapping(documents[0])["service"])["restart"], "unless-stopped")

    def test_unset_environment_is_distinct_from_literal_none(self) -> None:
        """
        Keep Compose's inherited or unset environment semantics during comparisons.

        Returns:
            None: Null environment values are not normalized into the string None.
        """
        inherited: dict[str, object] = {"services": {"a": {"environment": {"TOKEN": None}}}}
        literal: dict[str, object] = {"services": {"a": {"environment": {"TOKEN": "None"}}}}
        self.assertEqual(
            differences(canonical(inherited), canonical(literal)), ["/services/a/environment/TOKEN"]
        )
