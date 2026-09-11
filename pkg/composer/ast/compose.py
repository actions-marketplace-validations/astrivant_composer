"""
Docker Compose models, rendering, and Kubernetes workload compilation.
"""

from __future__ import annotations

import io
import logging
import re
import shlex
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

import attrs
from cattrs import Converter
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString, PlainScalarString

from composer.ast.kubernetes import (
    WORKLOAD_KINDS,
    ConfigData,
    KubernetesObject,
    Resource,
    SecretData,
    describe_resource,
    lookup_config,
    lookup_secret,
    namespace_key,
    secret_env_name,
    stateful_claim_names,
    workload_kind,
    workload_pod_spec,
)
from composer.constants import (
    BINARY_QUANTITY_FACTORS,
    DECIMAL_QUANTITY_FACTORS,
)
from composer.utils import (
    as_dict,
    as_list,
    as_str,
    make_yaml,
    normalize_compose_name,
    normalize_env_part,
    normalize_path_part,
    scalar_to_env_value,
    sort_dict,
    unique_list,
)

if TYPE_CHECKING:
    import argparse
    from typing import Any


log = logging.getLogger(__name__)
TMPFS_VOLUME_SIZE = "512m"
TMPFS_MOUNT_DEFAULT = "/tmp"
HELM_CHART_LABEL_KEYS = ("helm.sh/chart", "chart")
COMPOSE_RUNTIME_VARIABLE_PATTERN = re.compile(
    r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)
DEPENDENCY_CONDITION_COMPLETED = "service_completed_successfully"
DEPENDENCY_CONDITION_HEALTHY = "service_healthy"
VOLUME_INIT_DEPENDENCY_SUFFIX = "-volume-init"
UNANCHORED_ENVIRONMENT_KEYS = {
    "CONTAINER_CPU_LIMIT",
    "CONTAINER_CPU_REQUEST",
    "CONTAINER_MEM_LIMIT",
    "CONTAINER_MEM_REQUEST",
}


@attrs.define
class ConversionState:
    """
    Tracks mutable compilation output and diagnostics.

    Attributes:
        env_vars (dict[str, str]): Secret environment values to write.
        env_source (dict[str, str]): Source labels for environment values.
        warnings (list[str]): Human-readable compilation warnings.
        generated_files (dict[Path, str]): Generated support files by path.
        compose_volumes (dict[str, dict[str, object]]): Top-level Compose volumes.
        kept_workloads (set[str]): Workload names already compiled.
        skipped_workloads (list[str]): Duplicate workload diagnostics.
        service_names (set[str]): Compose service names already allocated.
    """

    env_vars: dict[str, str] = attrs.field(factory=dict)
    env_source: dict[str, str] = attrs.field(factory=dict)
    warnings: list[str] = attrs.field(factory=list)
    generated_files: dict[Path, str] = attrs.field(factory=dict)
    compose_volumes: dict[str, dict[str, object]] = attrs.field(factory=dict)
    kept_workloads: set[str] = attrs.field(factory=set)
    skipped_workloads: list[str] = attrs.field(factory=list)
    service_names: set[str] = attrs.field(factory=set)


type ComposeCommand = str | list[str]
type ComposeVolume = str | dict[str, object]


@attrs.define
class ComposeService:
    """
    Represents the minimal Compose service emitted for one container.

    Attributes:
        image (str): Container image reference.
        source_manifest (str): Manifest path that produced the service.
        chart (str): Helm chart label that produced the service, when available.
        container_name (str | None): Explicit Docker container name.
        user (str | None): Runtime user or user/group string.
        group_add (list[str]): Supplementary groups added to the container process.
        entrypoint (ComposeCommand | None): Compose entrypoint override.
        command (ComposeCommand | None): Compose command override.
        environment (dict[str, str]): Environment variables.
        ports (list[str]): Compose port mappings.
        volumes (list[ComposeVolume]): Compose volume mounts.
        networks (list[str]): Compose networks joined without aliases.
        network_aliases (dict[str, list[str]]): Network aliases by network.
        healthcheck (dict[str, object] | None): Compose healthcheck block.
        restart (str | None): Compose restart policy.
        profiles (list[str]): Compose profiles that gate this service.
        depends_on (list[str]): Dependency service names.
    """

    image: str
    source_manifest: str = ""
    chart: str = ""
    container_name: str | None = None
    user: str | None = None
    group_add: list[str] = attrs.field(factory=list)
    entrypoint: ComposeCommand | None = None
    command: ComposeCommand | None = None
    environment: dict[str, str] = attrs.field(factory=dict)
    ports: list[str] = attrs.field(factory=list)
    volumes: list[ComposeVolume] = attrs.field(factory=list)
    networks: list[str] = attrs.field(factory=list)
    network_aliases: dict[str, list[str]] = attrs.field(factory=dict)
    healthcheck: dict[str, object] | None = None
    restart: str | None = None
    profiles: list[str] = attrs.field(factory=list)
    depends_on: list[str] = attrs.field(factory=list)


@attrs.define(frozen=True)
class ComposeDocument:
    """
    Represents the top-level Docker Compose document.

    Attributes:
        services (dict[str, ComposeService]): Compose services by name.
        networks (dict[str, dict[str, object]]): Top-level network definitions.
        volumes (dict[str, dict[str, object]]): Top-level volume definitions.
    """

    services: dict[str, ComposeService]
    networks: dict[str, dict[str, object]]
    volumes: dict[str, dict[str, object]] = attrs.field(factory=dict)


def make_converter() -> Converter:
    """
    Create the cattrs converter used for Compose output.

    Returns:
        Converter: Configured converter for Compose output models.
    """
    converter = Converter()
    converter.register_unstructure_hook(
        ComposeService,
        unstructure_compose_service,
    )
    converter.register_unstructure_hook(
        ComposeDocument,
        unstructure_compose_document,
    )
    return converter


def unstructure_compose_service(service: ComposeService) -> dict[str, Any]:
    """
    Convert a Compose service model to a minimal mapping.

    Args:
        service (ComposeService): Compose service model.

    Returns:
        dict[str, Any]: Compose service mapping with empty optional fields omitted.
    """
    result: dict[str, Any] = {"image": service.image}
    if service.container_name is not None:
        result["container_name"] = service.container_name
    if service.user is not None:
        result["user"] = service.user
    if service.group_add:
        result["group_add"] = service.group_add
    if service.entrypoint is not None:
        result["entrypoint"] = service.entrypoint
    if service.command is not None:
        result["command"] = service.command
    if service.environment:
        result["environment"] = service.environment
    if service.ports:
        result["ports"] = service.ports
    if service.volumes:
        result["volumes"] = service.volumes
    if service.network_aliases:
        result["networks"] = {
            network: {"aliases": aliases} if aliases else {}
            for network, aliases in service.network_aliases.items()
        }
    elif service.networks:
        result["networks"] = service.networks
    if service.healthcheck is not None:
        result["healthcheck"] = service.healthcheck
    if service.restart is not None:
        result["restart"] = service.restart
    if service.profiles:
        result["profiles"] = service.profiles
    if service.depends_on:
        result["depends_on"] = compose_depends_on(service.depends_on)
    return result


def compose_depends_on(
    dependencies: list[str],
) -> dict[str, dict[str, str]]:
    """
    Build a Compose ``depends_on`` mapping with dependency conditions.

    Args:
        dependencies (list[str]): Dependency service names to render.

    Returns:
        dict[str, dict[str, str]]: Compose ``depends_on`` mapping keyed by dependency service name.
    """
    return {
        dependency: {"condition": dependency_condition(dependency)} for dependency in dependencies
    }


def dependency_condition(dependency: str) -> str:
    """
    Return the Compose condition for a dependency service.

    Args:
        dependency (str): Dependency service name.

    Returns:
        str: Completion condition for init services, otherwise health condition.
    """
    if dependency.endswith(VOLUME_INIT_DEPENDENCY_SUFFIX):
        return DEPENDENCY_CONDITION_COMPLETED
    return "service_started"


def unstructure_compose_document(document: ComposeDocument) -> dict[str, Any]:
    """
    Convert a Compose document model to a minimal mapping.

    Args:
        document (ComposeDocument): Compose document model.

    Returns:
        dict[str, Any]: Compose document mapping with empty optional fields omitted.
    """
    result: dict[str, Any] = {
        "services": {
            name: COMPOSE_CONVERTER.unstructure(service)
            for name, service in document.services.items()
        },
        "networks": document.networks,
    }
    if document.volumes:
        result["volumes"] = document.volumes
    return result


COMPOSE_CONVERTER = make_converter()


def build_services(
    resources: list[Resource],
    configmaps: dict[tuple[str, str], ConfigData],
    secrets: dict[tuple[str, str], SecretData],
    state: ConversionState,
    args: argparse.Namespace,
) -> dict[str, ComposeService]:
    """
    Compile Kubernetes workload resources into Compose services.

    Args:
        resources (list[Resource]): Parsed Kubernetes resources.
        configmaps (dict[tuple[str, str], ConfigData]): Indexed ConfigMap data.
        secrets (dict[tuple[str, str], SecretData]): Indexed Secret data.
        state (ConversionState): Mutable compilation state.
        args (argparse.Namespace): Parsed command-line options.

    Returns:
        dict[str, ComposeService]: Compose service mapping.
    """
    services: dict[str, ComposeService] = {}
    dropped_kinds: dict[str, int] = {}

    for resource in resources:
        obj = resource.obj
        kind = as_str(obj.get("kind"))
        if kind not in WORKLOAD_KINDS:
            dropped_kinds[kind or "Unknown"] = dropped_kinds.get(kind or "Unknown", 0) + 1
            continue
        metadata = as_dict(obj.get("metadata"))
        workload_name = as_str(metadata.get("name"))
        namespace = namespace_key(metadata, True)
        dedupe_key = workload_name
        if dedupe_key in state.kept_workloads:
            state.skipped_workloads.append(describe_resource(resource))
            log.info(
                "Dropping workload %s reason=duplicate-single-namespace",
                describe_resource(resource),
            )
            continue
        state.kept_workloads.add(dedupe_key)

        pod_spec = workload_pod_spec(obj)
        containers = as_list(pod_spec.get("containers"))
        if not containers:
            state.warnings.append(f"{describe_resource(resource)} has no containers")
            log.info(
                "Dropping workload %s reason=no-containers",
                describe_resource(resource),
            )
            continue

        for container in containers:
            container_data = as_dict(container)
            service_name = unique_service_name(
                state,
                service_base_name(workload_name, container_data),
            )
            service = convert_container_to_service(
                obj=obj,
                pod_spec=pod_spec,
                container=container_data,
                service_name=service_name,
                workload_name=workload_name,
                namespace=namespace,
                source_manifest=resource.source.file.as_posix(),
                configmaps=configmaps,
                secrets=secrets,
                state=state,
                args=args,
            )
            services[service_name] = service
            log.info(
                (
                    "Translated workload kind=%s name=%s container=%s service=%s "
                    "chart=%s env=%s ports=%s volumes=%s"
                ),
                kind,
                workload_name,
                container_data.get("name", ""),
                service_name,
                service.chart,
                len(service.environment),
                len(service.ports),
                len(service.volumes),
            )

    add_depends_on(services)

    for kind, count in sorted(dropped_kinds.items()):
        log.info("Dropped Kubernetes resources kind=%s count=%s", kind, count)
    log.info("Built Compose services count=%s", len(services))
    return services


def convert_container_to_service(
    obj: KubernetesObject,
    pod_spec: dict[str, Any],
    container: dict[str, Any],
    service_name: str,
    workload_name: str,
    namespace: str,
    source_manifest: str,
    configmaps: dict[tuple[str, str], ConfigData],
    secrets: dict[tuple[str, str], SecretData],
    state: ConversionState,
    args: argparse.Namespace,
) -> ComposeService:
    """
    Compile one Kubernetes container spec into a Compose service.

    Args:
        obj (KubernetesObject): Source workload object.
        pod_spec (dict[str, Any]): Kubernetes pod spec that owns the container.
        container (dict[str, Any]): Container spec to compile.
        service_name (str): Compose service name.
        workload_name (str): Kubernetes workload name.
        namespace (str): Flattened or real namespace lookup key.
        source_manifest (str): Manifest path that produced the workload.
        configmaps (dict[tuple[str, str], ConfigData]): Indexed ConfigMap data.
        secrets (dict[tuple[str, str], SecretData]): Indexed Secret data.
        state (ConversionState): Mutable compilation state.
        args (argparse.Namespace): Parsed command-line options.

    Returns:
        ComposeService: Compose service definition.
    """
    image = as_str(container.get("image"))
    service = ComposeService(
        image=image,
        source_manifest=source_manifest,
        chart=workload_chart_label(obj),
        container_name=service_name,
    )

    user = collect_container_user(pod_spec, container, service_name)
    if user:
        service.user = user
    group_add = collect_container_groups(pod_spec, container, service_name, user)
    if group_add:
        service.group_add = group_add

    command = container.get("command")
    if command:
        service.entrypoint = normalize_command(command)
    args_value = container.get("args")
    if args_value:
        service.command = normalize_command(args_value)

    env = collect_container_env(
        container=container,
        workload_name=workload_name,
        namespace=namespace,
        configmaps=configmaps,
        secrets=secrets,
        state=state,
        args=args,
    )
    if env:
        service.environment = env

    ports = collect_ports(container, args.publish_ports)
    if ports:
        service.ports = ports

    volumes = collect_volumes(
        obj=obj,
        pod_spec=pod_spec,
        container=container,
        service_name=service_name,
        namespace=namespace,
        configmaps=configmaps,
        secrets=secrets,
        state=state,
        args=args,
    )
    if volumes:
        service.volumes = compose_volume_mounts(volumes)

    storage_volumes = collect_resource_storage_volumes(
        container=container,
        service_name=service_name,
        environment=env,
        state=state,
    )
    if storage_volumes:
        combined_volumes: list[ComposeVolume] = [*service.volumes, *storage_volumes]
        service.volumes = unique_compose_volume_mounts(combined_volumes)

    healthcheck = readiness_healthcheck(container)
    if healthcheck:
        service.healthcheck = healthcheck

    service.networks = [args.network]

    if workload_kind(obj) == "Job":
        service.restart = "on-failure"

    return service


def workload_chart_label(obj: KubernetesObject) -> str:
    """
    Return the Helm chart label attached to a workload.

    Args:
        obj (KubernetesObject): Source Kubernetes workload object.

    Returns:
        str: Helm chart label value, or an empty string when absent.
    """
    for labels in workload_label_sets(obj):
        for label_key in HELM_CHART_LABEL_KEYS:
            chart = as_str(labels.get(label_key))
            if chart:
                return chart
    return ""


def workload_label_sets(obj: KubernetesObject) -> list[dict[str, Any]]:
    """
    Return resource and pod-template label mappings for a workload.

    Args:
        obj (KubernetesObject): Source Kubernetes workload object.

    Returns:
        list[dict[str, Any]]: Label mappings in lookup priority order.
    """
    metadata = as_dict(obj.get("metadata"))
    spec = as_dict(obj.get("spec"))
    template = as_dict(spec.get("template"))
    template_metadata = as_dict(template.get("metadata"))
    return [
        as_dict(metadata.get("labels")),
        as_dict(template_metadata.get("labels")),
    ]


def collect_container_user(
    pod_spec: dict[str, Any],
    container: dict[str, Any],
    service_name: str,
) -> str:
    """
    Translate Kubernetes user/group security context into Compose user syntax.

    Args:
        pod_spec (dict[str, Any]): Kubernetes pod spec that owns the container.
        container (dict[str, Any]): Container spec to inspect.
        service_name (str): Compose service name used for audit logging.

    Returns:
        str: Compose ``user`` value, or an empty string when no user/group is defined.
    """
    pod_context = as_dict(pod_spec.get("securityContext"))
    container_context = as_dict(container.get("securityContext"))
    run_as_user = security_context_value(
        container_context,
        pod_context,
        "runAsUser",
    )
    run_as_group = security_context_value(
        container_context,
        pod_context,
        "runAsGroup",
    )
    if not run_as_user and not run_as_group:
        return ""
    if not run_as_user:
        log.info(
            "Skipped group-only security context user service=%s group=%s",
            service_name,
            run_as_group,
        )
        return ""
    user = run_as_user
    if run_as_group:
        user = f"{user}:{run_as_group}"
    log.info("Translated security context user service=%s user=%s", service_name, user)
    return user


def collect_container_groups(
    pod_spec: dict[str, Any],
    container: dict[str, Any],
    service_name: str,
    user: str,
) -> list[str]:
    """
    Translate Kubernetes supplementary group context into Compose groups.

    Args:
        pod_spec (dict[str, Any]): Kubernetes pod spec that owns the container.
        container (dict[str, Any]): Container spec to inspect.
        service_name (str): Compose service name used for audit logging.
        user (str): Already translated Compose ``user`` value, if any.

    Returns:
        list[str]: Compose ``group_add`` values.
    """
    pod_context = as_dict(pod_spec.get("securityContext"))
    container_context = as_dict(container.get("securityContext"))
    groups: list[str] = []
    run_as_user = security_context_value(
        container_context,
        pod_context,
        "runAsUser",
    )
    run_as_group = security_context_value(
        container_context,
        pod_context,
        "runAsGroup",
    )
    fs_group = security_context_value(container_context, pod_context, "fsGroup")
    supplemental_groups = security_context_list(
        container_context,
        pod_context,
        "supplementalGroups",
    )
    if run_as_group and not run_as_user and not user:
        groups.append(run_as_group)
    if fs_group:
        groups.append(fs_group)
    groups.extend(supplemental_groups)
    result = unique_list(groups)
    if result:
        log.info(
            "Translated security context groups service=%s groups=%s",
            service_name,
            ",".join(result),
        )
    return result


def security_context_value(
    container_context: dict[str, Any],
    pod_context: dict[str, Any],
    key: str,
) -> str:
    """
    Return a security context value with container context taking precedence.

    Args:
        container_context (dict[str, Any]): Container-level securityContext mapping.
        pod_context (dict[str, Any]): Pod-level securityContext mapping.
        key (str): Security context key to resolve.

    Returns:
        str: Stringified context value, or an empty string when undefined.
    """
    value = container_context.get(key)
    if value is None:
        value = pod_context.get(key)
    if value is None:
        return ""
    return str(value)


def security_context_list(
    container_context: dict[str, Any],
    pod_context: dict[str, Any],
    key: str,
) -> list[str]:
    """
    Return list security context values with container context taking precedence.

    Args:
        container_context (dict[str, Any]): Container-level securityContext mapping.
        pod_context (dict[str, Any]): Pod-level securityContext mapping.
        key (str): Security context key to resolve.

    Returns:
        list[str]: Stringified context values, or an empty list when undefined.
    """
    value = container_context.get(key)
    if value is None:
        value = pod_context.get(key)
    return [str(item) for item in as_list(value)]


def collect_container_env(
    container: dict[str, Any],
    workload_name: str,
    namespace: str,
    configmaps: dict[tuple[str, str], ConfigData],
    secrets: dict[tuple[str, str], SecretData],
    state: ConversionState,
    args: argparse.Namespace,
) -> dict[str, str]:
    """
    Build a Compose environment mapping from Kubernetes env sources.

    Args:
        container (dict[str, Any]): Container spec to inspect.
        workload_name (str): Name used in diagnostics.
        namespace (str): Flattened or real namespace lookup key.
        configmaps (dict[tuple[str, str], ConfigData]): Indexed ConfigMap data.
        secrets (dict[tuple[str, str], SecretData]): Indexed Secret data.
        state (ConversionState): Mutable compilation state.
        args (argparse.Namespace): Parsed command-line options.

    Returns:
        dict[str, str]: Compose environment mapping.
    """
    env: dict[str, str] = {}
    for source in as_list(container.get("envFrom")):
        source_data = as_dict(source)
        config_ref = as_dict(source_data.get("configMapRef"))
        secret_ref = as_dict(source_data.get("secretRef"))
        if config_ref:
            name = as_str(config_ref.get("name"))
            config = lookup_config(configmaps, namespace, name)
            if not config:
                if not config_ref.get("optional"):
                    state.warnings.append(f"{workload_name} references missing ConfigMap {name}")
                continue
            for key, value in config.values.items():
                env[key] = normalize_env_value(key, value, args.namespace)
        if secret_ref:
            name = as_str(secret_ref.get("name"))
            secret = lookup_secret(secrets, namespace, name)
            if not secret:
                if not secret_ref.get("optional"):
                    state.warnings.append(f"{workload_name} references missing Secret {name}")
                continue
            for key, value in secret.values.items():
                variable = remember_env_value(
                    state,
                    secret_env_name(name, key),
                    value,
                    f"secret {name}/{key}",
                )
                env[key] = compose_environment_reference(variable)

    for entry in as_list(container.get("env")):
        env_entry = as_dict(entry)
        name = as_str(env_entry.get("name"))
        if not name:
            continue
        if "value" in env_entry:
            env[name] = normalize_env_value(
                name,
                scalar_to_env_value(env_entry.get("value")),
                args.namespace,
            )
            continue

        value_from = as_dict(env_entry.get("valueFrom"))
        secret_ref = as_dict(value_from.get("secretKeyRef"))
        config_ref = as_dict(value_from.get("configMapKeyRef"))
        field_ref = as_dict(value_from.get("fieldRef"))
        resource_ref = as_dict(value_from.get("resourceFieldRef"))

        if secret_ref:
            secret_name = as_str(secret_ref.get("name"))
            secret_key = as_str(secret_ref.get("key"))
            requested_variable = secret_env_name(secret_name, secret_key)
            source_value = state.env_vars.get(requested_variable, "")
            secret = lookup_secret(secrets, namespace, secret_name)
            value = ""
            if secret and secret_key in secret.values:
                value = secret.values[secret_key]
            elif source_value:
                value = source_value
            elif secret_ref.get("optional"):
                value = ""
            else:
                state.warnings.append(
                    f"{workload_name} references missing Secret key {secret_name}/{secret_key}"
                )
            variable = remember_env_value(
                state,
                requested_variable,
                value,
                f"secret {secret_name}/{secret_key}",
            )
            env[name] = compose_environment_reference(variable)
        elif config_ref:
            config_name = as_str(config_ref.get("name"))
            config_key = as_str(config_ref.get("key"))
            config = lookup_config(configmaps, namespace, config_name)
            if config and config_key in config.values:
                env[name] = normalize_env_value(
                    name,
                    config.values[config_key],
                    args.namespace,
                )
            elif not config_ref.get("optional"):
                state.warnings.append(
                    f"{workload_name} references missing ConfigMap key {config_name}/{config_key}"
                )
        elif field_ref:
            field_path = as_str(field_ref.get("fieldPath"))
            env[name] = field_ref_value(field_path, args.namespace)
        elif resource_ref:
            env[name] = resource_ref_value(resource_ref, container)

    return sort_dict(env)


def collect_ports(container: dict[str, Any], publish_ports: bool) -> list[str]:
    """
    Translate Kubernetes container ports to Compose port entries.

    Args:
        container (dict[str, Any]): Container spec to inspect.
        publish_ports (bool): Whether to expose ports on the host.

    Returns:
        list[str]: Compose port strings.
    """
    ports: list[str] = []
    for port in as_list(container.get("ports")):
        port_data = as_dict(port)
        container_port = port_data.get("containerPort")
        if container_port is None:
            continue
        port_text = str(container_port)
        protocol = as_str(port_data.get("protocol", "TCP")).lower()
        suffix = "" if protocol == "tcp" else f"/{protocol}"
        if publish_ports:
            ports.append(f"{port_text}:{port_text}{suffix}")
        else:
            ports.append(f"{port_text}{suffix}")
    return unique_list(ports)


def collect_volumes(
    obj: KubernetesObject,
    pod_spec: dict[str, Any],
    container: dict[str, Any],
    service_name: str,
    namespace: str,
    configmaps: dict[tuple[str, str], ConfigData],
    secrets: dict[tuple[str, str], SecretData],
    state: ConversionState,
    args: argparse.Namespace,
) -> list[str]:
    """
    Translate Kubernetes volume mounts into Compose volume strings.

    Args:
        obj (KubernetesObject): Source workload object.
        pod_spec (dict[str, Any]): Kubernetes pod spec that owns the container.
        container (dict[str, Any]): Container spec to inspect.
        service_name (str): Compose service name.
        namespace (str): Flattened or real namespace lookup key.
        configmaps (dict[tuple[str, str], ConfigData]): Indexed ConfigMap data.
        secrets (dict[tuple[str, str], SecretData]): Indexed Secret data.
        state (ConversionState): Mutable compilation state.
        args (argparse.Namespace): Parsed command-line options.

    Returns:
        list[str]: Compose volume mount strings.
    """
    volumes_by_name = {
        as_str(volume.get("name")): as_dict(volume) for volume in as_list(pod_spec.get("volumes"))
    }
    claim_names = stateful_claim_names(obj)
    mounts: list[str] = []

    for mount in as_list(container.get("volumeMounts")):
        mount_data = as_dict(mount)
        volume_name = as_str(mount_data.get("name"))
        mount_path = as_str(mount_data.get("mountPath"))
        sub_path = as_str(mount_data.get("subPath"))
        read_only = bool(mount_data.get("readOnly"))
        mode = ":ro" if read_only else ""

        if volume_name in volumes_by_name:
            volume = volumes_by_name[volume_name]
            if "configMap" in volume:
                config_ref = as_dict(volume.get("configMap"))
                config_name = as_str(config_ref.get("name"))
                config = lookup_config(configmaps, namespace, config_name)
                if config:
                    config_files = projected_volume_files(
                        values=config.values,
                        items=as_list(config_ref.get("items")),
                        service_name=service_name,
                        source_kind="ConfigMap",
                        source_name=config_name,
                        state=state,
                    )
                    if sub_path:
                        content = dict(config_files).get(sub_path)
                        if content is None:
                            state.warnings.append(
                                f"{service_name} mounts missing ConfigMap key "
                                f"{config_name}/{sub_path}"
                            )
                            continue
                        rel = generated_config_path(namespace, config_name, sub_path)
                        state.generated_files[rel] = content
                        source = generated_mount_source(args.configmap_data_dir, rel)
                        mounts.append(f"{source}:{mount_path}{mode}")
                    else:
                        for item_path, content in config_files:
                            rel = generated_config_path(
                                namespace,
                                config_name,
                                item_path,
                            )
                            state.generated_files[rel] = content
                        config_dir = generated_config_dir(namespace, config_name)
                        source = generated_mount_source(
                            args.configmap_data_dir,
                            config_dir,
                        )
                        mounts.append(f"{source}:{mount_path}{mode}")
                continue
            if "secret" in volume:
                secret_ref = as_dict(volume.get("secret"))
                secret_name = as_str(secret_ref.get("secretName"))
                secret = lookup_secret(secrets, namespace, secret_name)
                if secret:
                    secret_files = projected_volume_files(
                        values=secret.values,
                        items=as_list(secret_ref.get("items")),
                        service_name=service_name,
                        source_kind="Secret",
                        source_name=secret_name,
                        state=state,
                    )
                    if sub_path:
                        content = dict(secret_files).get(sub_path)
                        if content is None:
                            state.warnings.append(
                                f"{service_name} mounts missing Secret key {secret_name}/{sub_path}"
                            )
                            continue
                        rel = generated_secret_path(namespace, secret_name, sub_path)
                        state.generated_files[rel] = content
                        source = generated_mount_source(args.configmap_data_dir, rel)
                        mounts.append(f"{source}:{mount_path}{mode}")
                    else:
                        for item_path, content in secret_files:
                            rel = generated_secret_path(
                                namespace,
                                secret_name,
                                item_path,
                            )
                            state.generated_files[rel] = content
                        secret_dir = generated_secret_dir(namespace, secret_name)
                        source = generated_mount_source(
                            args.configmap_data_dir,
                            secret_dir,
                        )
                        mounts.append(f"{source}:{mount_path}{mode}")
                    log.info(
                        "Materialized Secret volume service=%s secret=%s keys=%s",
                        service_name,
                        secret_name,
                        len(secret_files),
                    )
                elif not secret_ref.get("optional"):
                    state.warnings.append(f"{service_name} mounts missing Secret {secret_name}")
                continue
            if "emptyDir" in volume:
                compose_volume = compose_volume_name(service_name, volume_name)
                state.compose_volumes.setdefault(compose_volume, {})
                mounts.append(f"{compose_volume}:{mount_path}{mode}")
                continue
            if "persistentVolumeClaim" in volume:
                claim = as_str(as_dict(volume.get("persistentVolumeClaim")).get("claimName"))
                compose_volume = compose_volume_name(service_name, claim or volume_name)
                state.compose_volumes.setdefault(compose_volume, {})
                mounts.append(f"{compose_volume}:{mount_path}{mode}")
                continue
            if "hostPath" in volume:
                host_path = as_str(as_dict(volume.get("hostPath")).get("path"))
                if host_path:
                    mounts.append(f"{host_path}:{mount_path}{mode}")
                continue

        if volume_name in claim_names:
            compose_volume = compose_volume_name(service_name, volume_name)
            state.compose_volumes.setdefault(compose_volume, {})
            mounts.append(f"{compose_volume}:{mount_path}{mode}")

    if mounts:
        log.info(
            "Translated service volume mounts service=%s count=%s",
            service_name,
            len(mounts),
        )
    return unique_list(mounts)


def projected_volume_files(
    values: dict[str, str],
    items: list[Any],
    service_name: str,
    source_kind: str,
    source_name: str,
    state: ConversionState,
) -> list[tuple[str, str]]:
    """
    Return generated file paths and content for a ConfigMap or Secret volume.

    Args:
        values (dict[str, str]): Source key/value data.
        items (list[Any]): Optional Kubernetes volume ``items`` entries.
        service_name (str): Compose service name for diagnostics.
        source_kind (str): Kubernetes source kind, such as ``ConfigMap`` or ``Secret``.
        source_name (str): Kubernetes source name for diagnostics.
        state (ConversionState): Mutable compilation state for warnings.

    Returns:
        list[tuple[str, str]]: Pairs of projected path and file content.
    """
    if not items:
        return list(values.items())

    files: list[tuple[str, str]] = []
    for item in items:
        item_data = as_dict(item)
        key = as_str(item_data.get("key"))
        item_path = as_str(item_data.get("path")) or key
        if key not in values:
            state.warnings.append(
                f"{service_name} mounts missing {source_kind} key {source_name}/{key}"
            )
            continue
        files.append((item_path, values[key]))
    return files


def collect_resource_storage_volumes(
    container: dict[str, Any],
    service_name: str,
    environment: dict[str, str],
    state: ConversionState,
) -> list[str]:
    """
    Translate ephemeral-storage resources into bounded tmpfs volumes.

    Args:
        container (dict[str, Any]): Container spec to inspect.
        service_name (str): Compose service name.
        environment (dict[str, str]): Already translated service environment.
        state (ConversionState): Mutable compilation state that receives volume definitions.

    Returns:
        list[str]: Compose volume mounts for ephemeral container storage.
    """
    resources = as_dict(container.get("resources"))
    if not has_ephemeral_storage(resources):
        return []
    volume_name = compose_volume_name(service_name, "ephemeral-storage")
    mount_path = TMPFS_MOUNT_DEFAULT
    state.compose_volumes[volume_name] = tmpfs_volume_definition()
    log.info(
        "Translated ephemeral-storage resource service=%s volume=%s size=%s",
        service_name,
        volume_name,
        TMPFS_VOLUME_SIZE,
    )
    return [f"{volume_name}:{mount_path}"]


def has_ephemeral_storage(resources: dict[str, Any]) -> bool:
    """
    Return whether resources mention ephemeral-storage requests or limits.

    Args:
        resources (dict[str, Any]): Kubernetes resources block from a container.

    Returns:
        bool: True when ephemeral-storage is requested or limited.
    """
    requests = as_dict(resources.get("requests"))
    limits = as_dict(resources.get("limits"))
    return "ephemeral-storage" in requests or "ephemeral-storage" in limits


def tmpfs_volume_definition() -> dict[str, Any]:
    """
    Return the Compose volume definition for bounded tmpfs storage.

    Returns:
        dict[str, Any]: Compose named-volume definition using local tmpfs.
    """
    return {
        "driver": "local",
        "driver_opts": {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": f"size={TMPFS_VOLUME_SIZE}",
        },
    }


def service_base_name(workload_name: str, container: dict[str, Any]) -> str:
    """
    Derive a stable Compose service base name from workload/container names.

    Args:
        workload_name (str): Kubernetes workload name.
        container (dict[str, Any]): Container spec within the workload.

    Returns:
        str: Normalized Compose service base name.
    """
    container_name = as_str(container.get("name"))
    if not container_name or container_name == workload_name:
        return normalize_compose_name(workload_name)
    return normalize_compose_name(f"{workload_name}-{container_name}")


def unique_service_name(state: ConversionState, base_name: str) -> str:
    """
    Reserve and return a unique Compose service name.

    Args:
        state (ConversionState): Mutable compilation state with allocated names.
        base_name (str): Preferred Compose service name.

    Returns:
        str: Unique service name reserved in the compilation state.
    """
    name = base_name
    counter = 2
    while name in state.service_names:
        name = f"{base_name}-{counter}"
        counter += 1
    state.service_names.add(name)
    return name


def compose_volume_mounts(volumes: list[str]) -> list[ComposeVolume]:
    """
    Widen string volume mounts to the Compose volume mount union type.

    Args:
        volumes (list[str]): String volume mounts.

    Returns:
        list[ComposeVolume]: Volume mounts accepted by the Compose service model.
    """
    return list(volumes)


def unique_compose_volume_mounts(volumes: list[ComposeVolume]) -> list[ComposeVolume]:
    """
    Deduplicate Compose volume mounts while preserving order.

    Args:
        volumes (list[ComposeVolume]): Compose volume mount entries.

    Returns:
        list[ComposeVolume]: Unique volume mounts.
    """
    seen: set[Any] = set()
    result: list[ComposeVolume] = []
    for volume in volumes:
        signature = stable_signature(volume)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(volume)
    return result


def normalize_command(value: Any) -> Any:
    """
    Normalize Kubernetes command or args values for Compose output.

    Args:
        value (Any): Kubernetes command or args value.

    Returns:
        Any: Compose command value with scalar items stringified.
    """
    if isinstance(value, list):
        return [scalar_to_env_value(item) for item in value]
    return scalar_to_env_value(value)


def field_ref_value(field_path: str, namespace: str) -> str:
    """
    Approximate Kubernetes fieldRef values for local Compose containers.

    Args:
        field_path (str): Kubernetes field path requested by env ``fieldRef``.
        namespace (str): Namespace value to expose for metadata namespace refs.

    Returns:
        str: Local approximation for the requested field.
    """
    if field_path == "metadata.namespace":
        return namespace
    if field_path == "metadata.name":
        return ""
    if field_path == "status.podIP":
        return "127.0.0.1"
    return ""


def resource_ref_value(
    resource_ref: dict[str, Any],
    container: dict[str, Any],
) -> str:
    """
    Approximate Kubernetes resourceFieldRef values for Compose containers.

    Args:
        resource_ref (dict[str, Any]): Kubernetes ``resourceFieldRef`` data.
        container (dict[str, Any]): Container spec that owns the resource ref.

    Returns:
        str: Local resource value approximation.
    """
    resource = as_str(resource_ref.get("resource"))
    divisor = as_str(resource_ref.get("divisor"))
    resources = as_dict(container.get("resources"))
    value = resource_value(resources, resource)
    if value == "":
        return "0"
    return divide_quantity(value, divisor, resource)


def resource_value(resources: dict[str, Any], resource: str) -> str:
    """
    Look up a resource request or limit value from container resources.

    Args:
        resources (dict[str, Any]): Kubernetes container resources block.
        resource (str): Resource selector such as ``requests.cpu``.

    Returns:
        str: Resource quantity value, or an empty string when absent.
    """
    scope, _, name = resource.partition(".")
    if scope not in {"requests", "limits"} or not name:
        return ""
    return scalar_to_env_value(as_dict(resources.get(scope)).get(name))


def divide_quantity(value: str, divisor: str, resource: str) -> str:
    """
    Apply a Kubernetes resourceFieldRef divisor to a quantity value.

    Args:
        value (str): Kubernetes quantity value to divide.
        divisor (str): Kubernetes divisor quantity.
        resource (str): Resource selector used to pick quantity parsing rules.

    Returns:
        str: Divided quantity, or the original value when parsing fails.
    """
    if not divisor:
        return value
    try:
        numerator = parse_quantity(value, resource)
        denominator = parse_quantity(divisor, resource)
    except InvalidOperation:
        return value
    if denominator == 0:
        return value
    return format_decimal(numerator / denominator)


def parse_quantity(value: str, resource: str) -> Decimal:
    """
    Parse a small Kubernetes resource quantity into a decimal value.

    Args:
        value (str): Kubernetes quantity text.
        resource (str): Resource selector used to choose CPU or memory parsing.

    Returns:
        Decimal: Parsed decimal quantity.
    """
    text = value.strip()
    if text == "":
        return Decimal(0)
    if resource.endswith(".cpu"):
        return parse_cpu_quantity(text)
    return parse_memory_quantity(text)


def parse_cpu_quantity(value: str) -> Decimal:
    """
    Parse a CPU quantity into cores.

    Args:
        value (str): Kubernetes CPU quantity text.

    Returns:
        Decimal: CPU quantity expressed in cores.
    """
    suffix = value[-1:]
    if suffix in {"n", "u", "m"}:
        return Decimal(value[:-1]) * DECIMAL_QUANTITY_FACTORS[suffix]
    return Decimal(value)


def parse_memory_quantity(value: str) -> Decimal:
    """
    Parse a memory or storage quantity into bytes.

    Args:
        value (str): Kubernetes memory or storage quantity text.

    Returns:
        Decimal: Quantity expressed in bytes.
    """
    for suffix, factor in BINARY_QUANTITY_FACTORS.items():
        if value.endswith(suffix):
            return Decimal(value[: -len(suffix)]) * factor
    suffix = value[-1:]
    if suffix in DECIMAL_QUANTITY_FACTORS and suffix not in {"n", "u", "m"}:
        return Decimal(value[:-1]) * DECIMAL_QUANTITY_FACTORS[suffix]
    return Decimal(value)


def format_decimal(value: Decimal) -> str:
    """
    Format a Decimal without scientific notation or trailing zeroes.

    Args:
        value (Decimal): Decimal value to format.

    Returns:
        str: Plain decimal text.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def readiness_healthcheck(container: dict[str, Any]) -> dict[str, Any] | None:
    """
    Translate a Kubernetes readiness probe into a Compose healthcheck.

    Args:
        container (dict[str, Any]): Container spec to inspect.

    Returns:
        dict[str, Any] | None: Compose healthcheck block, or ``None`` when absent.
    """
    probe = as_dict(container.get("readinessProbe"))
    if not probe:
        return None
    test = readiness_probe_test(probe, container)
    if not test:
        return None
    healthcheck: dict[str, Any] = {"test": test}
    if "periodSeconds" in probe:
        healthcheck["interval"] = f"{probe['periodSeconds']}s"
    if "timeoutSeconds" in probe:
        healthcheck["timeout"] = f"{probe['timeoutSeconds']}s"
    if "failureThreshold" in probe:
        healthcheck["retries"] = probe["failureThreshold"]
    if "initialDelaySeconds" in probe:
        healthcheck["start_period"] = f"{probe['initialDelaySeconds']}s"
    return healthcheck


def readiness_probe_test(
    probe: dict[str, Any],
    container: dict[str, Any],
) -> list[str] | None:
    """
    Build a Compose healthcheck test from a readiness probe.

    Args:
        probe (dict[str, Any]): Kubernetes readiness probe.
        container (dict[str, Any]): Container spec that owns the probe.

    Returns:
        list[str] | None: Compose healthcheck command, or ``None`` when unsupported.
    """
    http_get = as_dict(probe.get("httpGet"))
    if http_get:
        probe_port = http_get.get("port")
        port = resolve_probe_port(probe_port, container)
        if not port:
            return None
        scheme = as_str(http_get.get("scheme", "HTTP")).lower()
        path = normalize_probe_path(as_str(http_get.get("path", "/")))
        host = as_str(http_get.get("host", "127.0.0.1")) or "127.0.0.1"
        shell_command = http_probe_shell_command(scheme, host, port, path)
        return ["CMD-SHELL", escape_compose_runtime_variables(shell_command)]

    tcp_socket = as_dict(probe.get("tcpSocket"))
    if tcp_socket:
        port = resolve_probe_port(tcp_socket.get("port"), container)
        if not port:
            return None
        return ["CMD-SHELL", f"nc -z 127.0.0.1 {shlex.quote(port)}"]

    exec_probe = as_dict(probe.get("exec"))
    command = [
        escape_compose_runtime_variables(as_str(item))
        for item in as_list(exec_probe.get("command"))
    ]
    if command:
        return ["CMD", *command]
    return None


def normalize_probe_path(path: str) -> str:
    """
    Normalize a Kubernetes HTTP probe path for Compose healthchecks.

    Args:
        path (str): Kubernetes HTTP probe path.

    Returns:
        str: HTTP path with a leading slash.
    """
    normalized = path or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def http_probe_shell_command(
    scheme: str,
    host: str,
    port: str,
    path: str,
) -> str:
    """
    Build a portable shell command for an HTTP readiness healthcheck.

    Args:
        scheme (str): HTTP scheme from the Kubernetes probe.
        host (str): Probe host.
        port (str): Probe port.
        path (str): Probe path.

    Returns:
        str: Shell command that tries curl, wget, then bash ``/dev/tcp``.
    """
    url = f"{scheme}://{host}:{port}{path}"
    bash_probe = bash_http_probe_script(scheme, host, port, path)
    return " ".join(
        [
            "if command -v curl >/dev/null 2>&1; then",
            f"curl -fsS -L --max-time 5 {shlex.quote(url)} >/dev/null;",
            "exit $?;",
            "fi;",
            "if command -v wget >/dev/null 2>&1; then",
            f"wget -q --spider {shlex.quote(url)};",
            "exit $?;",
            "fi;",
            "if command -v bash >/dev/null 2>&1; then",
            f"bash -ec {shlex.quote(bash_probe)};",
            "exit $?;",
            "fi;",
            "exit 1",
        ]
    )


def bash_http_probe_script(
    scheme: str,
    host: str,
    port: str,
    path: str,
) -> str:
    """
    Build a Bash fallback script for an HTTP readiness healthcheck.

    Args:
        scheme (str): HTTP scheme from the Kubernetes probe.
        host (str): Probe host.
        port (str): Probe port.
        path (str): Probe path.

    Returns:
        str: Bash script that checks HTTP status, or TCP reachability for HTTPS.
    """
    lines = [
        f"host={shlex.quote(host)}",
        f"port={shlex.quote(port)}",
        f"path={shlex.quote(path)}",
        'exec 3<>"/dev/tcp/${host}/${port}"',
    ]
    if scheme != "http":
        return "\n".join(lines)
    lines.extend(
        [
            "printf 'GET %s HTTP/1.1\\r\\nHost: %s\\r\\n"
            'Connection: close\\r\\n\\r\\n\' "$path" "$host" >&3',
            "mapfile -t -n 1 status_lines <&3",
            'status="$${status_lines[0]:-}"',
            'case "$status" in',
            '    HTTP/*" 2"*|HTTP/*" 3"*) exit 0 ;;',
            '    *) printf "%s\\n" "$status"; exit 1 ;;',
            "esac",
        ]
    )
    return "\n".join(lines)


def escape_compose_runtime_variables(value: str) -> str:
    """
    Escape container-runtime variable references for Compose rendering.

    Args:
        value (str): Command argument that may contain shell variable references.

    Returns:
        str: Command argument with runtime variables escaped for Docker Compose.
    """

    def escape_match(match: re.Match[str]) -> str:
        """
        Convert one Compose-interpolated variable into a runtime variable.

        Args:
            match (re.Match[str]): Matched shell variable reference.

        Returns:
            str: Escaped variable reference for Docker Compose.
        """
        braced_name, bare_name = match.groups()
        if braced_name:
            return "$${" + braced_name + "}"
        return "$$" + bare_name

    return COMPOSE_RUNTIME_VARIABLE_PATTERN.sub(escape_match, value)


def resolve_probe_port(value: Any, container: dict[str, Any]) -> str:
    """
    Resolve a readiness probe port value to a container port number.

    Args:
        value (Any): Probe port value, either a number or a named port.
        container (dict[str, Any]): Container spec containing named ports.

    Returns:
        str: Resolved numeric port, or an empty string when unresolved.
    """
    if isinstance(value, int):
        return str(value)
    port_name = as_str(value)
    if port_name.isdigit():
        return port_name
    for port in as_list(container.get("ports")):
        port_data = as_dict(port)
        if as_str(port_data.get("name")) == port_name:
            return as_str(port_data.get("containerPort"))
    return ""


def normalize_env_value(key: str, value: str, namespace: str) -> str:
    """
    Flatten runtime names and Kubernetes DNS values for Compose networking.

    Args:
        key (str): Environment variable name.
        value (str): Environment variable value.
        namespace (str): Namespace value to substitute for runtime identity variables.

    Returns:
        str: Normalized Compose environment value.
    """
    if key == "NAMESPACE" or key.endswith("_NAMESPACE"):
        return namespace
    value = strip_kubernetes_dns(value)
    value = value.replace(".svc.cluster.local", "")
    value = value.replace(".svc", "")
    return value


def compose_environment_reference(variable: str) -> str:
    """
    Return a Docker Compose environment interpolation reference.

    Args:
        variable (str): Environment variable name to reference.

    Returns:
        str: Compose interpolation expression for the variable.
    """
    return "${" + normalize_env_part(variable) + "}"


def strip_kubernetes_dns(value: str) -> str:
    """
    Strip simple Kubernetes service DNS suffixes from a value.

    Args:
        value (str): Environment value that may contain Kubernetes DNS.

    Returns:
        str: Value with simple service DNS suffixes removed.
    """
    match = re.fullmatch(
        (
            r"([a-z0-9][a-z0-9-]*)"
            r"(?:\.[a-z0-9][a-z0-9-]*)?"
            r"\.svc(?:\.cluster\.local)?"
        ),
        value,
    )
    if match:
        return match.group(1)
    return value


def remember_env_value(
    state: ConversionState,
    variable: str,
    value: str,
    source: str,
) -> str:
    """
    Store a secret value for later emission to ``.env.sh``.

    Args:
        state (ConversionState): Mutable compilation state.
        variable (str): Desired environment variable name.
        value (str): Secret value to export.
        source (str): Human-readable source used for collision diagnostics.

    Returns:
        str: Final environment variable name stored in compilation state.
    """
    variable = normalize_env_part(variable)
    existing = state.env_vars.get(variable)
    if existing not in (None, "") and existing != value and value:
        short_hash = abs(hash((source, value))) % 10000
        variable = f"{variable}_{short_hash}"
    if existing is None or value:
        state.env_vars[variable] = value
        state.env_source[variable] = source
    return variable


def add_depends_on(services: dict[str, ComposeService]) -> None:
    """
    Add simple Compose dependencies inferred from service host variables.

    Args:
        services (dict[str, ComposeService]): Mutable Compose service mapping.

    Returns:
        None: Acyclic inferred dependencies have been assigned to the services.
    """
    service_names = set(services)
    dependencies: dict[str, set[str]] = {name: set() for name in services}
    for name, service in services.items():
        candidates = inferred_service_dependencies(name, service, service_names)
        for dependency in sorted(candidates):
            if dependency_creates_cycle(name, dependency, dependencies):
                log.info(
                    "Dropping Compose dependency service=%s dependency=%s reason=cycle",
                    name,
                    dependency,
                )
                continue
            dependencies[name].add(dependency)
        if dependencies[name]:
            service.depends_on = sorted(dependencies[name])
            log.info(
                "Added Compose dependencies service=%s count=%s",
                name,
                len(dependencies[name]),
            )


def inferred_service_dependencies(
    name: str,
    service: ComposeService,
    service_names: set[str],
) -> set[str]:
    """
    Infer service dependencies from host-like environment variables.

    Args:
        name (str): Compose service name.
        service (ComposeService): Compose service to inspect.
        service_names (set[str]): Generated Compose service names.

    Returns:
        set[str]: Candidate dependency service names.
    """
    candidates: set[str] = set()
    for key, value in service.environment.items():
        if key.endswith("_SERVICE_HOST") and value in service_names and value != name:
            candidates.add(value)
    return candidates


def dependency_creates_cycle(
    service: str,
    dependency: str,
    dependencies: dict[str, set[str]],
) -> bool:
    """
    Return whether adding a dependency would create a Compose cycle.

    Args:
        service (str): Service that would receive a ``depends_on`` entry.
        dependency (str): Candidate dependency service.
        dependencies (dict[str, set[str]]): Already accepted dependency graph.

    Returns:
        bool: ``True`` when the candidate would create a cycle.
    """
    stack = [dependency]
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        if current == service:
            return True
        if current in visited:
            continue
        visited.add(current)
        stack.extend(dependencies.get(current, set()))
    return False


def build_compose_document(
    services: dict[str, ComposeService],
    volumes: dict[str, dict[str, Any]],
    network: str,
) -> ComposeDocument:
    """
    Assemble the top-level Compose document.

    Args:
        services (dict[str, ComposeService]): Compose services.
        volumes (dict[str, dict[str, Any]]): Named Compose volumes to include.
        network (str): Shared Compose network name.

    Returns:
        ComposeDocument: Compose document ready for YAML rendering.
    """
    document = ComposeDocument(
        services=services,
        networks=compose_networks(services, network),
        volumes={name: volumes[name] for name in sorted(volumes)},
    )
    log.info(
        "Built Compose document services=%s volumes=%s network=%s",
        len(services),
        len(volumes),
        network,
    )
    return document


def compose_networks(
    services: Mapping[str, ComposeService],
    default_network: str,
) -> dict[str, dict[str, Any]]:
    """
    Return top-level Compose networks referenced by services.

    Args:
        services (Mapping[str, ComposeService]): Compose services to inspect.
        default_network (str): Fallback network name to include.

    Returns:
        dict[str, dict[str, Any]]: Compose network definitions keyed by network name.
    """
    network_names = {default_network}
    for service in services.values():
        network_names.update(service.networks)
        network_names.update(service.network_aliases)
    return {network: {} for network in sorted(network_names)}


def dump_yaml(value: Any) -> str:
    """
    Render a value as YAML using ruamel.yaml.

    Args:
        value (Any): attrs model or plain Python value to render.

    Returns:
        str: YAML document text.
    """
    yaml = make_yaml()
    stream = io.StringIO()
    yaml.dump(
        add_yaml_anchors(literalize_multiline_strings(COMPOSE_CONVERTER.unstructure(value))),
        stream,
    )
    content = strip_trailing_whitespace(stream.getvalue())
    log.info("Rendered Compose YAML bytes=%s", len(content))
    return content


def strip_trailing_whitespace(value: str) -> str:
    """
    Remove trailing spaces introduced by YAML line wrapping.

    Args:
        value (str): Rendered YAML document text.

    Returns:
        str: YAML text with trailing line whitespace removed.
    """
    return "\n".join(line.rstrip() for line in value.splitlines()) + "\n"


def literalize_multiline_strings(value: Any) -> Any:
    """
    Mark multiline strings so ruamel.yaml emits literal blocks.

    Args:
        value (Any): Plain unstructured value.

    Returns:
        Any: Equivalent value with multiline strings wrapped for YAML output.
    """
    if isinstance(value, dict):
        return {key: literalize_multiline_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [literalize_multiline_strings(item) for item in value]
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value)
    return value


def add_yaml_anchors(value: Any) -> Any:
    """
    Add YAML anchors for repeated Compose blocks and environment values.

    Args:
        value (Any): Plain Compose document data.

    Returns:
        Any: ruamel.yaml comment-aware node with anchors applied.
    """
    node = to_commented_node(value)
    if isinstance(node, CommentedMap):
        anchor_service_networks(node)
        anchor_repeated_depends_on(node)
        anchor_repeated_environment_values(node)
        anchor_repeated_volume_definitions(node)
    return node


def to_commented_node(value: Any) -> Any:
    """
    Convert plain containers into ruamel comment-aware containers.

    Args:
        value (Any): Plain Python value to convert.

    Returns:
        Any: CommentedMap, CommentedSeq, or original scalar value.
    """
    if isinstance(value, Mapping):
        node = CommentedMap()
        for key, item in value.items():
            node[key] = to_commented_node(item)
        return node
    if isinstance(value, list):
        return CommentedSeq([to_commented_node(item) for item in value])
    return value


def anchor_service_networks(document: CommentedMap) -> None:
    """
    Anchor repeated service network lists.

    Args:
        document (CommentedMap): Compose document to update in place.

    Returns:
        None: Repeated network lists share YAML anchors.
    """
    services = document.get("services")
    if not isinstance(services, Mapping):
        return
    grouped: dict[tuple[str, ...], list[CommentedMap]] = {}
    for service in services.values():
        if not isinstance(service, CommentedMap):
            continue
        networks = service.get("networks")
        if isinstance(networks, list):
            grouped.setdefault(tuple(as_str(item) for item in networks), []).append(service)
    used: set[str] = set()
    for _networks, service_group in grouped.items():
        if len(service_group) < 2:
            continue
        anchor_name = unique_anchor_name("service_networks", used)
        shared = service_group[0]["networks"]
        shared.yaml_set_anchor(anchor_name, always_dump=True)
        for service in service_group[1:]:
            service["networks"] = shared
        log.info(
            "Added YAML anchor name=%s block=service_networks count=%s",
            anchor_name,
            len(service_group),
        )


def anchor_repeated_depends_on(document: CommentedMap) -> None:
    """
    Anchor repeated dependency blocks.

    Args:
        document (CommentedMap): Compose document to update in place.

    Returns:
        None: Repeated dependency mappings share YAML anchors.
    """
    services = document.get("services")
    if not isinstance(services, Mapping):
        return
    grouped: dict[Any, list[CommentedMap]] = {}
    for _name, service in services.items():
        if not isinstance(service, CommentedMap):
            continue
        depends_on = service.get("depends_on")
        if isinstance(depends_on, Mapping):
            grouped.setdefault(stable_signature(depends_on), []).append(service)

    used: set[str] = set()
    for service_group in grouped.values():
        if len(service_group) < 2:
            continue
        anchor_name = unique_anchor_name("service_depends_on", used)
        shared = service_group[0]["depends_on"]
        shared.yaml_set_anchor(anchor_name, always_dump=True)
        for service in service_group[1:]:
            service["depends_on"] = shared
        log.info(
            "Added YAML anchor name=%s block=service_depends_on count=%s",
            anchor_name,
            len(service_group),
        )


def anchor_repeated_environment_values(document: CommentedMap) -> None:
    """
    Anchor repeated environment key/value pairs across services.

    Args:
        document (CommentedMap): Compose document to update in place.

    Returns:
        None: Repeated environment values share YAML anchors.
    """
    services = document.get("services")
    if not isinstance(services, Mapping):
        return
    grouped: dict[tuple[str, str], list[CommentedMap]] = {}
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        environment = service.get("environment")
        if not isinstance(environment, CommentedMap):
            continue
        for key, item in environment.items():
            key_name = as_str(key)
            if should_anchor_environment_value(key_name, item):
                grouped.setdefault((key_name, item), []).append(environment)

    used: set[str] = set()
    for (key, item), environments in grouped.items():
        if len(environments) < 3:
            continue
        anchor_name = unique_anchor_name(f"env_{key}", used)
        shared = PlainScalarString(item)
        shared.yaml_set_anchor(anchor_name, always_dump=True)
        for environment in environments:
            environment[key] = shared
        log.info(
            "Added YAML anchor name=%s block=environment_value key=%s count=%s",
            anchor_name,
            key,
            len(environments),
        )


def should_anchor_environment_value(key: str, value: Any) -> bool:
    """
    Decide whether an environment scalar should be deduplicated with an anchor.

    Args:
        key (str): Environment variable name.
        value (Any): Environment variable value.

    Returns:
        bool: True when the value can be anchored safely for readability.
    """
    if key in UNANCHORED_ENVIRONMENT_KEYS:
        return False
    return isinstance(value, str) and "\n" not in value and value != ""


def anchor_repeated_volume_definitions(document: CommentedMap) -> None:
    """
    Anchor repeated top-level volume definitions.

    Args:
        document (CommentedMap): Compose document to update in place.

    Returns:
        None: Repeated volume definitions share YAML anchors.
    """
    volumes = document.get("volumes")
    if not isinstance(volumes, CommentedMap):
        return
    grouped: dict[Any, list[str]] = {}
    for name, definition in volumes.items():
        grouped.setdefault(stable_signature(definition), []).append(as_str(name))

    used: set[str] = set()
    for names in grouped.values():
        if len(names) < 2:
            continue
        anchor_name = unique_anchor_name("volume_definition", used)
        shared = volumes[names[0]]
        shared.yaml_set_anchor(anchor_name, always_dump=True)
        for name in names[1:]:
            volumes[name] = shared
        log.info(
            "Added YAML anchor name=%s block=volume_definition count=%s",
            anchor_name,
            len(names),
        )


def stable_signature(value: Any) -> Any:
    """
    Build a hashable signature for a YAML node.

    Args:
        value (Any): YAML node value to represent as a hashable signature.

    Returns:
        Any: Hashable signature for mapping, sequence, or scalar input.
    """
    if isinstance(value, Mapping):
        return tuple((key, stable_signature(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(stable_signature(item) for item in value)
    return value


def unique_anchor_name(base_name: str, used: set[str]) -> str:
    """
    Return a unique YAML anchor name from a suggested base name.

    Args:
        base_name (str): Suggested anchor name before normalization.
        used (set[str]): Anchor names already allocated.

    Returns:
        str: Unique YAML anchor name.
    """
    name = normalize_anchor_name(base_name)
    candidate = name
    counter = 2
    while candidate in used:
        candidate = f"{name}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def normalize_anchor_name(value: str) -> str:
    """
    Convert arbitrary text into a YAML-anchor-friendly name.

    Args:
        value (str): Raw anchor name text.

    Returns:
        str: YAML-anchor-friendly name.
    """
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    if not value:
        return "anchor"
    if value[0].isdigit():
        return f"anchor_{value}"
    return value


def generated_config_path(namespace: str, config_name: str, key: str) -> Path:
    """
    Return the relative path for a generated ConfigMap key file.

    Args:
        namespace (str): Kubernetes namespace lookup key.
        config_name (str): ConfigMap name.
        key (str): ConfigMap key.

    Returns:
        Path: Relative generated file path for the ConfigMap key.
    """
    return generated_config_dir(namespace, config_name) / normalized_relative_path(key)


def generated_config_dir(namespace: str, config_name: str) -> Path:
    """
    Return the relative directory for generated ConfigMap files.

    Args:
        namespace (str): Kubernetes namespace lookup key.
        config_name (str): ConfigMap name.

    Returns:
        Path: Relative generated directory path for ConfigMap data.
    """
    if namespace == "__single__":
        return Path("configmaps") / normalize_path_part(config_name)
    return Path("configmaps") / normalize_path_part(namespace) / normalize_path_part(config_name)


def generated_secret_path(namespace: str, secret_name: str, key: str) -> Path:
    """
    Return the relative path for a generated Secret key file.

    Args:
        namespace (str): Kubernetes namespace lookup key.
        secret_name (str): Secret name.
        key (str): Secret key.

    Returns:
        Path: Relative generated file path for the Secret key.
    """
    return generated_secret_dir(namespace, secret_name) / normalized_relative_path(key)


def generated_secret_dir(namespace: str, secret_name: str) -> Path:
    """
    Return the relative directory for generated Secret files.

    Args:
        namespace (str): Kubernetes namespace lookup key.
        secret_name (str): Secret name.

    Returns:
        Path: Relative generated directory path for Secret data.
    """
    if namespace == "__single__":
        return Path("secrets") / normalize_path_part(secret_name)
    return Path("secrets") / normalize_path_part(namespace) / normalize_path_part(secret_name)


def normalized_relative_path(value: str) -> Path:
    """
    Convert a projected volume path into safe relative path components.

    Args:
        value (str): Raw projected file path from a volume item.

    Returns:
        Path: File-safe relative path.
    """
    parts = [normalize_path_part(part) for part in value.split("/") if part not in ("", ".", "..")]
    if not parts:
        return Path("value")
    return Path(*parts)


def generated_mount_source(generated_dir: str, relative_path: Path) -> str:
    """
    Return a Compose mount source for a generated support-data path.

    Args:
        generated_dir (str): Root generated-data directory.
        relative_path (Path): Relative generated file path.

    Returns:
        str: Compose bind-mount source path.
    """
    source = Path(generated_dir) / relative_path
    if source.is_absolute():
        return source.as_posix()
    return f"./{source.as_posix()}"


def compose_volume_name(service_name: str, volume_name: str) -> str:
    """
    Build a valid Compose named volume from service and volume names.

    Args:
        service_name (str): Compose service name.
        volume_name (str): Kubernetes volume name.

    Returns:
        str: Normalized Compose named volume.
    """
    return normalize_compose_name(f"{service_name}-{volume_name}")
