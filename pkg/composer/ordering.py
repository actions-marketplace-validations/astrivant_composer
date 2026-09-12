"""
Project declarative GitOps ordering onto the selected Compose services.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import networkx as nx

from composer.exceptions import CompilationError
from composer.profile import mapping
from composer.utils import as_list

if TYPE_CHECKING:
    from composer.ast.kubernetes import Resource

log = logging.getLogger(__name__)
ARGO = "argocd.argoproj.io/"
FLUX = {"Kustomization": "kustomize.toolkit.fluxcd.io", "HelmRelease": "helm.toolkit.fluxcd.io"}


def startup_resource(resource: Resource) -> bool:
    """
    Exclude hooks that do not participate in successful startup.

    Args:
        resource (Resource): Input resource.

    Returns:
        bool: Whether the resource participates in startup.
    """
    annotations = mapping(resource.obj.metadata.get("annotations", {}))
    hooks = {part.strip() for part in str(annotations.get(ARGO + "hook", "Sync")).split(",")}
    if "Skip" in hooks or not hooks.intersection({"PreSync", "Sync", "PostSync"}):
        log.warning("Excluding non-startup Argo hook %s", resource.obj.metadata.get("name"))
        return False
    if len(hooks) != 1:
        raise CompilationError("Multiple Argo hook phases cannot be represented by one service")
    return True


def apply_ordering(
    resources: list[Resource], sources: dict[str, Resource], services: dict[str, object]
) -> None:
    """
    Expand controller dependencies and phase/wave barriers into Compose conditions.

    Args:
        resources (list[Resource]): Inputs including GitOps controller objects.
        sources (dict[str, Resource]): Selected source for each Compose service.
        services (dict[str, object]): Mutable generated services after profile overrides.

    Returns:
        None: Native ordering has been merged into the services.
    """

    def identity(resource: Resource) -> tuple[str, str, str, str]:
        obj = resource.obj
        return (
            obj.api_version.split("/")[0],
            obj.kind,
            str(obj.metadata.get("namespace", "default")),
            str(obj.metadata.get("name", "")),
        )

    indexed = {identity(resource): i for i, resource in enumerate(resources)}
    if len(indexed) != len(resources):
        raise CompilationError("Duplicate resource identity in ordering inputs")
    members = {
        i: {name for name, source in sources.items() if source is resource}
        for i, resource in enumerate(resources)
    }
    ownership: nx.DiGraph[int] = nx.DiGraph()
    ownership.add_nodes_from(members)
    ordering: nx.DiGraph[int] = nx.DiGraph()
    ordering.add_nodes_from(members)
    scopes: dict[str, list[tuple[int, tuple[int, int]]]] = {}
    annotated_scopes: set[str] = set()
    for i, resource in enumerate(resources):
        obj = resource.obj
        group, kind, namespace, name = identity(resource)
        labels = mapping(obj.metadata.get("labels", {}))
        annotations = mapping(obj.metadata.get("annotations", {}))
        scope = str(annotations.get(ARGO + "tracking-id", "")).split(":")[0]
        scope = scope or str(labels.get(ARGO + "instance", ""))
        # Argo's default label is also used by Helm, so require a supplied Application.
        instance = str(labels.get("app.kubernetes.io/instance", ""))
        if not scope and any(
            key[0:2] == ("argoproj.io", "Application") and key[3] == instance for key in indexed
        ):
            scope = instance
        phase = {"PreSync": 0, "Sync": 1, "PostSync": 2}.get(
            str(annotations.get(ARGO + "hook", "Sync")), 1
        )
        wave_text = str(annotations.get(ARGO + "sync-wave", "0"))
        if not re.fullmatch(r"-?\d+", wave_text):
            raise CompilationError(f"{kind}/{name}: invalid Argo sync-wave {wave_text!r}")
        scopes.setdefault(scope, []).append((i, (phase, int(wave_text))))
        if ARGO + "sync-wave" in annotations or ARGO + "hook" in annotations:
            annotated_scopes.add(scope)
        for owner_kind, owner_group in FLUX.items():
            owner_name = labels.get(owner_group + "/name")
            owner_namespace = labels.get(owner_group + "/namespace", namespace)
            owner = indexed.get((owner_group, owner_kind, str(owner_namespace), str(owner_name)))
            if owner is not None and owner != i:
                ownership.add_edge(owner, i)
        if scope:
            applications = [
                j
                for key, j in indexed.items()
                if key[0:2] == ("argoproj.io", "Application") and key[3] == scope
            ]
            if len(applications) > 1:
                raise CompilationError(f"Ambiguous Argo Application ownership: {scope}")
            if applications and applications[0] != i:
                ownership.add_edge(applications[0], i)
        if FLUX.get(kind) == group:
            for raw in as_list(obj.spec.get("dependsOn")):
                ref = mapping(raw)
                target = indexed.get(
                    (group, kind, str(ref.get("namespace", namespace)), str(ref.get("name", "")))
                )
                if target is None:
                    raise CompilationError(
                        f"{kind}/{name}: missing Flux dependency {ref.get('name')}"
                    )
                if "readyExpr" in ref:
                    raise CompilationError(
                        f"{kind}/{name}: Flux readyExpr cannot be evaluated by Compose"
                    )
                ordering.add_edge(target, i)
    if not nx.is_directed_acyclic_graph(ownership):
        raise CompilationError("GitOps ownership contains a cycle")
    for i in reversed(list(nx.topological_sort(ownership))):
        for child in ownership.successors(i):
            members[i].update(members[child])
    for scope in annotated_scopes:
        for before, before_rank in scopes[scope]:
            for after, after_rank in scopes[scope]:
                if before_rank < after_rank:
                    ordering.add_edge(before, after)
    if not nx.is_directed_acyclic_graph(ordering):
        raise CompilationError("GitOps ordering contains a cycle")
    for before, after in ordering.edges:
        if not members[before] or not members[after]:
            log.warning(
                "GitOps ordering endpoint has no selected runtime services: %s -> %s",
                resources[before].obj.metadata.get("name"),
                resources[after].obj.metadata.get("name"),
            )
    # Ancestors preserve dependencies through controller objects without runtime services.
    for after in ordering:
        for before in nx.ancestors(ordering, after):
            for name in sorted(members[after]):
                service = mapping(services[name])
                dependencies = mapping(service.get("depends_on", {}))
                for dependency in sorted(members[before]):
                    if dependency == name:
                        raise CompilationError(
                            "GitOps ordering overlaps a controller's own services"
                        )
                    target_service = mapping(services[dependency])
                    target_obj = sources[dependency].obj
                    health = mapping(target_service.get("healthcheck", {}))
                    condition = "service_started"
                    if target_obj.kind == "Job":
                        if target_service.get("restart") in {"always", "unless-stopped"}:
                            raise CompilationError(
                                f"{dependency}: Job completion needs a finite restart policy"
                            )
                        condition = "service_completed_successfully"
                    elif health and not health.get("disable") and health.get("test") != ["NONE"]:
                        condition = "service_healthy"
                    existing = mapping(dependencies.get(dependency, {}))
                    if (
                        condition == "service_completed_successfully"
                        or existing.get("condition", "service_started") == "service_started"
                    ):
                        existing["condition"] = condition
                    dependencies[dependency] = existing
                service["depends_on"] = dependencies
                services[name] = service
