#!/usr/bin/env bash
set -euo pipefail

# prepare_release.sh
# Run the local checks before a release and print the release steps.
# The release flow is: "Manual Version Bump" workflow -> bump PR -> merge ->
# "Publish Release on Bump PR Merge" (tag + GitHub release + Docker image).

BRANCH_DEFAULT=main
CHECK_COMMITS_FROM=HEAD~15

die() { echo "ERROR: $*" >&2; exit 1; }

echo "Preparing release checks..."

if [ ! -e .git ]; then
  die "must be run from the repository root"
fi

branch=$(git rev-parse --abbrev-ref HEAD)
echo "Current branch: $branch"
if [ "$branch" != "$BRANCH_DEFAULT" ]; then
  echo "Warning: you are not on '$BRANCH_DEFAULT'; releases are cut from '$BRANCH_DEFAULT'."
fi

echo "Checking working tree is clean..."
if ! git diff-index --quiet HEAD --; then
  die "working tree is dirty; commit or stash changes before preparing a release"
fi

echo "Running Python tests (pytest)..."
if command -v pytest >/dev/null 2>&1; then
  REQUESTS_DB="${REQUESTS_DB:-$(mktemp -d)/requests.db}" python -m pytest -q || die "pytest failed"
else
  echo "pytest not found: skipping tests (install with 'pip install -r requirements-dev.txt')"
fi

echo "Running pre-commit hooks..."
if command -v pre-commit >/dev/null 2>&1; then
  pre-commit run --all-files || die "pre-commit checks failed"
else
  echo "pre-commit not found: skipping hook run"
fi

echo "Running commitlint on recent commits ($CHECK_COMMITS_FROM..HEAD)..."
if [ -x ./node_modules/.bin/commitlint ]; then
  ./node_modules/.bin/commitlint --from="$CHECK_COMMITS_FROM" --to=HEAD || die "commitlint failed"
else
  echo "commitlint not installed locally. Run 'npm ci' to install the dev dependencies and retry."
fi

if command -v helm >/dev/null 2>&1; then
  echo "Linting the Helm chart..."
  helm lint charts/social-media-reuploader --strict || die "helm lint failed"
fi

echo "Checking release workflows..."
for wf in manual-version-bump.yml publish-bump-release.yml docker-publish.yml; do
  [ -f ".github/workflows/$wf" ] || echo "Missing .github/workflows/$wf"
done

echo "Checking version metadata..."
if [ -f src/__init__.py ]; then
  ver=$(python3 scripts/get_version.py)
  echo "Version (latest tag or src/__init__.py): $ver"
fi
if [ -f charts/social-media-reuploader/Chart.yaml ]; then
  grep -E '^(version|appVersion):' charts/social-media-reuploader/Chart.yaml
fi

cat <<'EOF'

Local checks passed. To publish a release:
  1) Run the "Manual Version Bump" workflow (Actions tab, or:
       gh workflow run manual-version-bump.yml --ref main)
     It computes the next version from the Conventional Commits since the last
     tag, bumps src/__init__.py and the chart, opens the bump PR and runs CI on it.
  2) Review and merge the bump PR "chore(release): bump versions to vX.Y.Z".
  3) "Publish Release on Bump PR Merge" creates the tag and the GitHub release
     and publishes ghcr.io/<owner>/social-media-reuploader:X.Y.Z (and :latest).
  To rebuild the image of an existing tag: run "Manual Docker build and publish
  to GHCR" with that tag.
EOF

echo "Done."
