"""Normalize macOS screen-context JSONL into typed `ambient_events` rows."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from store import ambient_event_insert_ignore

log = logging.getLogger(__name__)


def ingest_screen_context_jsonl(
    *,
    data_dir: Path,
    conn: Any,
    max_lines: int = 1200,
) -> Dict[str, Any]:
    """Tail-read `<data_dir>/screen_context/stream.jsonl`, insert deduped window_focus rows."""
    path = Path(data_dir).expanduser().resolve() / "screen_context" / "stream.jsonl"
    if not path.is_file():
        return {"ingested": 0, "skipped_no_file": True}

    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        log.warning("ambient ingest could not read %s: %s", path, e)
        return {"ingested": 0, "error": str(e)}

    tail = raw_lines[-max(1, min(max_lines, 50_000)) :]
    inserted = 0
    for ln in tail:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") != "window_focus":
            continue
        try:
            ts = float(rec.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        app = str(rec.get("app_name") or "")
        title = str(rec.get("window_title") or "")
        dedupe_key = f"wf:{ts:.6f}:{app}\x1f{title}"
        payload = {k: v for k, v in rec.items()}
        try:
            if ambient_event_insert_ignore(
                conn,
                event_type="window_focus",
                captured_at=ts,
                dedupe_key=dedupe_key,
                payload=payload,
                sensitivity="vault_local",
                storage_tier="hot",
            ):
                inserted += 1
        except Exception:
            log.exception("ambient_event_insert_ignore failed")
    return {"ingested": inserted, "scanned_lines": len(tail)}
