#!/usr/bin/env bash
# Start Python api.py + Vite with VITE_E2E for Playwright. Repo root is parent of desktop/.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT="${E2E_API_PORT:-9876}"
TMP="$(mktemp -d)"
export MINION_DATA_DIR="$TMP/data"
export MINION_INBOX="$TMP/inbox"
mkdir -p "$MINION_DATA_DIR" "$MINION_INBOX"
export MINION_DISABLE_WATCHER=1
PYTHON="${ROOT}/chatgpt_mcp_memory/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "run-e2e-stack: need ${PYTHON} (create chatgpt_mcp_memory/.venv per README)" >&2
  exit 1
fi
cd "$ROOT/chatgpt_mcp_memory/src"
"$PYTHON" api.py --port "$PORT" &
API_PID=$!
cleanup() {
  kill "$API_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null; then
    break
  fi
  sleep 0.25
done
cd "$ROOT/desktop"
export VITE_E2E=true
export VITE_E2E_API_PORT="$PORT"
exec npm run dev -- --host 127.0.0.1 --port 1420
