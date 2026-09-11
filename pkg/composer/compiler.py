"""
Compile selected Helm resources through the reusable Kubernetes and Compose ASTs.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import attrs

from composer.ast.compose import (
    ConversionState,
    build_services,
    convert_container_to_service,
    parse_cpu_quantity,
    parse_memory_quantity,
    unstructure_compose_service,
)
from composer.ast.kubernetes import (
    WORKLOAD_KINDS,
    collect_configmaps,
    collect_secrets,
    namespace_key,
    workload_pod_spec,
)
from composer.exceptions import CompilationError
from composer.profile import (
    Context,
    bind,
    mapping,
    merge,
    remove,
    select_resource,
    validate_dependencies,
)
from composer.utils import as_dict, as_list

if TYPE_CHECKING:
    from composer.ast.kubernetes import Resource

log = logging.getLogger(__name__)


@attrs.frozen
class Compilation:
    """
    Keep runtime output separate from source provenance and materialized files.

    Attributes:
        document (dict[str, object]): Docker Compose document.
        provenance (dict[str, object]): Per-service source and override inventory.
        files (dict[Path, str]): ConfigMap and Secret files referenced by generated mounts.
    """

    document: dict[str, object]
    provenance: dict[str, object]
    files: dict[Path, str]


def limits(container: dict[str, object]) -> dict[str, object]:
    """
    Translate container resource limits into Compose CPU and memory caps.

    Args:
        container (dict[str, object]): Kubernetes container.

    Returns:
        dict[str, object]: CPU count and memory bytes when limits are present.

    Raises:
        CompilationError: A resources mapping has an invalid type.
        ValueError: A resource quantity cannot be parsed.
    """
    configured = mapping(mapping(container.get("resources", {})).get("limits", {}))
    result: dict[str, object] = {}
    if "cpu" in configured:
        result["cpus"] = float(parse_cpu_quantity(str(configured["cpu"])))
    if "memory" in configured:
        result["mem_limit"] = int(parse_memory_quantity(str(configured["memory"])))
    return result


def native_service(
    resource: Resource,
    rule: dict[str, object],
    name: str,
    resources: list[Resource],
    state: ConversionState,
    data_dir: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """
    Convert one selected workload container or expose an operator resource to bindings.

    Args:
        resource (Resource): Selected rendered object.
        rule (dict[str, object]): Container selector and runtime policy.
        name (str): Stable Compose service name.
        resources (list[Resource]): Inputs for ConfigMap and Secret resolution.
        state (ConversionState): Generated support-file state.
        data_dir (str): Compose-relative directory for mounted generated files.

    Returns:
        tuple[dict[str, object], dict[str, object]]: Native service and selected container.

    Raises:
        CompilationError: A container selector is absent or ambiguous.
        ValueError: Container resource limits are invalid.
    """
    obj = resource.obj
    if obj.kind not in WORKLOAD_KINDS:
        if "container" in rule:
            raise CompilationError(f"{name}: {obj.kind} is not a pod workload")
        return {}, {}
    pod = workload_pod_spec(obj)
    candidates = [
        mapping(item)
        for item in as_list(pod.get("initContainers" if rule.get("init", False) else "containers"))
    ]
    if "container" in rule:
        candidates = [item for item in candidates if item.get("name") == rule["container"]]
    if len(candidates) != 1:
        raise CompilationError(f"{name}: expected one selected container, found {len(candidates)}")
    container = candidates[0]
    args = argparse.Namespace(
        namespace=str(obj.metadata.get("namespace", "default")),
        network="default",
        publish_ports=False,
        configmap_data_dir=data_dir,
    )
    service = convert_container_to_service(
        obj,
        pod,
        as_dict(container),
        name,
        str(obj.metadata["name"]),
        namespace_key(obj.metadata, False),
        resource.source.file.as_posix(),
        collect_configmaps(resources, False),
        collect_secrets(resources, False),
        state,
        args,
    )
    result = mapping(unstructure_compose_service(service))
    result.update(limits(container))
    if "terminationGracePeriodSeconds" in pod:
        result["stop_grace_period"] = f"{pod['terminationGracePeriodSeconds']}s"
    if "ports" in result:
        result["expose"] = result.pop("ports")
    return result, container


def compile_resources(
    resources: list[Resource],
    profile: dict[str, object],
    values: dict[str, object],
    data_dir: str = ".compose-generated",
) -> Compilation:
    """
    Compile explicit runtime choices without reading an existing Compose file.

    Args:
        resources (list[Resource]): Rendered Helm manifests.
        profile (dict[str, object]): Validated deployment profile, or an empty object.
        values (dict[str, object]): Merged chart values available to bindings.
        data_dir (str): Compose-relative support-file directory.

    Returns:
        Compilation: Compose output, provenance, and referenced generated support files.

    Raises:
        CompilationError: Sources, bindings, or startup dependencies are invalid.
        ValueError: Resource quantities or conversion inputs are malformed.
    """
    state = ConversionState()
    services: dict[str, object] = {}
    provenance: dict[str, object] = {}
    if not profile:
        args = argparse.Namespace(
            namespace="default", network="default", publish_ports=False, configmap_data_dir=data_dir
        )
        native = build_services(
            resources,
            collect_configmaps(resources, True),
            collect_secrets(resources, True),
            state,
            args,
        )
        for name, converted in native.items():
            service = mapping(unstructure_compose_service(converted))
            if "ports" in service:
                service["expose"] = service.pop("ports")
            services[name] = service
    else:
        defaults = mapping(profile.get("defaults", {}))
        for name, raw in mapping(profile["services"]).items():
            rule = mapping(raw)
            resource = select_resource(resources, mapping(rule["source"]))
            service, container = native_service(resource, rule, name, resources, state, data_dir)
            context = Context(mapping(resource.obj.raw), container, values)
            removals = [
                str(path) for path in as_list(defaults.get("remove")) + as_list(rule.get("remove"))
            ]
            service = remove(service, [str(path) for path in as_list(defaults.get("remove"))])
            service = merge(service, mapping(bind(defaults.get("set", {}), context)))
            service = remove(service, [str(path) for path in as_list(rule.get("remove"))])
            service = merge(service, mapping(bind(rule.get("set", {}), context)))
            services[name] = service
            provenance[name] = {
                "kind": resource.obj.kind,
                "name": resource.obj.metadata["name"],
                "container": container.get("name"),
                "init": rule.get("init", False),
                "removed": removals,
                "overridden": sorted(mapping(rule.get("set", {}))),
            }
            log.info(
                "Compiled service=%s source_kind=%s source_name=%s",
                name,
                resource.obj.kind,
                resource.obj.metadata["name"],
            )
    if not services:
        raise CompilationError("No services were compiled")
    document: dict[str, object] = {"name": profile.get("name", "composer"), "services": services}
    volumes = profile.get("volumes", state.compose_volumes)
    if volumes:
        document["volumes"] = volumes
    if profile.get("networks"):
        document["networks"] = profile["networks"]
    validate_dependencies(document)
    # Dropped Kubernetes mounts must not cause unused Secret files to be written.
    mounts = [
        str(mapping(mount).get("source", ""))
        if isinstance(mount, dict)
        else str(mount).split(":", 1)[0]
        for raw in services.values()
        for mount in as_list(mapping(raw).get("volumes"))
    ]
    files = {
        path: text
        for path, text in state.generated_files.items()
        if any(
            (Path(data_dir) / path)
            .as_posix()
            .removeprefix("./")
            .startswith(source.removeprefix("./").rstrip("/") + "/")
            or (Path(data_dir) / path).as_posix().removeprefix("./") == source.removeprefix("./")
            for source in mounts
        )
    }
    return Compilation(document, provenance, files)
