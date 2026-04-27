"""User-togglable runtime settings (persisted to <data_dir>/settings.json).

Small on purpose. One concern: which kinds of files the user wants Minion
to parse. The schema is additive — unknown keys are preserved on write so
future settings land next to this one without migration.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Set

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


def merge_identity_defaults(raw: Any) -> Dict[str, Any]:
    """ISA session grants + cluster options; unknown keys preserved."""
    base: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    grants_raw = base.get("session_layer_grants")
    cleaned: List[int] = []
    seen: Set[int] = set()
    seq = grants_raw if isinstance(grants_raw, list) else []
    for x in seq:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 7 and n not in seen:
            cleaned.append(n)
            seen.add(n)
    cleaned.sort()
    cap = base.get("cluster_auto_propose")
    cluster_auto = bool(cap) if cap is not None else False
    ts_raw = base.get("session_grants_updated_at")
    ts: Any = None
    if ts_raw is not None:
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            ts = None
    out: Dict[str, Any] = {
        "session_layer_grants": cleaned,
        "cluster_auto_propose": cluster_auto,
    }
    if ts is not None:
        out["session_grants_updated_at"] = ts
    skip = frozenset({"session_layer_grants", "cluster_auto_propose", "session_grants_updated_at"})
    for k, v in base.items():
        if k in skip:
            continue
        out[k] = v
    return out


def _default() -> Dict[str, Any]:
    return {
        "disabled_kinds": [],
        "telemetry_opt_out": False,
        "ambient": merge_ambient_defaults({}),
        "identity": merge_identity_defaults({}),
    }


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
    out["identity"] = merge_identity_defaults(out.get("identity"))
    return out
