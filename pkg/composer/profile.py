"""
Apply declarative runtime choices and bindings to typed Kubernetes inputs.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

import attrs
import networkx as nx

from composer.exceptions import CompilationError

if TYPE_CHECKING:
    from composer.ast.kubernetes import Resource

log = logging.getLogger(__name__)


@attrs.frozen
class Context:
    """
    Provide explicit input trees available to runtime bindings.

    Attributes:
        resource (dict[str, object]): Selected Kubernetes resource.
        container (dict[str, object]): Selected regular or init container.
        values (dict[str, object]): Coalesced chart values supplied to Helm.
    """

    resource: dict[str, object]
    container: dict[str, object]
    values: dict[str, object]


def mapping(value: object) -> dict[str, object]:
    """
    Require an object with string keys at the input boundary.

    Args:
        value (object): Decoded YAML or JSON value.

    Returns:
        dict[str, object]: Validated mapping.

    Raises:
        CompilationError: The value is not an object with string keys.
    """
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CompilationError("Expected an object with string keys")
    return {str(key): item for key, item in value.items()}


def pointer(value: object, path: str) -> object:
    """
    Resolve a JSON Pointer without evaluating code or silently missing fields.

    Args:
        value (object): Source object or array.
        path (str): RFC 6901 pointer, including escaped slash and tilde segments.

    Returns:
        object: Referenced value.

    Raises:
        CompilationError: The pointer is malformed or cannot be resolved.
    """
    if path == "":
        return value
    if not path.startswith("/"):
        raise CompilationError(f"JSON pointer must begin with '/': {path}")
    try:
        for raw in path[1:].split("/"):
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not part.isdecimal():
                    raise CompilationError(f"Invalid array index in {path}")
                value = value[int(part)]
            else:
                value = mapping(value)[part]
        return value
    except (KeyError, IndexError) as error:
        raise CompilationError(f"Unresolved JSON pointer: {path}") from error


def bind(value: object, context: Context) -> object:
    """
    Resolve explicit input references inside a Compose overlay.

    Args:
        value (object): Literal or tree containing binding objects.
        context (Context): Selected resource, container, and chart values.

    Returns:
        object: Resolved deep copy, preserving ordinary Compose interpolation strings.

    Raises:
        CompilationError: A binding has invalid operands or an unresolved pointer.
    """
    if isinstance(value, list):
        return [bind(item, context) for item in value]
    if not isinstance(value, dict):
        return value
    obj = mapping(value)
    for key, tree in (
        ("$resource", context.resource),
        ("$container", context.container),
        ("$values", context.values),
    ):
        if set(obj) == {key}:
            reference = obj[key]
            if not isinstance(reference, str):
                raise CompilationError(f"{key} requires a JSON pointer string")
            return copy.deepcopy(pointer(tree, reference))
    if set(obj) == {"$string"}:
        result = bind(obj["$string"], context)
        if isinstance(result, dict | list) or result is None:
            raise CompilationError("$string requires a scalar")
        return str(result).lower() if isinstance(result, bool) else str(result)
    if set(obj) == {"$concat"}:
        operands = obj["$concat"]
        if not isinstance(operands, list):
            raise CompilationError("$concat requires a list")
        return "".join(str(bind({"$string": part}, context)) for part in operands)
    if any(key.startswith("$") for key in obj):
        raise CompilationError("Unknown or malformed binding object")
    return {key: bind(item, context) for key, item in obj.items()}


def merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """
    Merge objects recursively while replacing scalar values and lists.

    Args:
        base (dict[str, object]): Compiled service or default values.
        overlay (dict[str, object]): Explicit runtime changes.

    Returns:
        dict[str, object]: Independent merged tree; neither input is modified.
    """
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(mapping(result[key]), mapping(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


def remove(value: dict[str, object], paths: list[str]) -> dict[str, object]:
    """
    Remove fields declared in a runtime profile using JSON Pointers.

    Args:
        value (dict[str, object]): Compiled service.
        paths (list[str]): Object fields that have no equivalent in the target runtime.

    Returns:
        dict[str, object]: Independent tree with those fields removed when present.

    Raises:
        CompilationError: A removal path is not an object-field JSON pointer.
    """
    result = copy.deepcopy(value)
    for path in paths:
        if not path.startswith("/") or path == "/":
            raise CompilationError(f"Invalid removal pointer: {path}")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        target = result
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                break
            target = child
        else:
            target.pop(parts[-1], None)
    return result


def select_resource(resources: list[Resource], selector: dict[str, object]) -> Resource:
    """
    Select exactly one Kubernetes resource for a declared Compose service.

    Args:
        resources (list[Resource]): Rendered chart objects.
        selector (dict[str, object]): Kind, name, optional namespace and label constraints.

    Returns:
        Resource: Unique source resource.

    Raises:
        CompilationError: No source or multiple sources match the selector.
    """
    matches = []
    for resource in resources:
        obj = resource.obj
        if obj.kind != selector["kind"]:
            continue
        if "name" in selector and obj.metadata.get("name") != selector["name"]:
            continue
        if (
            "namespace" in selector
            and obj.metadata.get("namespace", "default") != selector["namespace"]
        ):
            continue
        labels = mapping(obj.metadata.get("labels", {}))
        if any(
            labels.get(key) != value for key, value in mapping(selector.get("labels", {})).items()
        ):
            continue
        matches.append(resource)
    if len(matches) != 1:
        raise CompilationError(f"Expected one source for {selector}; found {len(matches)}")
    return matches[0]


def validate_dependencies(document: dict[str, object]) -> None:
    """
    Reject missing dependencies, impossible health gates, and startup cycles.

    Args:
        document (dict[str, object]): Complete Compose document.

    Returns:
        None: Dependencies reference services with compatible startup conditions.

    Raises:
        CompilationError: A dependency is missing, cyclic, or cannot satisfy its condition.
    """
    services = mapping(document["services"])
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(services)
    for name, raw in services.items():
        dependencies = mapping(mapping(raw).get("depends_on", {}))
        for dependency, condition in dependencies.items():
            if dependency not in services:
                raise CompilationError(f"{name} depends on missing service {dependency}")
            if mapping(condition).get("condition") == "service_healthy":
                if "healthcheck" not in mapping(services[dependency]):
                    raise CompilationError(f"{dependency} has no healthcheck required by {name}")
            graph.add_edge(dependency, name)
    if not nx.is_directed_acyclic_graph(graph):
        raise CompilationError("Compose startup dependencies contain a cycle")
