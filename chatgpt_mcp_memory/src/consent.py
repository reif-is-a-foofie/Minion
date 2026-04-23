"""Ambient consent registry (stored under settings.json ``ambient`` key)."""
from __future__ import annotations

import time
from copy import deepcopy
from typing import Any, Dict


DEFAULT_SOURCE_TEMPLATE: Dict[str, Any] = {
    "enabled": False,
    "last_sync_at": None,
    "revoked_at": None,
}


def _src(enabled: bool) -> Dict[str, Any]:
    d = deepcopy(DEFAULT_SOURCE_TEMPLATE)
    d["enabled"] = enabled
    return d


DEFAULT_AMBIENT: Dict[str, Any] = {
    "enabled": False,
    "delete_plaintext_export_after_seal": False,
    "email_body_parsing": False,
    "gps_coarse": False,
    "healthkit_clinical": False,
    "sources": {
        "chatgpt_export": _src(True),
        "calendar_google": _src(False),
        "email_gmail": _src(False),
        "plaid": _src(False),
        "contacts": _src(False),
        "readwise": _src(False),
        "healthkit": _src(False),
    },
}


def merge_ambient_defaults(raw: Any) -> Dict[str, Any]:
    out = deepcopy(DEFAULT_AMBIENT)
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    for k in (
        "delete_plaintext_export_after_seal",
        "email_body_parsing",
        "gps_coarse",
        "healthkit_clinical",
    ):
        if k in raw:
            out[k] = bool(raw[k])
    src_in = raw.get("sources") or {}
    if isinstance(src_in, dict):
        merged_sources = dict(out["sources"])
        for key, tmpl in merged_sources.items():
            cur = src_in.get(key)
            if isinstance(cur, dict):
                m = deepcopy(tmpl)
                if "enabled" in cur:
                    m["enabled"] = bool(cur["enabled"])
                if cur.get("last_sync_at") is not None:
                    try:
                        m["last_sync_at"] = float(cur["last_sync_at"])
                    except (TypeError, ValueError):
                        pass
                if cur.get("revoked_at") is not None:
                    try:
                        m["revoked_at"] = float(cur["revoked_at"])
                    except (TypeError, ValueError):
                        pass
                merged_sources[key] = m
        out["sources"] = merged_sources
    return out


def ambient_sources(settings: Dict[str, Any]) -> Dict[str, Any]:
    return merge_ambient_defaults(settings.get("ambient"))["sources"]


def is_ambient_globally_enabled(settings: Dict[str, Any]) -> bool:
    return bool(merge_ambient_defaults(settings.get("ambient"))["enabled"])


def is_source_enabled(settings: Dict[str, Any], source_key: str) -> bool:
    amb = merge_ambient_defaults(settings.get("ambient"))
    if not amb["enabled"]:
        return False
    src = amb["sources"].get(source_key) or {}
    if src.get("revoked_at"):
        return False
    return bool(src.get("enabled"))


def touch_source_sync(settings: Dict[str, Any], source_key: str) -> Dict[str, Any]:
    amb = merge_ambient_defaults(settings.get("ambient"))
    if source_key in amb["sources"]:
        amb["sources"][source_key]["last_sync_at"] = time.time()
    out = dict(settings)
    out["ambient"] = amb
    return out


def revoke_source_consent(settings: Dict[str, Any], source_key: str) -> Dict[str, Any]:
    amb = merge_ambient_defaults(settings.get("ambient"))
    if source_key in amb["sources"]:
        amb["sources"][source_key]["enabled"] = False
        amb["sources"][source_key]["revoked_at"] = time.time()
    out = dict(settings)
    out["ambient"] = amb
    return out
