#!/usr/bin/env bash
# Smoke-test the same stack the desktop uses on first launch: empty venv,
# pip install -r chatgpt_mcp_memory/requirements.txt, start api.py, GET /status.
# Does not run Tauri — use this in CI or before a release; for the full shell,
# see desktop/README.md "Testing a fresh sidecar".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REQ="$ROOT/chatgpt_mcp_memory/requirements.txt"
SRC="$ROOT/chatgpt_mcp_memory/src"
PORT="${MINION_SMOKE_PORT:-18765}"

if [[ ! -f "$REQ" ]]; then
  echo "missing $REQ" >&2
  exit 1
fi

DATA="$(mktemp -d)"
cleanup() {
  rm -rf "$DATA"
}
trap cleanup EXIT

python3 -m venv "$DATA/venv"
PY="$DATA/venv/bin/python"
"$PY" -m pip install --upgrade pip setuptools wheel >/dev/null
"$PY" -m pip install -r "$REQ"

export MINION_DATA_DIR="$DATA"
export MINION_INBOX="$DATA/inbox"
mkdir -p "$MINION_INBOX"
export MINION_API_PORT="$PORT"
export MINION_DISABLE_WATCHER="${MINION_DISABLE_WATCHER:-1}"

(cd "$SRC" && "$PY" api.py --port "$PORT") &
PID=$!

for _ in $(seq 1 90); do
  if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/status" >/dev/null; then
    echo "ok: GET http://127.0.0.1:${PORT}/status (venv + sidecar)"
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    exit 0
  fi
  sleep 1
done

echo "timeout: sidecar did not respond on port ${PORT}" >&2
kill "$PID" 2>/dev/null || true
exit 1
