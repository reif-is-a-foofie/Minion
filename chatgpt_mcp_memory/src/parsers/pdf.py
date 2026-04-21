"""PDF parser. pypdf fast path; pdfminer.six fallback for tricky layouts.

Distinguishes three failure modes so the UI can show actionable hints:
- Missing dep  -> raises EmptyParse("missing-deps: ...") so caller can prompt
                  the user to `pip install -r requirements-docs.txt`.
- Image-only   -> raises EmptyParse("image-only: N pages, no selectable text.
                  OCR it first (see requirements-images.txt for OCR support).")
- Real parse error -> re-raised so ingest surfaces "parse-error: ...".
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from . import ParsedChunk, ParseResult
from ._common import chunk_text


class EmptyParse(Exception):
    """Raised when the PDF produced no text for a diagnosable reason."""


def _import_pypdf():
    try:
        from pypdf import PdfReader  # type: ignore
        return PdfReader
    except ImportError:
        return None


def _import_pdfminer():
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return extract_text
    except ImportError:
        return None


def _extract_pypdf(path: Path) -> tuple[List[tuple[int, str]], int]:
    """Returns (pages_with_text, total_page_count)."""
    PdfReader = _import_pypdf()
    if PdfReader is None:
        raise EmptyParse("missing-deps: install `pip install -r requirements-docs.txt` to enable PDF text extraction")
    reader = PdfReader(str(path))
    total = len(reader.pages)
    out: List[tuple[int, str]] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            out.append((i + 1, text))
    return out, total


def _extract_pdfminer(path: Path) -> List[tuple[int, str]]:
    extract_text = _import_pdfminer()
    if extract_text is None:
        return []
    text = extract_text(str(path)) or ""
    return [(1, text)] if text.strip() else []


def parse(path: Path) -> ParseResult:
    pages: List[tuple[int, str]] = []
    total_pages = 0
    extractor = "pypdf"
    try:
        pages, total_pages = _extract_pypdf(path)
    except EmptyParse:
        raise
    except Exception as e:  # real parse error -- surface it
        raise RuntimeError(f"pypdf failed: {e}") from e

    if not pages:
        try:
            pages = _extract_pdfminer(path)
            extractor = "pdfminer.six"
        except Exception as e:
            raise RuntimeError(f"pdfminer failed: {e}") from e

    if not pages:
        # Both extractors succeeded but found zero text -- almost always an
        # image-only/scanned PDF (e.g. slide decks exported as PDF).
        hint = (
            f"image-only PDF: {total_pages} page(s) with no selectable text. "
            "OCR it (e.g. `ocrmypdf in.pdf out.pdf`) and re-drop."
        )
        raise EmptyParse(hint)

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
        source_meta={"extractor": extractor, "pages": len(pages), "total_pages": total_pages},
        kind="pdf",
        parser=extractor,
    )
