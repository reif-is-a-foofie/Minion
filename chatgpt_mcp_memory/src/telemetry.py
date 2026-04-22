"""Append-only telemetry log for Minion.

Why: the system needs a feedback loop. Search quality, ingest failures,
and skipped files are the three signals that tell us whether the memory
is working. Writing them to one JSONL file means future-me (and the user)
can grep patterns without standing up a DB table or a dashboard.

Format: one JSON object per line, under `<data_dir>/telemetry.jsonl`.
Rotated to `telemetry.jsonl.1` at 10 MB so it never grows unbounded.

Intentionally cheap: no embedded library, no network, no threads. A
`log_event()` call is ~1 ms of file I/O.

Shape of events (add more as needed; unknown fields are preserved by
readers):

    {"ts": 1776795000.0, "kind": "search", "mode": "relevance",
     "query": "patriarchal blessing", "returned": 3,
     "top_score": 0.754, "top_path": ".../blessing.txt",
     "top_kind": "text", "rerank": "rrf",
     "hit_kinds": ["text", "chatgpt-export"], "dedup_dropped": 2}

    {"ts": ..., "kind": "ingest", "path": "...", "result": "ingested",
     "file_kind": "image", "parser": "rapidocr+ollama",
     "chunks": 1, "bytes": 214778}

    {"ts": ..., "kind": "ingest", "path": "...", "result": "skipped",
     "file_kind": "image", "reason": "deferred: awaiting vision model"}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


log = logging.getLogger("minion.telemetry")

TELEMETRY_FILENAME = "telemetry.jsonl"
MAX_BYTES = 10 * 1024 * 1024  # 10 MB

_write_lock = threading.Lock()
_base_dir: Optional[Path] = None


def configure(data_dir: Path) -> None:
    """Set the directory where telemetry.jsonl lives.

    Called once at sidecar startup. Safe to call multiple times.
    """
    global _base_dir
    _base_dir = Path(data_dir).expanduser().resolve()


def _resolve_dir() -> Optional[Path]:
    if _base_dir is not None:
        return _base_dir
    env = os.environ.get("MINION_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return None


def _path() -> Optional[Path]:
    d = _resolve_dir()
    if d is None:
        return None
    return d / TELEMETRY_FILENAME


def data_dir() -> Optional[Path]:
    """Resolved data directory for telemetry, or None if not configured yet."""
    return _resolve_dir()


def _maybe_rotate(p: Path) -> None:
    try:
        if p.exists() and p.stat().st_size > MAX_BYTES:
            rolled = p.with_suffix(p.suffix + ".1")
            if rolled.exists():
                rolled.unlink()
            p.rename(rolled)
    except OSError:
        pass


def log_event(kind: str, **fields: Any) -> None:
    """Append a structured event to the telemetry log.

    Never raises: telemetry failures must not break ingest or search.
    """
    p = _path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "kind": kind,
        }
        payload.update(fields)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _write_lock:
            _maybe_rotate(p)
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        try:
            import analytics_remote

            analytics_remote.on_telemetry_logged(kind, fields)
        except Exception:
            pass
    except Exception:
        log.exception("telemetry write failed")


def tail(n: int = 100) -> list[dict]:
    """Return the last `n` events as parsed dicts. Used by debug tooling.

    Cheap for small N; reads the whole file. For large logs, the user can
    just `tail -n N` the JSONL directly.
    """
    p = _path()
    if p is None or not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
