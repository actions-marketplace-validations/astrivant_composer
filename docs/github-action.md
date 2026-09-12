# Get a Docker Compose file for free in your project

The Action regenerates Compose when files in a watched directory change, then commits
and pushes changed output back to the triggering branch. Both the watched directory
and default chart path are `helm/`. After generation, the Action uses `git diff --cached`
against `HEAD`, restricted to generated paths. It commits only when their contents
differ from the committed versions (including newly created files). Rewriting identical
output or changing file timestamps does not create a commit.

Add this workflow to your project after the Action is published, replacing the Action
reference with a reviewed commit SHA or release:

```yaml
name: Maintain Docker Compose
on:
  push:
    paths:
      - 'helm/**'
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: composer-${{ github.ref }}
  cancel-in-progress: false
jobs:
  compose:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astrivant/composer@<commit-or-release>
```

This generates and commits `compose.yaml`. The Action installs Python 3.13, Composer
from its own checkout, and Helm 4.3.0. Chart dependencies must already be built or
vendored. Linux and macOS runners are supported.

## Watch a different directory

Set `watch-directory` and update the workflow's `on.push.paths` filter to match:

```yaml
with:
  watch-directory: deploy/
  chart: deploy/charts/my-app
  profile: compose.profile.yaml
  output: generated/compose.yaml
```

The workflow controls when GitHub starts a run; an Action cannot register its own
workflow triggers. The Action also checks the full push range internally and skips
compilation if that directory did not change. Directory names are literal, not globs.
Renames and deletions count as changes. On a new branch, existing files under the
watched directory trigger generation. A manual `workflow_dispatch` always runs.
Use `watch-directory: '.'` and omit the workflow path filter to watch the whole project,
including profiles or values outside the chart directory.

`fetch-depth: 0` supplies the commit history used for comparison. Missing history fails
rather than silently skipping generation. Checkout must match the triggering commit.
The Action pushes normally, without force; branch protection or a concurrent remote
change can reject the push and fail the step. The checkout token needs `contents: write`
and permission to push to that branch. The default persisted checkout credentials are
used. Commits made using `GITHUB_TOKEN` do not start another push workflow; see
[GitHub's workflow triggering rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

Only the Compose file, its sibling `.compose-generated/` directory, and an optional
report are committed. Keep that support directory dedicated to Composer. Generated
mount files may contain Kubernetes Secret values; use suitable local development
values for output committed to your repository. Output paths must be inside the
checkout. Git ignores are respected: remove generated output paths from `.gitignore`
if you want the Action to commit them. Unrelated staged changes are left out of the
commit. The commit author is `github-actions[bot]`.

## Rendered manifests and validation

For already-rendered inputs, configure newline-separated paths:

```yaml
with:
  watch-directory: deploy/
  manifests: |
    deploy/controllers.yaml
    deploy/rendered.yaml
  output: generated/compose.yaml
```

For PR validation, use `check: 'true'` to verify committed output without writing,
committing or pushing. To generate files for artifact upload instead, use
`commit: 'false'`. Commit mode accepts branch pushes and manual branch runs; it rejects
PR merge refs, forks and tag events. Generation and check mode can run on PR events.
To upload generated output, include its sibling `.compose-generated/` directory
(`include-hidden-files: true` with `actions/upload-artifact`).

| Input | Default | Meaning |
| --- | --- | --- |
| `watch-directory` | `helm/` | Directory whose pushed changes trigger compilation. |
| `commit` | `'true'` | Commit and push changed generated output. Ignored in check mode. |
| `chart` | watched directory | Chart path when `manifests` is absent. |
| `manifests` | none | Manifest paths, one per line; mutually exclusive with `chart`. |
| `values` | none | Helm values paths, one per line in override order. |
| `profile` | none | Runtime profile. |
| `output` | `compose.yaml` | Output or drift-check target. |
| `report` | none | Optional JSON compilation report. |
| `check` | `'false'` | Set `'true'` to fail on drift without rewriting output. |
| `release` | `composer` | Helm release name. |
| `namespace` | `default` | Helm namespace. |

Outputs: `skipped` indicates no watched changes; `committed` indicates a successful
commit and push; `compose-file` is the absolute output path after successful generation
or checking, and is unset when skipped. Paths are relative to the caller's checkout.
Inputs are passed as arguments without evaluating shell expressions.

## Publishing

`action.yml` contains the entry point, inputs, outputs and Marketplace branding.
A pushed commit is directly usable as an Action; a release tag gives consumers a stable
reference. A repository maintainer can publish a release and select **Publish this
Action to the GitHub Marketplace**, subject to GitHub's repository and naming
requirements. Adding these files locally does not publish a release or Marketplace
listing. See [GitHub's action metadata reference](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax).
