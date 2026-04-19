#!/usr/bin/env python3
import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import numpy as np
from sentence_transformers import SentenceTransformer

from chatgpt_export_reader import ChatMessage, chunk_text, iter_messages


MODEL_NAME_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass
class Chunk:
    chunk_id: str
    conversation_id: str
    conversation_title: str
    role: str
    create_time: Optional[float]
    text: str


def build_chunks(
    export_dir: str,
    *,
    include_roles: Sequence[str],
    max_chars: int,
    drop_empty_text: bool = True,
) -> List[Chunk]:
    chunks: List[Chunk] = []
    n = 0
    for msg in iter_messages(export_dir, include_roles=include_roles):
        if drop_empty_text and not msg.text:
            continue
        for sub in chunk_text(msg.text, max_chars=max_chars):
            n += 1
            if n % 50_000 == 0:
                print(f"  … built {n} text chunks so far", flush=True)
            chunks.append(
                Chunk(
                    chunk_id=f"chunk-{n:06d}",
                    conversation_id=msg.conversation_id,
                    conversation_title=msg.conversation_title,
                    role=msg.role,
                    create_time=msg.create_time,
                    text=sub,
                )
            )
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Build semantic index from a ChatGPT export directory.")
    parser.add_argument("--export", required=True, help="Path to unzipped ChatGPT export root (contains conversations-*.json)")
    parser.add_argument(
        "--derived-dir",
        default=str(Path(__file__).resolve().parents[1] / "data" / "derived"),
        help="Where to write chunks/embeddings/manifest",
    )
    parser.add_argument("--model", default=MODEL_NAME_DEFAULT, help="Sentence-transformers model name")
    parser.add_argument("--max-chars", type=int, default=1200, help="Max characters per chunk")
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

    chunks_path = derived_dir / "chunks.jsonl"
    embeddings_path = derived_dir / "embeddings.npy"
    manifest_path = derived_dir / "manifest.json"

    chunks = build_chunks(export_dir, include_roles=include_roles, max_chars=args.max_chars)
    print(f"Done chunking: {len(chunks)} chunks. Loading embedding model {args.model!r}…", flush=True)
    texts = [c.text for c in chunks]

    model = SentenceTransformer(args.model)
    print("Encoding chunks (progress bar)…", flush=True)
    embeddings = model.encode(
        texts,
        batch_size=64,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )

    with open(chunks_path, "w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    np.save(embeddings_path, embeddings)

    manifest = {
        "created_at_unix": time.time(),
        "model_name": args.model,
        "export_dir": export_dir,
        "chunks_path": str(chunks_path),
        "embeddings_path": str(embeddings_path),
        "chunk_count": len(chunks),
        "embedding_dim": int(embeddings.shape[1]) if len(embeddings.shape) == 2 else None,
        "roles_indexed": include_roles,
        "normalized_embeddings": True,
        "max_chars": int(args.max_chars),
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

