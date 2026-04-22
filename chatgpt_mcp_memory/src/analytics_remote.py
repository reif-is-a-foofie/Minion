"""Optional, opt-in HTTP analytics — anonymous aggregates only.

Requires **both**:
  - ``MINION_ANALYTICS_URL`` — HTTPS POST endpoint you operate (or empty → disabled)
  - ``analytics_opt_in: true`` in ``<data_dir>/settings.json`` (user toggle in the app)

The JSON body never includes search queries, file paths, chunk text, or tokens.
Your server still receives normal HTTP metadata (IP, User-Agent, TLS timing) the
same way any website does; disclose that in your privacy policy.

Never raises into callers; failures are dropped on the floor.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from settings import load_settings
from telemetry import data_dir as telemetry_data_dir
from version import __version__

log = logging.getLogger("minion.analytics_remote")

_SCHEMA = 1
_session_sent = False
_hour_bucket: int = 0
_hour_count: int = 0
_lock = threading.Lock()


def _install_id(root: Path) -> str:
    p = root / ".minion_install_id"
    try:
        if p.exists():
            s = p.read_text(encoding="utf-8").strip()
            if len(s) >= 8:
                return s
    except OSError:
        pass
    nid = str(uuid.uuid4())
    try:
        p.write_text(nid + "\n", encoding="utf-8")
    except OSError:
        pass
    return nid


def _remote_enabled(root: Path) -> tuple[bool, str]:
    url = os.environ.get("MINION_ANALYTICS_URL", "").strip()
    if not url:
        return False, ""
    try:
        if not load_settings(root).get("analytics_opt_in"):
            return False, ""
    except Exception:
        return False, ""
    return True, url


def _under_hourly_cap(max_per_hour: int = 120) -> bool:
    global _hour_bucket, _hour_count
    with _lock:
        b = int(time.time() // 3600)
        if b != _hour_bucket:
            _hour_bucket = b
            _hour_count = 0
        if _hour_count >= max_per_hour:
            return False
        _hour_count += 1
        return True


def _post(url: str, body: Dict[str, Any]) -> None:
    def _run() -> None:
        try:
            import urllib.error
            import urllib.request

            data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": f"Minion/{__version__} ({platform.system()}; {platform.machine()})",
                },
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read(256)
        except Exception:
            log.debug("analytics POST failed", exc_info=True)

    threading.Thread(target=_run, name="minion-analytics", daemon=True).start()


def emit_session_if_ready() -> None:
    """Fire once per sidecar process when opt-in + URL are set."""
    global _session_sent
    root = telemetry_data_dir()
    if root is None:
        return
    ok, url = _remote_enabled(root)
    if not ok or not url:
        return
    with _lock:
        if _session_sent:
            return
        _session_sent = True
    body = {
        "schema": _SCHEMA,
        "event": "session",
        "install_id": _install_id(root),
        "app_version": __version__,
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    _post(url, body)


def _sanitize(kind: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if kind == "search":
        hk = fields.get("hit_kinds")
        if isinstance(hk, list):
            kinds: List[str] = [str(x) for x in hk[:24] if x is not None]
        else:
            kinds = []
        return {
            "schema": _SCHEMA,
            "event": "search",
            "returned": fields.get("returned"),
            "top_k": fields.get("top_k"),
            "mode": fields.get("mode"),
            "rerank": fields.get("rerank"),
            "hit_kinds": kinds,
            "has_kind_filter": bool(fields.get("kind_filter")),
            "has_path_glob": bool(fields.get("path_glob")),
            "has_role_filter": bool(fields.get("role")),
            "has_query": bool(fields.get("query")),
        }
    if kind == "ingest":
        reason = fields.get("reason")
        rcls: Optional[str] = None
        if isinstance(reason, str) and reason.strip():
            rcls = reason.split(":", 1)[0].strip()[:72]
            if any(sep in rcls for sep in ("/", "\\", "..")):
                rcls = "redacted_path_token"
        skipped = bool(fields.get("skipped"))
        return {
            "schema": _SCHEMA,
            "event": "ingest",
            "file_kind": fields.get("file_kind"),
            "parser": fields.get("parser"),
            "chunks": fields.get("chunks"),
            "skipped": skipped,
            "result": fields.get("result"),
            "reason_class": rcls,
        }
    return None


def on_telemetry_logged(kind: str, fields: Dict[str, Any]) -> None:
    """Hook from ``telemetry.log_event`` after the local JSONL line is written."""
    root = telemetry_data_dir()
    if root is None:
        return
    ok, url = _remote_enabled(root)
    if not ok or not url:
        return
    if kind not in ("search", "ingest"):
        return
    if not _under_hourly_cap():
        return
    body = _sanitize(kind, fields)
    if body is None:
        return
    body["install_id"] = _install_id(root)
    body["app_version"] = __version__
    body["os"] = platform.system()
    _post(url, body)
