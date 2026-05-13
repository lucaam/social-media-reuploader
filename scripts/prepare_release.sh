#!/usr/bin/env bash
set -euo pipefail

# prepare_release.sh
# Run local checks and print needed steps to prepare a release.

BRANCH_DEFAULT=main
CHECK_COMMITS_FROM=HEAD~15

die() { echo "ERROR: $*" >&2; exit 1; }

echo "Preparing release checks..."

cwd=$(pwd)
if [ ! -d .git ]; then
  die "must be run from repository root"
fi

branch=$(git rev-parse --abbrev-ref HEAD)
echo "Current branch: $branch"
if [ "$branch" != "$BRANCH_DEFAULT" ]; then
  echo "Warning: you are not on '$BRANCH_DEFAULT' — run this on '$BRANCH_DEFAULT' or pass a clean merge/PR to it."
fi

echo "Checking working tree is clean..."
if ! git diff-index --quiet HEAD --; then
  die "working tree is dirty; commit or stash changes before preparing a release"
fi

echo "Running Python tests (pytest)..."
if command -v pytest >/dev/null 2>&1; then
  pytest -q || die "pytest failed"
else
  echo "pytest not found — skipping tests (install with 'pip install -r requirements-dev.txt')"
fi

echo "Running pre-commit hooks..."
if command -v pre-commit >/dev/null 2>&1; then
  pre-commit run --all-files || die "pre-commit checks failed"
else
  echo "pre-commit not found — skipping hook run"
fi

echo "Running commitlint on recent commits ($CHECK_COMMITS_FROM..HEAD)..."
if [ -x ./node_modules/.bin/commitlint ]; then
  ./node_modules/.bin/commitlint --from=$CHECK_COMMITS_FROM --to=HEAD || die "commitlint failed"
else
  echo "commitlint not installed locally. Run 'npm ci' to install dev deps and retry."
fi

echo "Checking repository files..."
[ -f .github/workflows/release-please.yml ] || echo "Missing .github/workflows/release-please.yml"
[ -f .github/workflows/docker-release.yml ] || echo "Missing .github/workflows/docker-release.yml"

echo "Checking version metadata..."
if [ -f src/__init__.py ]; then
  ver=$(python3 -c "import importlib.util,sys;spec=importlib.util.spec_from_file_location('pkg', 'src/__init__.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);print(getattr(m,'__version__','unknown'))")
  echo "Package version (src/__init__.py): $ver"
fi

echo "Checking Helm chart appVersion and Chart version"
if [ -f charts/social-media-reuploader/Chart.yaml ]; then
  echo "Chart.yaml:"; sed -n '1,40p' charts/social-media-reuploader/Chart.yaml
fi

echo
echo "Local checks passed. Next steps to publish a release:"
echo "  1) Ensure repository secrets are set: GHCR_PAT (for GHCR visibility/publishing), optionally RELEASE_PLEASE_TOKEN if GITHUB_TOKEN is restricted."
echo "  2) Merge your release branch/PR into 'main' (release-please runs on push to main)."
echo "  3) After merge, release-please will create/publish the GitHub Release (workflow runs on main)."
echo "  4) docker-release workflow will build & push container image on release; ensure GHCR_PAT is configured if required."

if command -v gh >/dev/null 2>&1; then
  echo
  echo "Hint: you can trigger the release-please workflow manually via the GitHub CLI:"
  echo "  gh workflow run 'Release Please' --repo \",$(git config --get remote.origin.url | sed -E 's#.*/(.*)\.git#\1#')" || true
fi

echo "Done."
