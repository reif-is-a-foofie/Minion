"""Plain text / markdown / structured-text parser."""
from __future__ import annotations

from pathlib import Path

from . import ParsedChunk, ParseResult
from ._common import chunk_text


def parse(path: Path) -> ParseResult:
    raw = path.read_text(encoding="utf-8", errors="replace")
    chunks = [
        ParsedChunk(text=t, role=None, meta={"seq": i})
        for i, t in enumerate(chunk_text(raw))
    ]
    return ParseResult(
        chunks=chunks,
        source_meta={"suffix": path.suffix.lower()},
        kind="text",
        parser="text",
    )
