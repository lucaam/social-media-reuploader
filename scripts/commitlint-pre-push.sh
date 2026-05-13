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
  echo "commitlint-pre-push: no ref info from stdin; checking last 100 commits"
  "$CLBIN" --from=HEAD~100 --to=HEAD || exit 1
fi
