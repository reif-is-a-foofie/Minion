#!/usr/bin/env bash
# Dev helper: list likely Minion-related Python processes for the current user.
# Review output before killing anything.
set -euo pipefail
echo "=== api.py (HTTP sidecars) ==="
ps aux 2>/dev/null | grep -E '[p]ython.*api\.py' || true
echo ""
echo "=== mcp_server.py (MCP) ==="
ps aux 2>/dev/null | grep -E '[p]ython.*mcp_server\.py' || true
echo ""
echo "=== Multipass / QEMU with minion in path (VM, not Python Minion) ==="
ps aux 2>/dev/null | grep -E '[q]emu.*minion' || true
echo ""
echo "Done. To stop a sidecar: kill <PID> (or Quit Minion app + Restart sidecar)."
