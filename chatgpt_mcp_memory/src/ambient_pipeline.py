"""Encrypt-at-ingest hooks (ChatGPT export first)."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ambient_session import get_session
from consent import is_ambient_globally_enabled, is_source_enabled, merge_ambient_defaults
from ingest import _chatgpt_export_manifest_paths, tar_manifest_files
from layer0 import MAX_TAR_SEAL_BYTES, build_raw_event, new_event_id, seal_raw_event_json
from settings import load_settings

log = logging.getLogger("minion.ambient")


def _safe_delete_tree(path: Path, allowed_roots: List[Path]) -> bool:
    rp = path.resolve()
    for root in allowed_roots:
        try:
            rp.relative_to(root.resolve())
            shutil.rmtree(rp, ignore_errors=True)
            return True
        except ValueError:
            continue
    return False


def maybe_seal_chatgpt_export_after_ingest(
    conn: sqlite3.Connection,
    data_dir: Path,
    export_path: Path,
    *,
    source_id: str,
    ingest_digest: str,
    inbox: Optional[Path] = None,
) -> Optional[str]:
    """If ambient crypto + consent allow, seal manifest tar to Layer 0. Returns event_id or None."""
    settings = load_settings(data_dir)
    amb = merge_ambient_defaults(settings.get("ambient"))
    if not is_ambient_globally_enabled(settings):
        return None
    if not is_source_enabled(settings, "chatgpt_export"):
        return None
    sess = get_session()
    if sess is None:
        return None

    manifests = _chatgpt_export_manifest_paths(export_path)
    if not manifests:
        return None
    try:
        tar_bytes, digest = tar_manifest_files(export_path, manifests)
    except Exception:
        log.exception("ambient: tar manifests failed")
        return None

    rel_paths = [m.resolve().relative_to(export_path.resolve()).as_posix() for m in manifests]
    if len(tar_bytes) <= MAX_TAR_SEAL_BYTES:
        payload: Dict[str, Any] = {
            "format": "tar_manifests_v1",
            "export_digest": ingest_digest,
            "tar_sha256": digest,
            "rel_paths": rel_paths,
            "tar_b64": base64.b64encode(tar_bytes).decode("ascii"),
        }
    else:
        files_meta = []
        for m in manifests:
            raw_f = m.read_bytes()
            files_meta.append(
                {
                    "rel": m.resolve().relative_to(export_path.resolve()).as_posix(),
                    "sha256": hashlib.sha256(raw_f).hexdigest(),
                    "size": len(raw_f),
                }
            )
        payload = {
            "format": "manifest_digest_v1",
            "export_digest": ingest_digest,
            "tar_sha256": digest,
            "rel_paths": rel_paths,
            "manifests": files_meta,
        }

    eid = new_event_id()
    event = build_raw_event(
        event_id=eid,
        source_type="chatgpt_export",
        consent_source_key="chatgpt_export",
        ingest_path=str(export_path.resolve()),
        payload=payload,
        source_id=source_id,
        layer0_key_tier="dek",
    )
    try:
        seal_raw_event_json(data_dir, sess, event, tier="dek")
    except Exception:
        log.exception("ambient: seal failed for %s", export_path)
        return None

    try:
        row = conn.execute("SELECT meta_json FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row:
            meta = json.loads(row["meta_json"] or "{}")
            ids = list(meta.get("layer0_event_ids") or [])
            ids.append(eid)
            meta["layer0_event_ids"] = ids
            conn.execute(
                "UPDATE sources SET meta_json=?, updated_at=? WHERE source_id=?",
                (json.dumps(meta, ensure_ascii=False), time.time(), source_id),
            )
    except Exception:
        log.exception("ambient: failed to attach layer0_event_ids")

    if amb.get("delete_plaintext_export_after_seal"):
        roots: List[Path] = [data_dir.resolve()]
        if inbox is not None:
            roots.append(inbox.resolve())
        if _safe_delete_tree(export_path, roots):
            log.info("ambient: removed plaintext export after seal: %s", export_path)

    return eid
