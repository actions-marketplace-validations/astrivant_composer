# Runtime profiles

Profiles describe the intentional differences between a rendered Kubernetes workload
and a Compose environment. Keep them with the application chart so reviewers can see
both deployment contracts together.

## Contents

- [Selection](#selection)
- [Override precedence](#override-precedence)
- [Bindings](#bindings)
- [Operator resources](#operator-resources)
- [Comparison and failure handling](#comparison-and-failure-handling)

## Selection

A profile requires `schemaVersion: 1`, `name` and a nonempty `services` mapping. Each
service selects exactly one resource with `source.kind` and either `source.name` or
`source.labels`. Optional `source.namespace` disambiguates names. Unknown profile
options are schema errors. Selection never falls back to a similar service name.

For workloads, `container` selects one container; it can be omitted only when exactly
one candidate exists. `init: true` selects from `initContainers`. A workload may feed
multiple Compose services, such as a worker plus a separately runnable migration.
Only selected resources become services when a profile is supplied.

## Override precedence

The compiler applies these steps in order:

1. Translate the selected workload container's native Kubernetes fields.
2. Delete paths listed in `defaults.remove`.
3. Recursively merge `defaults.set`.
4. Delete paths listed in the service's `remove`.
5. Recursively merge the service's `set`.

Mappings merge recursively; lists and scalars replace. Deletion uses JSON pointers.
Missing deletion paths are harmless, allowing an override to remove optional chart
fields. Service deletion happens after shared defaults so one-shot containers can
remove `restart` or resource settings inherited by long-running services.

Top-level `volumes` and `networks` supply the final Compose declarations. Profiles own
local port publication, build targets, bind mounts, runtime credentials, dependency
conditions and any deliberately different local image pins. These override paths
will not automatically track their Kubernetes counterparts.

## Bindings

Bindings work recursively inside `set`. Each binding object has exactly one key:

| Expression                                                 | Result                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------- |
| `{$resource: /spec/bootstrap/initdb/database}`             | Field from the selected raw Kubernetes object              |
| `{$container: /image}`                                     | Field from the selected workload container                 |
| `{$values: /analyzer/threads}`                             | Helm-coalesced value, or supplied resolved manifest values |
| `{$string: {$values: /analyzer/threads}}`                  | Scalar converted to a string, booleans lowercase           |
| `{$concat: ['broker:', {$resource: /spec/kafka/version}]}` | Scalar pieces joined into one string                       |

Pointers follow RFC 6901 escaping (`~1` for `/`, `~0` for `~`) and support nonnegative
array indices. Missing paths, malformed expressions, compound string values and
unknown binding names fail compilation. Literal strings are preserved, including
Compose `${VAR:-default}` expressions and escaped shell dollars such as `$$!`.
Use `remove: [/command]` before setting `command` only when deleting optional native
state is useful; lists already replace rather than merge.

## Operator resources

A selected custom resource exposes its complete object to `$resource` but contributes
no implicit native container. Define its Compose `image`, command, environment,
healthcheck and persistence explicitly in `set`. This makes a single-node database or
broker an auditable runtime substitution rather than embedding a controller emulator
inside the compiler. Kubernetes availability guarantees do not transfer to Compose.

Select real custom resources, not placeholder workload names. Binding important values
such as database names and broker versions makes relevant chart changes visible in
local output. Keep passwords as runtime interpolation or external local secrets.

## Comparison and failure handling

Compilation validates the profile, the generated Compose schema and startup dependency
graph before writing. Missing/ambiguous sources and containers fail; healthy dependency
gates require the target to define a healthcheck. Unselected Helm objects remain outside
the Compose runtime and should be reviewed when adding services to the chart.

`--check` and `--compare` compare parsed runtime documents. Object ordering, expanded
YAML anchors, top-level `x-*` extension blocks, equivalent memory units and scalar
environment/port representations do not count as drift. Commands, dependencies, profiles,
mounts and runtime settings do. Lists retain their order. This is a configuration check;
it does not start containers or establish application-level behavior.

A JSON report lists every generated service's source and overridden/removed paths.
Differences report paths rather than values. Compilation does not read a reference
Compose file until the comparison stage and never uses its services as generation input.
