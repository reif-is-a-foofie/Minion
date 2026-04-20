#!/usr/bin/env python3
import glob
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
from fastembed import TextEmbedding


DEFAULT_EXPORT_DIR = "/Users/reify/Classified/minion/d9eced211a2a0b9cd1b2d52f595ee063aea7ff88cddbcaad683ae4aa25df7992-2026-03-21-20-25-15-b54da2dcfb0a4295aae2c768c88d320c"
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
CHUNKS_PATH = os.path.join(AGENT_DIR, "memory_chunks.jsonl")
EMBEDDINGS_PATH = os.path.join(AGENT_DIR, "memory_embeddings.npy")
MANIFEST_PATH = os.path.join(AGENT_DIR, "memory_manifest.json")


@dataclass
class Chunk:
    chunk_id: str
    conversation_id: str
    conversation_title: str
    role: str
    create_time: Optional[float]
    text: str


def normalize_text(text):
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def is_persona_relevant(text):
    lowered = text.lower()
    if len(text) < 30:
        return False

    noise = [
        "http://",
        "https://",
        "localhost:",
        "traceback",
        "csv",
        "json",
        "dataframe",
        "countif(",
        "source conversation:",
        "poll ",
        "quiz ",
        "form ",
        "anonymous learner",
        "@csus.edu",
        "screenshot",
        "full url",
        "plain text for this",
        "fix this code",
        "write a histogram",
    ]
    if any(marker in lowered for marker in noise):
        return False

    first_person = [
        " i ",
        " i'm",
        "i'm ",
        " i’m",
        "my ",
        " me ",
        " myself",
    ]
    padded = f" {lowered} "
    if not any(marker in padded for marker in first_person):
        return False

    persona_signals = [
        "i am",
        "i'm",
        "i want",
        "i prefer",
        "i like",
        "i love",
        "i believe",
        "my goal",
        "my family",
        "my wife",
        "my mission",
        "my purpose",
        "my values",
        "faith",
        "calling",
        "present with my family",
        "responsible in how i spend",
        "saving money",
        "learn new things",
        "phone",
        "social media",
        "focus intensely",
        "company",
        "good capital",
        "gfp",
        "operator",
        "build",
        "working on",
    ]
    return any(signal in lowered for signal in persona_signals)


def chunk_text(text, max_chars=1400):
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current and current_len + para_len + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len + (2 if current_len else 0)

    if current:
        chunks.append("\n\n".join(current))

    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final_chunks.append(chunk)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        current = []
        current_len = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and current_len + len(sentence) + 1 > max_chars:
                final_chunks.append(" ".join(current))
                current = [sentence]
                current_len = len(sentence)
            else:
                current.append(sentence)
                current_len += len(sentence) + (1 if current_len else 0)
        if current:
            final_chunks.append(" ".join(current))

    return [c for c in final_chunks if c.strip()]


def get_linear_path(mapping, current_node):
    path = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id)
        if not node:
            break
        path.append(node_id)
        node_id = node.get("parent")
    return list(reversed(path))


def extract_text(parts):
    texts = []
    for part in parts:
        if isinstance(part, str):
            part = part.strip()
            if part:
                texts.append(part)
    return "\n".join(texts).strip()


def load_conversations():
    export_dir = os.environ.get("CHATGPT_EXPORT_DIR") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXPORT_DIR)
    files = sorted(glob.glob(os.path.join(export_dir, "conversations-*.json")))
    conversations = []
    for path in files:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            conversations.extend(data)
        else:
            conversations.append(data)
    return conversations, export_dir


def build_chunks():
    conversations, export_dir = load_conversations()
    chunks = []
    chunk_num = 0

    for conv in conversations:
        mapping = conv.get("mapping", {})
        current_node = conv.get("current_node")
        if not mapping or not current_node:
            continue

        title = conv.get("title") or "(untitled)"
        conv_id = conv.get("id") or conv.get("conversation_id") or "unknown"

        for node_id in get_linear_path(mapping, current_node):
            node = mapping.get(node_id) or {}
            msg = node.get("message") or {}
            role = msg.get("author", {}).get("role")
            include_assistant = os.environ.get("INCLUDE_ASSISTANT", "").lower() in {"1", "true", "yes"}
            allowed_roles = {"user", "assistant"} if include_assistant else {"user"}
            if role not in allowed_roles:
                continue
            content = msg.get("content") or {}
            if content.get("content_type") != "text":
                continue
            text = extract_text(content.get("parts") or [])
            text = normalize_text(text)
            if not text:
                continue
            if role == "user" and not is_persona_relevant(text):
                continue
            for subchunk in chunk_text(text):
                chunk_num += 1
                chunks.append(
                    Chunk(
                        chunk_id=f"chunk-{chunk_num:06d}",
                        conversation_id=conv_id,
                        conversation_title=title,
                        role=role,
                        create_time=msg.get("create_time"),
                        text=subchunk,
                    )
                )
    return chunks, export_dir


def main():
    os.makedirs(AGENT_DIR, exist_ok=True)
    chunks, export_dir = build_chunks()

    model = TextEmbedding(model_name=MODEL_NAME)
    texts = [chunk.text for chunk in chunks]
    vecs = list(model.embed(texts, batch_size=64))
    embeddings = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    embeddings = embeddings / norms

    with open(CHUNKS_PATH, "w") as fh:
        for chunk in chunks:
            fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")

    np.save(EMBEDDINGS_PATH, embeddings)

    manifest = {
        "model_name": MODEL_NAME,
        "export_dir": export_dir,
        "chunks_path": CHUNKS_PATH,
        "embeddings_path": EMBEDDINGS_PATH,
        "chunk_count": len(chunks),
        "embedding_dim": int(embeddings.shape[1]) if len(embeddings.shape) == 2 else None,
        "roles_indexed": ["user", "assistant"] if os.environ.get("INCLUDE_ASSISTANT", "").lower() in {"1", "true", "yes"} else ["user"],
        "normalized_embeddings": True,
    }
    with open(MANIFEST_PATH, "w") as fh:
        json.dump(manifest, fh, indent=2)

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
