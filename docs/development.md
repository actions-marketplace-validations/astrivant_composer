# Development

This guide is for contributors modifying Composer. Application authors can start with
[usage](usage.md) and [runtime profiles](profiles.md).

## Contents

- [Environment](#environment)
- [Checks](#checks)
- [Compilation pipeline](#compilation-pipeline)
- [Package organization](#package-organization)
- [Application integration](#application-integration)
- [Builds and provenance](#builds-and-provenance)

## Environment

Use Python 3.13+, Poetry 2.1+ and Helm 4. `.python-version` selects the local interpreter,
and `poetry.toml` keeps the virtual environment inside the checkout. The Poetry lockfile
pins contributor and CI dependencies.

```sh
poetry install
poetry run pre-commit install
```

If another project's environment is active, deactivate it or run:

```sh
env -u VIRTUAL_ENV -u PYENV_VERSION -u PYENV_VIRTUAL_ENV poetry install
```

Development dependencies include Ruff, strict mypy, Google-style docstring validation
and pre-commit. Runtime dependencies include attrs/cattrs for model conversion,
YAML parsers, JSON Schema validation and NetworkX for dependency checks.

## Checks

```sh
poetry check --lock
poetry run ruff check pkg tests
poetry run ruff format --check pkg tests
poetry run mypy
poetry run pydoclint --config=pyproject.toml pkg tests
poetry run python -m unittest discover -s tests
poetry run pre-commit run --all-files
poetry build
```

The tests cover native field inheritance, quantity conversion, bindings, source and
container selection, profile validation, dependency failures, YAML parsing, generated
mounts and runtime comparison. Real Helm fixtures verify coalesced values, changed-value
propagation and read-only drift checks. Keep Helm available when running them; otherwise
those rendering tests skip.

[CircleCI](../.circleci/config.yml) installs Helm and Poetry, then runs the portable
checks and fixture tests. It does not need an application repository or cluster.
Runtime comparison tests validate configuration; they do not launch a Compose stack.

## Compilation pipeline

1. **Read inputs.** Parse a profile and validate its schema. For chart input, copy the
   chart to a temporary directory and inject a temporary ConfigMap template that
   exposes `toJson .Values`. Render resources and actual coalesced values with Helm,
   then discard the marker. Manifest input skips rendering and merges supplied values.
2. **Translate containers.** Structure Kubernetes resources, resolve the selected
   container and translate native fields through the Kubernetes and Compose ASTs.
   Regular and init containers can be selected independently. Custom resources supply
   binding context and require explicit standalone runtime definitions.
3. **Apply the profile.** Remove shared paths, merge shared defaults, remove service
   paths, then merge service overrides. Mappings merge recursively; lists and scalars
   replace. Bindings resolve against the source resource, container and coalesced values.
4. **Validate.** Check the startup dependency graph and Compose schema. Profile selections
   must be unique. Missing dependencies, cycles and healthy gates without healthchecks
   fail before files are written.
5. **Compare and write.** Compare the normalized Compose tree to an optional independent
   reference. Unless `--check` is set, write Compose, referenced mounted files and an
   optional provenance report. Reference content is never used to generate services.

Generated mounted-file paths are restricted to `.compose-generated/` beside the output.
Only files still referenced after profile application are materialized. Runtime
comparisons normalize representation differences; changes to this normalization need
regression tests that distinguish semantic changes from formatting.

## Package organization

| Location                     | Responsibility                                                                     |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `pkg/composer/entrypoint.py` | Argparse CLI, Helm rendering, schema validation, comparison, output and JSON logs. |
| `pkg/composer/compiler.py`   | Source/container translation, profile application and compilation result.          |
| `pkg/composer/profile.py`    | Selectors, JSON pointer bindings, merge/removal rules and dependency validation.   |
| `pkg/composer/ast/`          | Kubernetes and Compose models and reusable conversion functions.                   |
| `pkg/composer/utils.py`      | YAML parsing and shared normalization helpers.                                     |
| `pkg/composer/exceptions.py` | Custom compilation exception.                                                      |
| `pkg/composer/schemas/`      | Profile and Compose schemas bundled into wheels and source distributions.          |
| `tests/`                     | Unit, real Helm and optional application integration tests.                        |

Python uses postponed annotations, inline types and Google-style docstrings. Imports
used only by annotations belong under `TYPE_CHECKING`; imports required for runtime
model conversion remain available. CLI parsers use `ArgumentDefaultsHelpFormatter`.
If flags or defaults change, regenerate the help block in [usage](usage.md#cli-reference).

## Application integration

Application repositories own chart inputs, local profiles, generated Compose contracts
and deployment tooling. Composer remains independently installable. Integration with
Astrivant is optional and requires access to that separate application checkout.

Its wrapper supplies release `astrivant`, namespace `platform`, the chart and local
runtime profile. For contributors with the repositories side by side:

```sh
cd ../astrivant
bash scripts/generate-compose.sh
bash scripts/generate-compose.sh --check
bash scripts/generate-compose.sh --report .cache/composer/report.json
cd ../composer
ASTRIVANT_ROOT=../astrivant poetry run python -m unittest discover -s tests
```

Set `COMPOSER_ROOT` when invoking the application wrapper with this compiler elsewhere.
Set `ASTRIVANT_ROOT` for the compiler's optional real-chart parity test; it skips when
that variable is absent. The application's `docs/composer.md` and Ansible runbook own
the deployment details.

The initial integration compared all 35 services and 11 named volumes against the
application's previous hand-maintained Compose file. Both normalized comparison and
Docker Compose's resolved configuration matched. Recheck the current contract when
changing conversion rules rather than treating those historical counts as fixed limits.

## Builds and provenance

`poetry build` produces the wheel and source distribution in `dist/`. Both validation
schemas are package data; verify their inclusion when changing packaging. Local checks
do not publish releases or change remote repository settings.

The reusable AST, conversion utilities and Compose schema originated in source revision
`20b06de0ff59dc94f47c35aa1facdd6113e512b7`. The extraction retained no source Git history,
environment files, credentials, deployment examples, service catalogs or infrastructure
shims. Application-specific substitutions belong in profiles rather than compiler code.
