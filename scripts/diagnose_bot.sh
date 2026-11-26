#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "Please set BOT_TOKEN environment variable, e.g. export BOT_TOKEN=\"123:ABC...\""
  exit 1
fi

echo "== getMe =="
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | (command -v jq >/dev/null 2>&1 && jq '.' || cat)

echo
echo "== getWebhookInfo =="
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | (command -v jq >/dev/null 2>&1 && jq '.' || cat)

if [ "${1:-}" = "--get-updates" ]; then
  echo
  echo "== getUpdates (CAUTION: this will consume pending updates) =="
  echo "Stop any running polling bot before calling this."
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?limit=10" | (command -v jq >/dev/null 2>&1 && jq '.' || cat)
fi

cat <<'EOF'

Next steps / quick checks:
- Ensure the bot is a member of the group and not banned.
- If the group is a supergroup, the chat id will be negative (starts with -100...).
- If you don't see group updates in the DB, check BotFather privacy (/setprivacy) and disable it to let the bot receive all messages.
- Avoid running getUpdates while the bot is running (it will steal updates).
EOF
