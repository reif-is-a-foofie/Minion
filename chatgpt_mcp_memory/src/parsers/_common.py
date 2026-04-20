"""Shared helpers for parsers (text normalization + chunking)."""
from __future__ import annotations

import re
from typing import List


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def chunk_text(text: str, *, max_chars: int = 1200) -> List[str]:
    """Paragraph-aware splitter with sentence fallback for oversize paragraphs."""
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current: List[str] = []
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

    final: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        cur: List[str] = []
        cur_len = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if cur and cur_len + len(sentence) + 1 > max_chars:
                final.append(" ".join(cur))
                cur = [sentence]
                cur_len = len(sentence)
            else:
                cur.append(sentence)
                cur_len += len(sentence) + (1 if cur_len else 0)
        if cur:
            final.append(" ".join(cur))

    return [c for c in final if c.strip()]


def window_text(text: str, *, max_chars: int = 1200) -> List[str]:
    """Hard window splitter for structured content (code, logs) where paragraph
    boundaries are unreliable."""
    text = text.replace("\r\n", "\n")
    if not text:
        return []
    lines = text.split("\n")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for line in lines:
        if cur and cur_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]
