"""User-togglable runtime settings (persisted to <data_dir>/settings.json).

Small on purpose. One concern: which kinds of files the user wants Minion
to parse. The schema is additive — unknown keys are preserved on write so
future settings land next to this one without migration.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from consent import merge_ambient_defaults
from parsers import ALL_KINDS, set_disabled_kinds


log = logging.getLogger("minion.settings")

SETTINGS_FILENAME = "settings.json"


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / SETTINGS_FILENAME


def load_settings(data_dir: Path) -> Dict[str, Any]:
    p = _settings_path(data_dir)
    if not p.exists():
        return _default()
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        log.exception("settings: failed to read %s; using defaults", p)
        return _default()
    if not isinstance(data, dict):
        return _default()
    return _normalize(data)


def save_settings(data_dir: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    p = _settings_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize(data)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return normalized


def apply_settings(data: Dict[str, Any]) -> None:
    """Wire settings into the runtime (parser registry, etc.)."""
    disabled = data.get("disabled_kinds") or []
    set_disabled_kinds(disabled)


def _default() -> Dict[str, Any]:
    return {"disabled_kinds": [], "telemetry_opt_out": False, "ambient": merge_ambient_defaults({})}


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    raw = out.get("disabled_kinds") or []
    if isinstance(raw, str):
        raw = [raw]
    cleaned: List[str] = []
    seen: set[str] = set()
    for k in raw:
        if not isinstance(k, str):
            continue
        k = k.strip()
        if k in ALL_KINDS and k not in seen:
            cleaned.append(k)
            seen.add(k)
    out["disabled_kinds"] = cleaned
    out.pop("analytics_opt_in", None)
    tot = out.get("telemetry_opt_out")
    out["telemetry_opt_out"] = bool(tot) if tot is not None else False
    out["ambient"] = merge_ambient_defaults(out.get("ambient"))
    return out
