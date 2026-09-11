"""
Kubernetes manifest parsing and resource indexing.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

import attrs
from cattrs import Converter

from composer.utils import (
    as_dict,
    as_list,
    as_str,
    is_non_string_sequence,
    load_documents,
    make_yaml,
    normalize_env_part,
    relative_to_cwd,
    scalar_to_env_value,
    to_plain_data,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


log = logging.getLogger(__name__)


WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Pod",
}
ENV_SECRET_PATTERNS = (
    "SECRET",
    "PASSWORD",
    "PASS",
    "TOKEN",
    "KEY",
    "CREDENTIAL",
    "APIKEY",
    "API_KEY",
    "CLIENTSECRET",
    "CLIENT_SECRET",
    "CERT",
    "CRT",
)


@attrs.define(frozen=True)
class SourceLocation:
    """
    Identifies where a parsed Kubernetes resource came from.

    Attributes:
        file (Path): Manifest file path.
        doc_index (int): Zero-based document index within the manifest file.
    """

    file: Path
    doc_index: int


@attrs.define(frozen=True)
class KubernetesObject:
    """
    Stores a typed view plus raw data for a Kubernetes manifest object.

    Attributes:
        raw (dict[str, object]): Raw manifest mapping.
        api_version (str): Kubernetes API version.
        kind (str): Kubernetes resource kind.
        metadata (dict[str, object]): Metadata mapping.
        spec (dict[str, object]): Spec mapping.
        data (dict[str, object]): Data mapping for ConfigMaps and Secrets.
        string_data (dict[str, object]): Secret stringData mapping.
    """

    raw: dict[str, object]
    api_version: str = ""
    kind: str = ""
    metadata: dict[str, object] = attrs.field(factory=dict)
    spec: dict[str, object] = attrs.field(factory=dict)
    data: dict[str, object] = attrs.field(factory=dict)
    string_data: dict[str, object] = attrs.field(factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Return a raw Kubernetes field value.

        Args:
            key (str): Kubernetes object field name.
            default (Any): Fallback returned when the field is absent.

        Returns:
            Any: The raw field value or the provided default.
        """
        return self.raw.get(key, default)


@attrs.define(frozen=True)
class Resource:
    """
    Wraps a parsed Kubernetes object with its source location.

    Attributes:
        obj (KubernetesObject): Parsed Kubernetes object.
        source (SourceLocation): Manifest source location.
    """

    obj: KubernetesObject
    source: SourceLocation


@attrs.define
class ConfigData:
    """
    Stores flattened ConfigMap keys for environment and file mounts.

    Attributes:
        values (dict[str, str]): ConfigMap key/value pairs.
        source (SourceLocation | None): Manifest source location.
    """

    values: dict[str, str] = attrs.field(factory=dict)
    source: SourceLocation | None = None


@attrs.define
class SecretData:
    """
    Stores decoded Secret keys for environment substitution.

    Attributes:
        values (dict[str, str]): Decoded Secret key/value pairs.
        source (SourceLocation | None): Manifest source location.
    """

    values: dict[str, str] = attrs.field(factory=dict)
    source: SourceLocation | None = None


@attrs.define(frozen=True)
class ServicePortData:
    """
    Stores one Kubernetes Service port mapping.

    Attributes:
        name (str): Kubernetes Service port name.
        port (str): Exposed Service port.
        target_port (str): Container-facing target port.
    """

    name: str
    port: str
    target_port: str


@attrs.define
class ServiceData:
    """
    Stores Kubernetes Service ports for ingress backend resolution.

    Attributes:
        name (str): Kubernetes Service name.
        service_type (str): Kubernetes Service type.
        external_name (str): ExternalName target hostname.
        selector (dict[str, str]): Service selector labels.
        ports (list[ServicePortData]): Service port mappings.
        source (SourceLocation | None): Manifest source location.
    """

    name: str = ""
    service_type: str = ""
    external_name: str = ""
    selector: dict[str, str] = attrs.field(factory=dict)
    ports: list[ServicePortData] = attrs.field(factory=list)
    source: SourceLocation | None = None


def make_converter() -> Converter:
    """
    Create the cattrs converter for Kubernetes input models.

    Returns:
        Converter: Configured converter for Kubernetes manifest objects.
    """
    converter = Converter()
    converter.register_structure_hook(KubernetesObject, structure_kubernetes_object)
    return converter


def structure_kubernetes_object(
    value: Any,
    _type: type[KubernetesObject],
) -> KubernetesObject:
    """
    Structure raw YAML data into a minimal Kubernetes object model.

    Args:
        value (Any): Raw value loaded by ruamel.yaml.
        _type (type[KubernetesObject]): Target type supplied by cattrs.

    Returns:
        KubernetesObject: Kubernetes object with common fields lifted out for convenience.
    """
    raw = as_dict(to_plain_data(value))
    return KubernetesObject(
        raw=raw,
        api_version=as_str(raw.get("apiVersion")),
        kind=as_str(raw.get("kind")),
        metadata=as_dict(raw.get("metadata")),
        spec=as_dict(raw.get("spec")),
        data=as_dict(raw.get("data")),
        string_data=as_dict(raw.get("stringData")),
    )


KUBERNETES_CONVERTER = make_converter()


def read_resources(paths: list[Path]) -> list[Resource]:
    """
    Load rendered Kubernetes YAML documents from manifest files.

    Args:
        paths (list[Path]): Manifest files to parse.

    Returns:
        list[Resource]: Parsed resources with source locations.
    """
    resources: list[Resource] = []
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        path_resource_count = 0
        dropped_doc_count = 0
        kind_counts: dict[str, int] = {}
        log.info("Reading Kubernetes manifest path=%s", relative_to_cwd(path))
        for index, raw_doc in enumerate(load_documents(path.read_text())):
            if not isinstance(raw_doc, Mapping) or not raw_doc:
                dropped_doc_count += 1
                continue
            obj = KUBERNETES_CONVERTER.structure(raw_doc, KubernetesObject)
            path_resource_count += 1
            kind_counts[obj.kind or "Unknown"] = kind_counts.get(obj.kind or "Unknown", 0) + 1
            resources.append(
                Resource(
                    obj=obj,
                    source=SourceLocation(file=path, doc_index=index),
                )
            )
        log.info(
            "Loaded manifest path=%s resources=%s dropped_docs=%s kinds=%s",
            relative_to_cwd(path),
            path_resource_count,
            dropped_doc_count,
            format_counts(kind_counts),
        )
    log.info("Loaded Kubernetes resources total=%s", len(resources))
    return resources


def collect_configmaps(
    resources: list[Resource],
    single_namespace: bool,
) -> dict[tuple[str, str], ConfigData]:
    """
    Index ConfigMap resources by namespace and name.

    Args:
        resources (list[Resource]): Parsed Kubernetes resources.
        single_namespace (bool): Whether namespace names should be flattened.

    Returns:
        dict[tuple[str, str], ConfigData]: ConfigMap data indexed by lookup key.
    """
    configmaps: dict[tuple[str, str], ConfigData] = {}
    for resource in resources:
        obj = resource.obj
        if obj.get("kind") != "ConfigMap":
            continue
        metadata = as_dict(obj.get("metadata"))
        name = as_str(metadata.get("name"))
        namespace = namespace_key(metadata, single_namespace)
        data = as_dict(obj.get("data"))
        configmaps[(namespace, name)] = ConfigData(
            values={key: scalar_to_env_value(value) for key, value in data.items()},
            source=resource.source,
        )
    log.info("Indexed ConfigMaps count=%s", len(configmaps))
    return configmaps


def collect_secrets(
    resources: list[Resource],
    single_namespace: bool,
) -> dict[tuple[str, str], SecretData]:
    """
    Index Secret resources by namespace and name.

    Args:
        resources (list[Resource]): Parsed Kubernetes resources.
        single_namespace (bool): Whether namespace names should be flattened.

    Returns:
        dict[tuple[str, str], SecretData]: Decoded Secret data indexed by lookup key.
    """
    secrets: dict[tuple[str, str], SecretData] = {}
    for resource in resources:
        obj = resource.obj
        if obj.get("kind") != "Secret":
            continue
        metadata = as_dict(obj.get("metadata"))
        name = as_str(metadata.get("name"))
        namespace = namespace_key(metadata, single_namespace)
        values: dict[str, str] = {}
        for key, value in as_dict(obj.get("stringData")).items():
            values[key] = scalar_to_env_value(value)
        for key, value in as_dict(obj.get("data")).items():
            raw_value = scalar_to_env_value(value)
            values[key] = decode_secret_value(raw_value)
        secrets[(namespace, name)] = SecretData(values=values, source=resource.source)
    log.info("Indexed Secrets count=%s", len(secrets))
    return secrets


def collect_services(
    resources: list[Resource],
    single_namespace: bool,
) -> dict[tuple[str, str], ServiceData]:
    """
    Index Service resources by namespace and name.

    Args:
        resources (list[Resource]): Parsed Kubernetes resources.
        single_namespace (bool): Whether namespace names should be flattened.

    Returns:
        dict[tuple[str, str], ServiceData]: Service data indexed by lookup key.
    """
    services: dict[tuple[str, str], ServiceData] = {}
    for resource in resources:
        obj = resource.obj
        if obj.get("kind") != "Service":
            continue
        metadata = as_dict(obj.get("metadata"))
        name = as_str(metadata.get("name"))
        namespace = namespace_key(metadata, single_namespace)
        spec = as_dict(obj.get("spec"))
        key = (namespace, name)
        service = ServiceData(
            name=name,
            service_type=as_str(spec.get("type")) or "ClusterIP",
            external_name=as_str(spec.get("externalName")),
            selector={
                as_str(key): scalar_to_env_value(value)
                for key, value in as_dict(spec.get("selector")).items()
            },
            ports=collect_service_ports(obj),
            source=resource.source,
        )
        previous_service = services.get(key)
        if previous_service and keep_existing_service(previous_service, service):
            log.info(
                (
                    "Keeping Kubernetes Service index entry namespace=%s name=%s "
                    "existing_type=%s dropped_type=%s reason=prefer-concrete-service"
                ),
                namespace,
                name,
                previous_service.service_type,
                service.service_type,
            )
            continue
        services[key] = service
    log.info("Indexed Services count=%s", len(services))
    return services


def keep_existing_service(
    existing_service: ServiceData,
    candidate_service: ServiceData,
) -> bool:
    """
    Decide whether an indexed Service should survive a duplicate Service name.

    Args:
        existing_service (ServiceData): Service already indexed for a namespace/name.
        candidate_service (ServiceData): Later Service with the same namespace/name.

    Returns:
        bool: True when the existing Service should be kept.
    """
    return (
        existing_service.service_type != "ExternalName"
        and candidate_service.service_type == "ExternalName"
    )


def collect_service_ports(obj: KubernetesObject) -> list[ServicePortData]:
    """
    Collect port mappings from one Kubernetes Service.

    Args:
        obj (KubernetesObject): Service object to inspect.

    Returns:
        list[ServicePortData]: Service port mappings found in the object.
    """
    ports: list[ServicePortData] = []
    for port in as_list(as_dict(obj.get("spec")).get("ports")):
        port_data = as_dict(port)
        service_port = scalar_to_env_value(port_data.get("port"))
        if not service_port:
            continue
        target_port = scalar_to_env_value(port_data.get("targetPort")) or service_port
        ports.append(
            ServicePortData(
                name=as_str(port_data.get("name")),
                port=service_port,
                target_port=target_port,
            )
        )
    return ports


def collect_source_secret_values(paths: list[Path]) -> dict[str, str]:
    """
    Extract secret-like values from Helm values files.

    Args:
        paths (list[Path]): Values files to scan.

    Returns:
        dict[str, str]: Environment variable names mapped to source values.
    """
    yaml = make_yaml(allow_duplicate_keys=True)
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            log.info(
                "Dropping values file path=%s reason=missing",
                relative_to_cwd(path),
            )
            continue
        before = len(values)
        log.info("Scanning values file for secret-like leaves path=%s", relative_to_cwd(path))
        loaded = yaml.load(path.read_text())
        collect_secret_values_from_node(to_plain_data(loaded), [], values)
        log.info(
            "Translated values file secret-like leaves path=%s count=%s",
            relative_to_cwd(path),
            len(values) - before,
        )
    log.info("Collected source secret-like values count=%s", len(values))
    return values


def collect_secret_values_from_node(
    value: Any,
    path: list[str],
    values: dict[str, str],
) -> None:
    """
    Collect secret-like leaves from a nested values document.

    Args:
        value (Any): Current value being inspected.
        path (list[str]): Normalized key path to the current value.
        values (dict[str, str]): Mutable output environment variable mapping.

    Returns:
        None: Secret-like leaves have been added to the supplied mapping.
    """
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            next_path = [*path, normalize_env_part(key)]
            if isinstance(item, Mapping) or is_non_string_sequence(item):
                collect_secret_values_from_node(item, next_path, values)
            elif is_secretish_key(key) and item is not None:
                values["_".join(next_path)] = scalar_to_env_value(item)
        return

    if is_non_string_sequence(value):
        for item in value:
            if isinstance(item, Mapping) or is_non_string_sequence(item):
                collect_secret_values_from_node(item, path, values)


def workload_pod_spec(obj: KubernetesObject) -> dict[str, Any]:
    """
    Return the pod spec nested inside a supported workload object.

    Args:
        obj (KubernetesObject): Workload object to inspect.

    Returns:
        dict[str, Any]: Nested pod spec, or an empty dictionary when unsupported.
    """
    kind = workload_kind(obj)
    spec = as_dict(obj.get("spec"))
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return as_dict(as_dict(as_dict(spec.get("template")).get("spec")))
    if kind == "CronJob":
        job_template = as_dict(as_dict(spec.get("jobTemplate")).get("spec"))
        return as_dict(as_dict(as_dict(job_template.get("template")).get("spec")))
    if kind == "Pod":
        return spec
    return {}


def workload_kind(obj: KubernetesObject) -> str:
    """
    Return the Kubernetes kind for a workload object.

    Args:
        obj (KubernetesObject): Kubernetes object to inspect.

    Returns:
        str: Kubernetes ``kind`` value.
    """
    return as_str(obj.get("kind"))


def stateful_claim_names(obj: KubernetesObject) -> set[str]:
    """
    Return volume claim template names from a StatefulSet.

    Args:
        obj (KubernetesObject): Kubernetes workload object to inspect.

    Returns:
        set[str]: StatefulSet volume claim template names.
    """
    if workload_kind(obj) != "StatefulSet":
        return set()
    names: set[str] = set()
    for claim in as_list(as_dict(obj.get("spec")).get("volumeClaimTemplates")):
        name = as_str(as_dict(claim).get("metadata", {}).get("name"))
        if name:
            names.add(name)
    return names


def lookup_config(
    configmaps: dict[tuple[str, str], ConfigData],
    namespace: str,
    name: str,
) -> ConfigData | None:
    """
    Find a ConfigMap by namespace and name, falling back to any namespace.

    Args:
        configmaps (dict[tuple[str, str], ConfigData]): Indexed ConfigMap data.
        namespace (str): Namespace lookup key.
        name (str): ConfigMap name to find.

    Returns:
        ConfigData | None: Matching ConfigMap data, or ``None`` when absent.
    """
    return configmaps.get((namespace, name)) or first_named_resource(configmaps, name)


def lookup_secret(
    secrets: dict[tuple[str, str], SecretData],
    namespace: str,
    name: str,
) -> SecretData | None:
    """
    Find a Secret by namespace and name, falling back to any namespace.

    Args:
        secrets (dict[tuple[str, str], SecretData]): Indexed Secret data.
        namespace (str): Namespace lookup key.
        name (str): Secret name to find.

    Returns:
        SecretData | None: Matching Secret data, or ``None`` when absent.
    """
    return secrets.get((namespace, name)) or first_named_resource(secrets, name)


def lookup_service(
    services: Mapping[tuple[str, str], ServiceData],
    namespace: str,
    name: str,
) -> ServiceData | None:
    """
    Find a Service by namespace and name, falling back to any namespace.

    Args:
        services (Mapping[tuple[str, str], ServiceData]): Indexed Service data.
        namespace (str): Namespace lookup key.
        name (str): Service name to find.

    Returns:
        ServiceData | None: Matching Service data, or ``None`` when absent.
    """
    return services.get((namespace, name)) or first_named_resource(services, name)


def first_named_resource(
    resources: Mapping[tuple[str, str], Any],
    name: str,
) -> Any | None:
    """
    Return the first resource whose lookup key has the requested name.

    Args:
        resources (Mapping[tuple[str, str], Any]): Resources keyed by namespace and name.
        name (str): Resource name to find.

    Returns:
        Any | None: Matching resource value, or ``None`` when absent.
    """
    for (_namespace, resource_name), resource in resources.items():
        if resource_name == name:
            return resource
    return None


def namespace_key(metadata: dict[str, Any], single_namespace: bool) -> str:
    """
    Return the resource namespace lookup key for compilation.

    Args:
        metadata (dict[str, Any]): Kubernetes resource metadata.
        single_namespace (bool): Whether all manifests are flattened into one namespace.

    Returns:
        str: Namespace lookup key used by the compiler.
    """
    if single_namespace:
        return "__single__"
    return as_str(metadata.get("namespace", "default")) or "default"


def secret_env_name(secret_name: str, secret_key: str) -> str:
    """
    Build the host environment variable name for a Secret key.

    Args:
        secret_name (str): Kubernetes Secret name.
        secret_key (str): Key within the Kubernetes Secret.

    Returns:
        str: Normalized host environment variable name.
    """
    return normalize_env_part(f"{secret_name}_{secret_key}")


def decode_secret_value(value: str) -> str:
    """
    Decode Kubernetes Secret data when it is valid base64 UTF-8.

    Args:
        value (str): Kubernetes Secret value to decode.

    Returns:
        str: Decoded UTF-8 value, or the original value when decoding fails.
    """
    if not value:
        return ""
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return value


def is_secretish_key(key: str) -> bool:
    """
    Return whether a key name likely contains a secret value.

    Args:
        key (str): Key name to classify.

    Returns:
        bool: True when the key name looks secret-like.
    """
    normalized = normalize_env_part(key)
    return any(pattern in normalized for pattern in ENV_SECRET_PATTERNS)


def describe_resource(resource: Resource) -> str:
    """
    Return a compact human-readable description of a resource.

    Args:
        resource (Resource): Parsed resource to describe.

    Returns:
        str: Resource kind/name and source file.
    """
    metadata = as_dict(resource.obj.get("metadata"))
    return (
        f"{resource.obj.get('kind')}/{metadata.get('name')} from "
        f"{relative_to_cwd(resource.source.file)}"
    )


def format_counts(counts: dict[str, int]) -> str:
    """
    Return a compact ``name=count`` summary for logging.

    Args:
        counts (dict[str, int]): Mapping of names to counts.

    Returns:
        str: Comma-separated count summary.
    """
    return ",".join(f"{key}={value}" for key, value in sorted(counts.items()))
