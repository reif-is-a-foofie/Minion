"""Audio / video parser via faster-whisper.

Model size controlled by $MINION_WHISPER_MODEL (default 'tiny.en').
Each transcript segment becomes one chunk with (start, end) timestamps in meta.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from . import ParsedChunk, ParseResult


_MODEL = None
_MODEL_NAME = None


def _load_model():  # pragma: no cover - lazy IO
    global _MODEL, _MODEL_NAME
    name = os.environ.get("MINION_WHISPER_MODEL", "tiny.en")
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    from faster_whisper import WhisperModel  # type: ignore

    _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
    _MODEL_NAME = name
    return _MODEL


def parse(path: Path) -> ParseResult:
    model = _load_model()
    segments_iter, info = model.transcribe(str(path), vad_filter=True)
    segments = list(segments_iter)

    chunks: List[ParsedChunk] = []
    for i, seg in enumerate(segments):
        text = (seg.text or "").strip()
        if not text:
            continue
        chunks.append(
            ParsedChunk(
                text=text,
                role=None,
                meta={
                    "seq": i,
                    "start": float(seg.start or 0.0),
                    "end": float(seg.end or 0.0),
                },
            )
        )

    return ParseResult(
        chunks=chunks,
        source_meta={
            "language": getattr(info, "language", None),
            "duration": getattr(info, "duration", None),
            "model": os.environ.get("MINION_WHISPER_MODEL", "tiny.en"),
        },
        kind="audio",
        parser="faster-whisper",
    )
