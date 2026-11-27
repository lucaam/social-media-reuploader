#!/usr/bin/env bash
# Run the admin GUI locally (uvicorn with reload), using the project's
# virtualenv when there is one. The GUI shares the SQLite DB with the bot:
# run the bot with the same REQUESTS_DB (default data/requests.db).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PYTHON=python3
if [ -x "./.venv/bin/python" ]; then
  PYTHON="./.venv/bin/python"
fi

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8081}
export REQUESTS_DB="${REQUESTS_DB:-$ROOT/data/requests.db}"
# Plain HTTP on localhost: the session cookie cannot be Secure-only.
export SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-false}"
mkdir -p "$(dirname "$REQUESTS_DB")"

exec "$PYTHON" -m uvicorn src.gui:app --host "$HOST" --port "$PORT" --reload
