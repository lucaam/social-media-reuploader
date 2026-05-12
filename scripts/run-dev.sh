#!/usr/bin/env bash
# Simple helper to run the FastAPI GUI using the project's virtualenv when available.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

UVICORN_BIN=""
if [ -x "./.venv/bin/uvicorn" ]; then
  UVICORN_BIN="./.venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN_BIN=$(command -v uvicorn)
elif python -m uvicorn --help >/dev/null 2>&1; then
  UVICORN_BIN="$(python -m site 2>/dev/null || true)" # fallback; will use python -m uvicorn below
fi

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8081}

if [ -n "$UVICORN_BIN" ] && [ -x "$UVICORN_BIN" ]; then
  exec "$UVICORN_BIN" src.gui:app --host "$HOST" --port "$PORT"
else
  exec python -m uvicorn src.gui:app --host "$HOST" --port "$PORT"
fi
