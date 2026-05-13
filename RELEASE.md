# Release Guide

This document lists the steps and checks required to prepare and publish a release for `social-media-reuploader`.

Prerequisites
- Have CI checks passing on branch to be merged into `main` (tests, linters, commitlint).
- Repository secrets configured (recommended):
  - `GHCR_PAT` — personal access token with `write:packages` to manage GHCR visibility and package publishing. Required for making GHCR packages public automatically.
  - `RELEASE_PLEASE_TOKEN` — optional PAT to use instead of `GITHUB_TOKEN` if your org restricts the default token.

Automated release flow
1. Merge the release branch into `main`. The `release-please` workflow is triggered on pushes to `main` and will create and publish a GitHub Release based on Conventional Commits.
2. Once the release is published, the `docker-release` workflow will build and publish the container image to GHCR (if configured).

Local preparation
1. Install dev tools:

```bash
npm ci      # installs commitlint
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install pre-commit
```

2. Install pre-commit hooks:

```bash
pre-commit install
pre-commit install --hook-type commit-msg
pre-commit install --hook-type pre-push
```

3. Run the helper script to validate everything locally:

```bash
./scripts/prepare_release.sh
```

Manual trigger
- If you need to run the `release-please` action manually, use the Actions UI or the `workflow_dispatch` trigger.
- Example with GitHub CLI (optional):

```bash
# list workflows
gh workflow list
# then run the 'Release Please' workflow
gh workflow run 'Release Please' --repo <owner>/<repo> --ref main
```

Troubleshooting
- If release is not published after merge, check the Actions run logs for `release-please` and ensure the token used has proper permissions.
- If container publish fails, ensure `GHCR_PAT` is configured and has `write:packages` and `delete:packages` (if needed) permissions.
