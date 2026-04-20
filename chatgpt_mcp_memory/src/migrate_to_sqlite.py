#!/usr/bin/env python3
"""
One-shot migration: chunks.jsonl + embeddings.npy  ->  memory.db

Imports the existing flat index as a single synthetic source
(path=<export_dir>, kind='chatgpt-export') so search still works
immediately after migration without re-embedding. Idempotent:
re-running with the same derived dir replaces the synthetic source.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from store import (
    DB_FILENAME,
    connect,
    set_meta,
    source_id_for,
    transaction,
    _vec_blob,
    _l2_normalise,
    chunk_id_for,
)


SYNTHETIC_PARSER = "chatgpt-export-legacy"
SYNTHETIC_KIND = "chatgpt-export"


def _load_manifest(derived_dir: Path) -> Dict[str, Any]:
    p = derived_dir / "manifest.json"
    if not p.exists():
        raise FileNotFoundError(f"Missing manifest: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _load_chunks(derived_dir: Path) -> List[Dict[str, Any]]:
    p = derived_dir / "chunks.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"Missing chunks: {p}")
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _load_embeddings(derived_dir: Path) -> np.ndarray:
    p = derived_dir / "embeddings.npy"
    if not p.exists():
        raise FileNotFoundError(f"Missing embeddings: {p}")
    return np.load(p)


def migrate(derived_dir: Path) -> Tuple[Path, int]:
    """Returns (db_path, chunks_migrated)."""
    derived_dir = derived_dir.expanduser().resolve()
    manifest = _load_manifest(derived_dir)
    chunks = _load_chunks(derived_dir)
    embeddings = _load_embeddings(derived_dir)

    if len(chunks) != embeddings.shape[0]:
        raise ValueError(
            f"count mismatch chunks={len(chunks)} embeddings={embeddings.shape[0]}"
        )

    dim = int(embeddings.shape[1]) if embeddings.ndim == 2 else 0
    if dim == 0:
        raise ValueError("embeddings.npy has no data to migrate")

    db_path = derived_dir / DB_FILENAME
    conn = connect(db_path, embed_dim=dim)
    set_meta(conn, "model_name", str(manifest.get("model_name", "")))
    set_meta(conn, "embedding_backend", str(manifest.get("embedding_backend", "fastembed")))

    synthetic_path = str(manifest.get("export_dir") or derived_dir)
    sid = source_id_for(synthetic_path)
    now = time.time()
    embeddings = _l2_normalise(embeddings.astype(np.float32, copy=False))

    source_meta = {
        "migrated_from": "chunks.jsonl+embeddings.npy",
        "roles_indexed": manifest.get("roles_indexed"),
        "max_chars": manifest.get("max_chars"),
        "created_at_unix": manifest.get("created_at_unix"),
    }

    with transaction(conn):
        prior = conn.execute(
            "SELECT rowid FROM chunks WHERE source_id=?", (sid,)
        ).fetchall()
        for r in prior:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (int(r["rowid"]),))
        conn.execute("DELETE FROM sources WHERE source_id=?", (sid,))

        conn.execute(
            "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                synthetic_path,
                SYNTHETIC_KIND,
                "legacy-import",
                float(manifest.get("created_at_unix") or now),
                0,
                SYNTHETIC_PARSER,
                json.dumps(source_meta, ensure_ascii=False),
                now,
            ),
        )

        # Preserve the original chunk_id where present (keeps back-references valid),
        # otherwise synthesize one.
        for seq, (c, emb) in enumerate(zip(chunks, embeddings)):
            cid = str(c.get("chunk_id") or chunk_id_for(sid, seq))
            cmeta = {
                "conversation_id": c.get("conversation_id"),
                "conversation_title": c.get("conversation_title"),
                "create_time": c.get("create_time"),
            }
            cur = conn.execute(
                "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    sid,
                    seq,
                    c.get("role"),
                    c.get("text") or "",
                    json.dumps(cmeta, ensure_ascii=False),
                ),
            )
            rid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                (rid, _vec_blob(emb)),
            )

    return db_path, len(chunks)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--derived-dir",
        default=str(Path(__file__).resolve().parents[1] / "data" / "derived"),
        help="Directory containing manifest.json / chunks.jsonl / embeddings.npy",
    )
    args = p.parse_args()

    derived = Path(args.derived_dir)
    db_path, n = migrate(derived)
    print(json.dumps({"db": str(db_path), "chunks_migrated": n}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
