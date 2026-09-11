# Usage

Composer accepts a Helm chart or rendered Kubernetes manifests and writes a validated
Docker Compose configuration. Install the checkout as described in the
[README](../README.md#install), then run commands from that checkout.

## Contents

- [Charts and values](#charts-and-values)
- [Runtime profiles](#runtime-profiles)
- [Rendered manifests](#rendered-manifests)
- [Comparison and reports](#comparison-and-reports)
- [Generated files](#generated-files)
- [Runtime boundaries](#runtime-boundaries)
- [CLI reference](#cli-reference)

## Charts and values

```sh
helm dependency build ./path/to/chart
poetry run composer --chart ./path/to/chart \
  --release example --namespace default \
  --values ./path/to/local-values.yaml --output compose.yaml
```

Run `helm dependency build` when dependencies are missing or the chart lock changes.
Composer does not fetch them itself. Repeat `--values` in override order; chart mode
uses Helm's actual coalesced values. `--kube-version` controls the Kubernetes capability
version exposed to Helm during rendering, independently of the local Docker runtime.

The source chart is rendered in a temporary copy. Compilation neither contacts a
Kubernetes cluster nor modifies the source templates. Bind mounts in the generated
Compose file are relative to the output directory, so choose `--output` accordingly.

## Runtime profiles

A profile selects the resources and containers that belong in your local environment
and describes the changes needed to run them there. For a chart that renders a
Deployment named `example-worker` with a `worker` container, save this as
`compose.profile.yaml`:

```yaml
schemaVersion: 1
name: example
defaults:
  remove: [/container_name, /networks]
services:
  worker:
    source:
      kind: Deployment
      name: example-worker
      namespace: default
    container: worker
    set:
      restart: unless-stopped
      environment:
        TOKEN: ${TOKEN:-local}
        WORKERS:
          $string:
            $values: /worker/threads
```

This example expects a `worker.threads` value in the chart. Change the selectors and
bindings to match your inputs, then compile:

```sh
poetry run composer --chart ./path/to/chart \
  --release example --namespace default \
  --profile compose.profile.yaml --output compose.yaml
```

Native fields stay inherited unless the profile removes or overrides them. Bindings
read resource, container and coalesced value fields through JSON pointers. Missing
bindings and ambiguous selectors fail compilation. Container ports remain private
unless the profile explicitly sets Compose `ports`.

See [the profile reference](profiles.md) for precedence, init containers, custom
resources and the bundled profile schema.

## Rendered manifests

Pass one or more rendered YAML inputs instead of a chart:

```sh
poetry run composer --manifest workloads.yaml --manifest configuration.yaml \
  --profile compose.profile.yaml --values resolved-values.yaml \
  --output compose.yaml
```

`--chart` and `--manifest` are mutually exclusive. In manifest mode, repeated values
files are merged as plain mappings, with lists and scalars replaced by later values.
Supply already-resolved values when the profile uses `$values`; Helm coalescing occurs
only in chart mode. Duplicate YAML mapping keys are rejected.

## Comparison and reports

For a committed runtime contract, check whether regeneration would change its
configuration:

```sh
poetry run composer --chart ./path/to/chart \
  --profile compose.profile.yaml --output compose.yaml --check
```

For an independent reference, save it before regenerating:

```sh
cp compose.yaml /tmp/compose-reference.yaml
poetry run composer --chart ./path/to/chart \
  --profile compose.profile.yaml --output compose.yaml \
  --compare /tmp/compose-reference.yaml --report .cache/composer/report.json
```

Comparison occurs after compilation. The reference file never supplies services or
settings to the generator. `--compare` can write generated output while reporting drift;
`--check` suppresses all output, report and mounted-file writes. Combining `--check`
and `--compare` selects the explicit reference instead of the output file.

| Exit status | Meaning                                                       |
| ----------- | ------------------------------------------------------------- |
| `0`         | Compilation succeeded and any requested comparison matched.   |
| `1`         | Generated configuration differs from the reference.           |
| `2`         | An input, rendering or compilation failure prevented success. |

Comparison ignores object order, expanded anchors, top-level YAML `x-*` extensions,
equivalent memory units and scalar environment/port representations. Commands,
dependencies, profiles, mounts and other runtime settings still count as drift.
Lists retain their order. This validates configuration equivalence rather than starting
the application or testing its behavior.

The JSON report includes service and volume counts, source identities, removed paths,
overridden keys and differing paths. Detailed service provenance is populated for
profile-selected services. It omits configuration values. Logs are JSON objects on
stderr; the command prints a JSON result summary to stdout.

## Generated files

ConfigMap and Secret files are emitted only when final services still mount them. They
live under `.compose-generated/` beside the output, with owner-only file permissions.
Keep generated secrets and local environment files out of Git. This repository already
ignores `.compose-generated/`, `.cache/`, `.env` and the root generated `compose.yaml`;
choose the corresponding ignore rules in an application repository.

`--check` compares the Compose document. Regenerate to refresh mounted ConfigMap/Secret
contents; the drift check does not compare those files' contents. Unused files from a
previous run are not pruned automatically.

## Runtime boundaries

Without a profile, basic conversion provides a starting point from workload containers.
It flattens resource names into one Compose project; duplicate workload names across
namespaces are skipped after the first. Use explicit selectors and distinct service
names for multi-namespace applications.

Profiles also make operator-managed resources explicit: a custom resource supplies
binding data while its `set` block defines a standalone container. Kubernetes
replication, scheduling, access policies and autoscaling do not transfer to Compose.
CronJobs become containers; supply a schedule or one-shot invocation in your runtime.
A profile includes only selected resources, so review it when adding chart services.

## CLI reference

Generated with `poetry run composer --help`:

```text
usage: composer [-h] (--chart CHART | --manifest MANIFEST) [--values VALUES]
                [--profile PROFILE] [--release RELEASE]
                [--namespace NAMESPACE] [--kube-version KUBE_VERSION]
                [--output OUTPUT] [--compare COMPARE] [--report REPORT]
                [--check] [--log-level {DEBUG,INFO,WARNING,ERROR}]

Compile Helm charts into reproducible Docker Compose configurations.

options:
  -h, --help            show this help message and exit
  --chart CHART         Helm chart with built dependencies (default: None)
  --manifest MANIFEST   Rendered manifest; repeat to combine inputs (default:
                        None)
  --values, -f VALUES   Helm values override; repeat in precedence order
                        (default: [])
  --profile PROFILE     Declarative runtime profile (default: None)
  --release RELEASE     Helm release name (default: composer)
  --namespace NAMESPACE
                        Helm release namespace (default: default)
  --kube-version KUBE_VERSION
                        Kubernetes capabilities for rendering (default:
                        1.37.0)
  --output, -o OUTPUT   Compose output or drift-check target (default:
                        compose.yaml)
  --compare COMPARE     Compare against an independent Compose reference
                        (default: None)
  --report REPORT       Write provenance and comparison results as JSON
                        (default: None)
  --check               Check output drift without rewriting files (default:
                        False)
  --log-level {DEBUG,INFO,WARNING,ERROR}
                        Compiler log severity (default: WARNING)
```
