#!/usr/bin/env bash
set -euo pipefail

# prepare_release.sh
# Run locally the checks CI runs before a merge to main, and preview the
# release that the merge would cut. Releases are automatic: on every push to
# main the "Release" workflow (.github/workflows/release.yml) runs the same
# checks, computes the version from the Conventional Commits since the last
# tag and publishes image, chart, tag and GitHub release (see RELEASE.md).

BRANCH_DEFAULT=main
CHECK_COMMITS_FROM=HEAD~15

die() { echo "ERROR: $*" >&2; exit 1; }

echo "Running the pre-merge checks..."

if [ ! -e .git ]; then
  die "must be run from the repository root"
fi

branch=$(git rev-parse --abbrev-ref HEAD)
echo "Current branch: $branch"
if [ "$branch" = "$BRANCH_DEFAULT" ]; then
  echo "Note: you are on '$BRANCH_DEFAULT'; changes reach it through pull requests."
fi

echo "Checking working tree is clean..."
if ! git diff-index --quiet HEAD --; then
  die "working tree is dirty; commit or stash changes first"
fi

echo "Running Python tests (pytest)..."
if command -v pytest >/dev/null 2>&1; then
  REQUESTS_DB="${REQUESTS_DB:-$(mktemp -d)/requests.db}" python -m pytest -q || die "pytest failed"
else
  echo "pytest not found: skipping tests (install with 'pip install -r requirements-dev.txt')"
fi

echo "Running pre-commit hooks..."
if command -v pre-commit >/dev/null 2>&1; then
  # The helm-lint hook needs helm on PATH (optional locally; CI runs it in
  # "Helm chart lint"): skip it when helm is missing, on top of any SKIP
  # already set. Every other hook is pinned and installed by pre-commit.
  skip="${SKIP:-}"
  if ! command -v helm >/dev/null 2>&1; then
    echo "helm not found: skipping the helm-lint hook"
    skip="${skip:+$skip,}helm-lint"
  fi
  SKIP="$skip" pre-commit run --all-files || die "pre-commit checks failed"
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

echo "Previewing the release..."
if [ -f scripts/release/version.py ] && command -v python3 >/dev/null 2>&1; then
  # Needs the tags: git fetch --tags. A merge to main of these commits as one
  # squashed commit releases what its pull request title says (the PR title
  # check shows it).
  python3 scripts/release/version.py plan --ref HEAD || echo "release preview not available"
else
  echo "python3 or scripts/release/version.py not found: skipping the preview"
fi

cat <<'EOF'

Local checks passed. Releases need no manual step:
  1) Open a pull request with a Conventional Commits title (feat: minor,
     fix/perf: patch, `!` or BREAKING CHANGE: major, minor while 0.y.z).
  2) Squash-merge it once the checks are green.
  3) The "Release" workflow tests main again, publishes
     ghcr.io/<owner>/social-media-reuploader:X.Y.Z (and latest) and
     oci://ghcr.io/<owner>/charts/social-media-reuploader X.Y.Z, signs both
     and creates the tag vX.Y.Z and the GitHub release.
  To publish an existing tag again: Actions > Release > Run workflow, tag=vX.Y.Z.
EOF

echo "Done."
