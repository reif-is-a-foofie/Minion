#!/usr/bin/env python3
"""
Build / rebuild the semantic index for a ChatGPT export directory.

After the SQLite+vec migration, this script writes into the same
`memory.db` that the watcher and MCP read from. It registers the export
directory as a single source so re-running replaces it cleanly.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
from fastembed import TextEmbedding
from tqdm import tqdm

from chatgpt_export_reader import chunk_text, iter_messages
from store import DB_FILENAME, connect, set_meta, upsert_source


MODEL_NAME_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_BACKEND = "fastembed"
SOURCE_KIND = "chatgpt-export"
PARSER_NAME = "chatgpt-export"


@dataclass
class BuiltChunk:
    text: str
    role: str
    meta: dict


def build_chunks(
    export_dir: str,
    *,
    include_roles: Sequence[str],
    max_chars: int,
    drop_empty_text: bool = True,
) -> List[BuiltChunk]:
    chunks: List[BuiltChunk] = []
    for msg in iter_messages(export_dir, include_roles=include_roles):
        if drop_empty_text and not msg.text:
            continue
        for sub in chunk_text(msg.text, max_chars=max_chars):
            chunks.append(
                BuiltChunk(
                    text=sub,
                    role=msg.role,
                    meta={
                        "conversation_id": msg.conversation_id,
                        "conversation_title": msg.conversation_title,
                        "create_time": msg.create_time,
                        "message_id": msg.message_id,
                    },
                )
            )
    return chunks


def _embed_all(
    model: TextEmbedding, texts: List[str], *, batch_size: int
) -> np.ndarray:
    out: List[np.ndarray] = []
    total = len(texts)
    if total == 0:
        return np.zeros((0, 384), dtype=np.float32)
    with tqdm(total=total, desc="Embedding", unit="chunk") as bar:
        i = 0
        while i < total:
            batch = texts[i : i + batch_size]
            vecs = list(model.embed(batch, batch_size=batch_size))
            out.append(np.asarray(vecs, dtype=np.float32))
            i += len(batch)
            bar.update(len(batch))
    return np.concatenate(out, axis=0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build semantic index from a ChatGPT export directory."
    )
    parser.add_argument(
        "--export",
        required=True,
        help="Path to unzipped ChatGPT export root (contains conversations-*.json)",
    )
    parser.add_argument(
        "--derived-dir",
        default=str(Path(__file__).resolve().parents[1] / "data" / "derived"),
        help="Where memory.db lives (and legacy manifest.json for compat)",
    )
    parser.add_argument("--model", default=MODEL_NAME_DEFAULT, help="fastembed-compatible model name")
    parser.add_argument("--max-chars", type=int, default=1200, help="Max characters per chunk")
    parser.add_argument("--batch-size", type=int, default=64, help="Embedding batch size")
    parser.add_argument(
        "--include-assistant",
        action="store_true",
        help="Include assistant messages in the index (default: user only)",
    )
    args = parser.parse_args()

    export_dir = str(Path(args.export).expanduser().resolve())
    derived_dir = Path(args.derived_dir).expanduser().resolve()
    derived_dir.mkdir(parents=True, exist_ok=True)

    include_roles = ["user", "assistant"] if args.include_assistant else ["user"]

    print("Reading conversations and splitting into chunks (no embeddings yet)…", flush=True)
    built = build_chunks(export_dir, include_roles=include_roles, max_chars=args.max_chars)
    print(f"Done chunking: {len(built)} chunks. Loading embedding model {args.model!r} (fastembed)…", flush=True)

    texts = [b.text for b in built]
    model = TextEmbedding(model_name=args.model)
    print("Encoding chunks (progress bar)…", flush=True)
    embeddings = _embed_all(model, texts, batch_size=args.batch_size)

    dim = int(embeddings.shape[1]) if embeddings.ndim == 2 else 384
    db_path = derived_dir / DB_FILENAME
    conn = connect(db_path, embed_dim=dim)
    set_meta(conn, "model_name", args.model)
    set_meta(conn, "embedding_backend", EMBEDDING_BACKEND)

    chunk_tuples: List[Tuple[str, Optional[str], dict]] = [
        (b.text, b.role, b.meta) for b in built
    ]
    source_meta = {
        "created_at_unix": time.time(),
        "roles_indexed": include_roles,
        "max_chars": int(args.max_chars),
        "model_name": args.model,
        "embedding_backend": EMBEDDING_BACKEND,
    }

    source_id = upsert_source(
        conn,
        path=export_dir,
        kind=SOURCE_KIND,
        sha256="export-dir",
        mtime=time.time(),
        bytes_=0,
        parser=PARSER_NAME,
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
    )

    # Legacy manifest.json kept for tools that still read it.
    manifest = {
        "created_at_unix": time.time(),
        "model_name": args.model,
        "embedding_backend": EMBEDDING_BACKEND,
        "export_dir": export_dir,
        "db_path": str(db_path),
        "source_id": source_id,
        "chunk_count": len(built),
        "embedding_dim": dim,
        "roles_indexed": include_roles,
        "normalized_embeddings": True,
        "max_chars": int(args.max_chars),
    }
    (derived_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
