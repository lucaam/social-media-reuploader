#!/usr/bin/env bash
set -euo pipefail

# commitlint-pre-push.sh
# Run commitlint for commits that are about to be pushed. It reads ref information from stdin.

CLBIN="./node_modules/.bin/commitlint"
if [ ! -x "$CLBIN" ]; then
  echo "commitlint not installed; run 'npm install'" >&2
  exit 1
fi

read_any=false
while read local_ref local_sha remote_ref remote_sha; do
  read_any=true
  echo "commitlint-pre-push: refs local=$local_ref($local_sha) remote=$remote_ref($remote_sha)"
  if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
    FROM="HEAD~100"
  else
    FROM="$remote_sha"
  fi
  TO="$local_sha"
  echo "commitlint-pre-push: running commitlint --from=$FROM --to=$TO"
  "$CLBIN" --from="$FROM" --to="$TO" || {
    echo "commitlint failed for range $FROM..$TO" >&2
    exit 1
  }
done

if [ "$read_any" = false ]; then
  echo "commitlint-pre-push: no ref info from stdin; attempting to detect upstream or recent commit"
  # Prefer checking against upstream if present; otherwise validate the last commit only.
  set +e
  UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)
  set -e
  if [ -n "$UPSTREAM" ]; then
    FROM="$UPSTREAM"
  else
    if git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
      FROM=HEAD~1
    else
      FROM=HEAD
    fi
  fi
  echo "commitlint-pre-push: running commitlint --from=$FROM --to=HEAD"
  "$CLBIN" --from="$FROM" --to=HEAD || exit 1
fi
