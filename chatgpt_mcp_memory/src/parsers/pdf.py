"""PDF parser. pypdf fast path; pdfminer.six fallback for tricky layouts."""
from __future__ import annotations

from pathlib import Path
from typing import List

from . import ParsedChunk, ParseResult
from ._common import chunk_text


def _extract_pypdf(path: Path) -> List[tuple[int, str]]:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    out: List[tuple[int, str]] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            out.append((i + 1, text))
    return out


def _extract_pdfminer(path: Path) -> List[tuple[int, str]]:
    from pdfminer.high_level import extract_text  # type: ignore

    text = extract_text(str(path)) or ""
    return [(1, text)] if text.strip() else []


def parse(path: Path) -> ParseResult:
    pages: List[tuple[int, str]] = []
    extractor = "pypdf"
    try:
        pages = _extract_pypdf(path)
    except Exception:
        pages = []

    if not pages:
        try:
            pages = _extract_pdfminer(path)
            extractor = "pdfminer.six"
        except Exception:
            pages = []

    chunks: List[ParsedChunk] = []
    seq = 0
    for page_no, text in pages:
        for c in chunk_text(text):
            chunks.append(
                ParsedChunk(
                    text=c,
                    role=None,
                    meta={"seq": seq, "page": page_no},
                )
            )
            seq += 1

    return ParseResult(
        chunks=chunks,
        source_meta={"extractor": extractor, "pages": len(pages)},
        kind="pdf",
        parser=extractor,
    )
