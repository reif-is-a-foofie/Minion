"""
Ingestion pipeline: file path -> parser -> fastembed -> SQLite store.

This is the single choke-point every writer uses (watcher, `minion add`,
rebuild scripts). Keep it tiny and side-effect-free apart from DB writes
and model load.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from parsers import ParseResult, UnsupportedFile, parse_file
from store import sha256_of_file, upsert_source


DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_NAME: Optional[str] = None


def _get_model(name: str):
    """Cache the fastembed model. Safe to call from multiple threads."""
    global _MODEL, _MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == name:
            return _MODEL
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name=name)
        _MODEL_NAME = name
        return _MODEL


@dataclass
class IngestResult:
    path: str
    source_id: Optional[str]
    kind: str
    parser: str
    chunk_count: int
    skipped: bool
    reason: Optional[str] = None


def _embed(model, texts: List[str], *, batch_size: int = 64) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    out: List[np.ndarray] = []
    i = 0
    while i < len(texts):
        batch = texts[i : i + batch_size]
        vecs = list(model.embed(batch, batch_size=batch_size))
        out.append(np.asarray(vecs, dtype=np.float32))
        i += len(batch)
    return np.concatenate(out, axis=0)


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str] = None,
    force: bool = False,
) -> IngestResult:
    """
    Parse + embed + upsert a single file. Skips unchanged files (same sha256)
    unless force=True. Returns an IngestResult describing what happened.
    """
    path = Path(path).expanduser().resolve()
    spath = str(path)
    if not path.exists() or not path.is_file():
        return IngestResult(spath, None, "?", "?", 0, True, reason="missing")

    digest = sha256_of_file(path)
    if not force:
        row = conn.execute(
            "SELECT sha256 FROM sources WHERE path=?", (spath,)
        ).fetchone()
        if row and row["sha256"] == digest:
            return IngestResult(spath, None, "?", "?", 0, True, reason="unchanged")

    try:
        result: ParseResult = parse_file(path)
    except UnsupportedFile as e:
        return IngestResult(spath, None, "?", "?", 0, True, reason=str(e))
    except Exception as e:
        return IngestResult(spath, None, "?", "?", 0, True, reason=f"parse-error: {e}")

    if not result.chunks:
        return IngestResult(spath, None, result.kind, result.parser, 0, True, reason="no-text")

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    stat = path.stat()
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", path.suffix.lower())
    source_meta.setdefault("model_name", name)

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind,
        sha256=digest,
        mtime=stat.st_mtime,
        bytes_=stat.st_size,
        parser=result.parser,
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind,
        parser=result.parser,
        chunk_count=len(result.chunks),
        skipped=False,
    )
