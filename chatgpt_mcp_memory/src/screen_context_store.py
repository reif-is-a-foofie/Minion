"""Local screen-context timeline written by the Minion desktop app (macOS).

`<MINION_DATA_DIR>/screen_context/stream.jsonl` — one JSON object per line.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

STREAM_SUBDIR = "screen_context"
STREAM_NAME = "stream.jsonl"
_TAIL_READ_BYTES = 4_000_000


def stream_path(data_dir: Path) -> Path:
    return data_dir / STREAM_SUBDIR / STREAM_NAME


def _tail_lines(path: Path) -> List[str]:
    """Return complete newline-terminated logical lines from the tail of a large file."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= _TAIL_READ_BYTES:
            raw = f.read()
        else:
            f.seek(size - _TAIL_READ_BYTES)
            raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    return text.splitlines()


def read_recent(data_dir: Path, limit: int = 80) -> List[Dict[str, Any]]:
    """Return the last ``limit`` parsed JSON records (oldest-first within the slice)."""
    lim = max(1, min(limit, 500))
    path = stream_path(data_dir)
    lines = _tail_lines(path)
    rows: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-lim:]


def current_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    rows = read_recent(data_dir, limit=1)
    return rows[-1] if rows else None


def search_substring(data_dir: Path, needle: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Search recent events for a case-insensitive substring in title, app, or path."""
    lim = max(1, min(limit, 200))
    if not needle:
        return []
    n = needle.casefold()
    recent = read_recent(data_dir, limit=500)
    hits: List[Dict[str, Any]] = []
    for rec in reversed(recent):
        hay = " ".join(
            str(rec.get(k, "") or "")
            for k in (
                "app_name",
                "window_title",
                "process_path",
                "screenshot_inbox_rel",
                "ax_text_sample",
            )
        ).casefold()
        if n in hay:
            hits.append(rec)
            if len(hits) >= lim:
                break
    hits.reverse()
    return hits


def summarize_recent_heuristic(data_dir: Path, event_limit: int = 60) -> Dict[str, Any]:
    """Cheap textual digest without calling an LLM."""
    events = read_recent(data_dir, limit=max(1, min(event_limit, 300)))
    if not events:
        return {
            "status": "empty",
            "summary": (
                "No screen context captured yet. Launch Minion desktop on macOS and grant "
                "Accessibility + Screen Recording so focused-window logging runs."
            ),
            "events_used": 0,
        }
    from collections import Counter

    apps = Counter(str(e.get("app_name") or "?") for e in events)
    top_apps = ", ".join(f"{a} ({c})" for a, c in apps.most_common(8))
    lines: List[str] = []
    for e in events[-15:]:
        ts = e.get("ts")
        app = e.get("app_name") or "?"
        title = e.get("window_title") or ""
        lines.append(f"[{ts}] {app} — {title}")
    summary = (
        f"Recent activity spans {len(events)} window-focus events. "
        f"Most-used apps: {top_apps}.\nLast focal titles:\n"
        + "\n".join(lines)
    )
    return {"status": "ok", "summary": summary, "events_used": len(events)}
