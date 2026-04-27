#!/usr/bin/env bash
# Starts a scratch Minion sidecar + Vite (Tauri stubs) for Playwright. Run from repo root or desktop/.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="$(cd "$DESKTOP/.." && pwd)"
API_PORT="${MINION_E2E_API_PORT:-18765}"
TMP="$(mktemp -d)"

# Avoid stale listeners when re-running Playwright locally (reuseExistingServer).
if command -v lsof >/dev/null 2>&1; then
  lsof -ti ":${API_PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true
fi

cleanup() {
  kill "${VITE_PID:-}" 2>/dev/null || true
  kill "${API_PID:-}" 2>/dev/null || true
  wait "${VITE_PID:-}" 2>/dev/null || true
  wait "${API_PID:-}" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

export MINION_DATA_DIR="$TMP/data"
export MINION_INBOX="$TMP/inbox"
export MINION_DISABLE_WATCHER=1
mkdir -p "$MINION_DATA_DIR" "$MINION_INBOX"

PYTHON="${MINION_PYTHON:-python3}"
"$PYTHON" "$REPO/chatgpt_mcp_memory/src/api.py" --port "$API_PORT" &
API_PID=$!

ready=0
for _ in $(seq 1 120); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "e2e-desktop-webserver: sidecar exited before /status was ready (check Python deps / MINION_PYTHON)." >&2
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${API_PORT}/status" >/dev/null; then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" != 1 ]; then
  echo "e2e-desktop-webserver: timed out waiting for sidecar http://127.0.0.1:${API_PORT}/status" >&2
  exit 1
fi

export VITE_E2E=1
export VITE_E2E_API_BASE="http://127.0.0.1:${API_PORT}"
export MINION_E2E_API_PORT="$API_PORT"

cd "$DESKTOP"
./node_modules/.bin/vite dev --port 5173 --strictPort &
VITE_PID=$!
wait "$VITE_PID"
