"""Layer 0 sealed raw events (PRD ambient): schema + paths + seal helpers."""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ambient_vault import VaultSession, seal_plaintext, open_ciphertext

RAW_SCHEMA_VERSION = 1
MAX_TAR_SEAL_BYTES = 20 * 1024 * 1024


def raw_events_dir(data_dir: Path) -> Path:
    p = Path(data_dir) / "memory" / "raw"
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_event_id() -> str:
    return "evt-" + secrets.token_hex(10)


def build_raw_event(
    *,
    event_id: str,
    source_type: str,
    consent_source_key: str,
    ingest_path: str,
    payload: Dict[str, Any],
    source_id: Optional[str] = None,
    layer0_key_tier: str = "dek",
) -> Dict[str, Any]:
    """Canonical plaintext JSON sealed for Layer 0 (utf-8 JSON)."""
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "event_id": event_id,
        "source_type": source_type,
        "consent_source_key": consent_source_key,
        "captured_at": time.time(),
        "ingest_path": ingest_path,
        "source_id": source_id,
        "layer0_key_tier": layer0_key_tier,
        "payload": payload,
    }


def tar_manifest_files(export_root: Path, manifest_paths: List[Path]) -> Tuple[bytes, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for m in manifest_paths:
            arc = m.resolve().relative_to(export_root.resolve()).as_posix()
            tf.add(m, arcname=arc, recursive=False)
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def seal_raw_event_json(
    data_dir: Path,
    session: VaultSession,
    event: Dict[str, Any],
    *,
    tier: str = "dek",
) -> Path:
    """Encrypt JSON event; write ``memory/raw/{event_id}.sealed``. Returns path."""
    eid = str(event["event_id"])
    st = str(event["source_type"])
    blob = seal_plaintext(
        session,
        plaintext=json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        source_type=st,
        event_id=eid,
        tier="dek" if tier != "l7k" else "l7k",
    )
    out = raw_events_dir(data_dir) / f"{eid}.sealed"
    out.write_bytes(blob)
    return out


def open_sealed_file(data_dir: Path, session: VaultSession, sealed_path: Path) -> Dict[str, Any]:
    blob = sealed_path.read_bytes()
    raw = open_ciphertext(session, blob)
    return json.loads(raw.decode("utf-8"))
