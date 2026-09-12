# GitOps startup ordering

Composer translates ordering metadata on supplied resources automatically, with or
without a runtime profile. Profile service names and multiple containers retain their
source resource's ordering. Native edges are added after profile overrides and checked
alongside explicit `depends_on` entries for cycles.

## Argo CD

`argocd.argoproj.io/sync-wave` accepts integer strings, including negative waves.
Unannotated resources default to wave zero. `argocd.argoproj.io/hook` supports the
successful startup phases `PreSync`, `Sync` (default), and `PostSync`. Phase takes
precedence over wave; resources in the same phase and wave remain parallel.

`Skip`, failure-only and deletion-only hooks are excluded. A hook assigned multiple
phases including startup is rejected because one Compose service cannot execute at
multiple points in the graph. Hook deletion policies, wave delays, retries, selective
sync and ongoing reconciliation are not reproduced.

Resources are grouped by the application name in `argocd.argoproj.io/tracking-id`, or
by the `argocd.argoproj.io/instance` label. The default Argo label
`app.kubernetes.io/instance` is also recognized when it matches an included Application
(to avoid treating unrelated Helm release labels as Argo ownership). Inputs without this metadata form one sync
group. Supply ownership metadata when combining unrelated applications. An included
`argoproj.io` Application groups its tracked children, so waves on Application objects
also order their selected runtime services. Ambiguous application names are rejected.
Composer does not fetch or render Application sources or ApplicationSets.

## Flux

`kustomize.toolkit.fluxcd.io` Kustomization and `helm.toolkit.fluxcd.io` HelmRelease
objects support `spec.dependsOn`, including cross-namespace references. An omitted
dependency namespace defaults to the referring object's namespace. API versions within
these groups are accepted. The similarly named local Kustomize configuration is not a
Flux controller resource.

Include the controllers **and their rendered workloads**. Composer associates children
using the controller's `kustomize.toolkit.fluxcd.io/name` and `/namespace` labels, or
`helm.toolkit.fluxcd.io/name` and `/namespace` labels. Ownership can be nested (for
example, a Kustomization containing HelmReleases). Profiles can also select controller
resources and supply standalone runtime services explicitly.

Missing dependency CRs and controller cycles are errors. Ordering endpoints without
selected runtime services produce warnings; no synthetic controller containers are
created. Flux CEL `readyExpr` dependencies are rejected because Compose cannot evaluate
cluster state. Remote sources, reconciliation, suspension, health expressions and
controller readiness are not executed. Render sources separately and provide the
ownership labels; a HelmRelease alone does not contain its chart's containers.

## Compose conditions

Every service in a later group depends on the earlier group's selected services:

- A Job prerequisite uses `service_completed_successfully`. Infinite restart policies
  on such Jobs are rejected.
- A prerequisite with an enabled healthcheck uses `service_healthy`.
- Other prerequisites use `service_started`, which does not guarantee readiness.

Existing stronger profile conditions are retained; Job completion takes precedence. Add healthchecks in a runtime
profile when Kubernetes controller readiness needs an explicit local equivalent.
This is startup ordering for a single Compose invocation, not full GitOps lifecycle
emulation. Native ordering that conflicts with inferred or explicit dependencies fails
validation so the contradictory inputs can be corrected.

Semantics references: [Argo sync phases and waves](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/),
[Flux Kustomization dependencies](https://fluxcd.io/flux/components/kustomize/kustomizations/#dependencies),
and [Flux HelmRelease dependencies](https://fluxcd.io/flux/components/helm/helmreleases/#dependencies).
