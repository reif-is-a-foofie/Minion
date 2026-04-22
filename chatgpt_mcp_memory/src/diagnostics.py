"""Local-only diagnostics helpers for support: redacted log tail, peer scan.

All endpoints are GET-only and intended for 127.0.0.1. They never phone home;
the desktop shows them so operators can watch sidecars on this machine.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional


def redact_log_line(line: str) -> str:
    """Strip obvious secrets and shorten home paths; best-effort only."""
    text = line
    home = os.path.expanduser("~")
    if home and len(home) > 2:
        text = text.replace(home, "~")
    text = re.sub(r"(?i)(Authorization:\s*Bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._=-]+\b", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(MINION_API_TOKEN=)\S+", r"\1[redacted]", text)
    return text


def sidecar_log_path() -> Optional[Path]:
    raw = os.environ.get("MINION_LOG_FILE", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p


def read_log_tail(*, max_lines: int = 200, max_bytes: int = 256_000) -> tuple[Optional[Path], List[str]]:
    """Return (path, redacted lines newest-last) or (None, []) if no file."""
    path = sidecar_log_path()
    if path is None or not path.is_file():
        return path, []
    try:
        data = path.read_bytes()
    except OSError:
        return path, []
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
    text = data.decode("utf-8", errors="replace")
    lines = [redact_log_line(L) for L in text.splitlines() if L.strip() != ""]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return path, lines


def _probe_minion_port(port: int, timeout: float) -> Optional[Dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/capabilities"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    except Exception:
        return None
    if body.get("service") != "minion-api":
        return None
    return {
        "port": port,
        "version": body.get("version"),
        "product": body.get("product") or "minion",
    }


def discover_minion_peers(
    my_port: Optional[int],
    *,
    port_lo: Optional[int] = None,
    port_hi: Optional[int] = None,
    timeout: float = 0.08,
    max_workers: int = 48,
) -> List[Dict[str, Any]]:
    """TCP scan loopback for other Minion sidecars (parallel GET /capabilities)."""
    lo = port_lo if port_lo is not None else int(os.environ.get("MINION_PEER_SCAN_PORT_LO", "8688") or 8688)
    hi = port_hi if port_hi is not None else int(os.environ.get("MINION_PEER_SCAN_PORT_HI", "8799") or 8799)
    lo = max(1, min(lo, 65535))
    hi = max(lo, min(hi, 65535))
    ports_set = set(range(lo, hi + 1))
    if my_port and 1 <= int(my_port) <= 65535:
        ports_set.add(int(my_port))
    ports = sorted(ports_set)
    found: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe_minion_port, p, timeout): p for p in ports}
        for fut in as_completed(futures):
            row = fut.result()
            if row:
                found.append(row)
    found.sort(key=lambda r: int(r["port"]))
    mp = my_port
    for row in found:
        row["self"] = bool(mp is not None and int(row["port"]) == int(mp))
    return found


def iter_log_sse_events() -> Any:
    """Sync generator of SSE ``data:`` lines (redacted deltas)."""
    import time

    path = sidecar_log_path()
    if path is None:
        yield f"data: {json.dumps({'error': 'no_file_log', 'hint': 'MINION_LOG_FILE unset'})}\n\n"
        return
    pos: int = 0
    if path.is_file():
        try:
            sz = path.stat().st_size
        except OSError:
            sz = 0
        start = max(0, sz - 48_000)
        try:
            with path.open("rb") as f:
                f.seek(start)
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            chunk = ""
        if start > 0 and "\n" in chunk:
            chunk = chunk.split("\n", 1)[1]
        for line in chunk.splitlines():
            if line.strip():
                yield f"data: {json.dumps({'line': redact_log_line(line)})}\n\n"
        try:
            pos = path.stat().st_size
        except OSError:
            pos = 0
    while True:
        time.sleep(0.85)
        if not path.is_file():
            yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                new = f.read()
                pos = f.tell()
        except OSError:
            yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            continue
        if not new.strip():
            yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            continue
        for line in new.splitlines():
            if line.strip():
                yield f"data: {json.dumps({'line': redact_log_line(line)})}\n\n"
