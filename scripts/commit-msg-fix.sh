#!/usr/bin/env bash
set -euo pipefail

# commit-msg-fix.sh
# Tries to automatically truncate commit header longer than 100 chars
# then runs commitlint against the message file.

MSGFILE="${1:-}"
if [ -z "$MSGFILE" ] || [ ! -f "$MSGFILE" ]; then
  if [ -f .git/COMMIT_EDITMSG ]; then
    MSGFILE=".git/COMMIT_EDITMSG"
  else
    echo "commit-msg-fix: cannot find commit message file" >&2
    exit 1
  fi
fi

MAX=100
HEADER=$(sed -n '1p' "$MSGFILE" || true)
LEN=${#HEADER}

if [ "$LEN" -gt "$MAX" ]; then
  TRUNC=$(printf '%s' "$HEADER" | cut -c1-$MAX)
  # Prefer not to cut mid-word: remove a trailing partial word if possible
  TRUNC2=$(printf '%s' "$TRUNC" | awk '{ $NF=""; sub(/[[:space:]]+$/,""); print }')
  if [ -z "$TRUNC2" ]; then
    TRUNC2="$TRUNC"
  fi

  # Preserve rest of message
  tail_content=$(sed -n '2,$p' "$MSGFILE" || true)
  printf '%s\n%s\n' "$TRUNC2" "$tail_content" > "$MSGFILE"

  printf 'commit-msg-fix: truncated header from %d to %d chars\n' "$LEN" "${#TRUNC2}" >&2
fi

if [ -x ./node_modules/.bin/commitlint ]; then
  if [ -n "${1:-}" ]; then
    ./node_modules/.bin/commitlint --edit "$MSGFILE"
  else
    ./node_modules/.bin/commitlint --edit
  fi
else
  echo "commitlint not installed; run 'npm install'" >&2
  exit 1
fi
