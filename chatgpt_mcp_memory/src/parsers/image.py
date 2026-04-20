"""Image parser.

Pipeline:
1) OCR via rapidocr-onnxruntime (pure Python; no tesseract system dep)
2) Optional local caption via Ollama vision model (llava/llama3.2-vision)
   when MINION_VISION_MODEL is set. Gives semantic text even when the image
   has no legible words.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import ParsedChunk, ParseResult
from ._common import chunk_text


def _ocr_rapidocr(path: Path) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:
        return ""

    ocr = RapidOCR()
    result, _ = ocr(str(path))
    if not result:
        return ""
    lines = [segment[1] for segment in result if segment and len(segment) >= 2]
    return "\n".join(s for s in lines if s and s.strip())


def _caption_ollama(path: Path, model: str) -> Optional[str]:
    try:
        import ollama  # type: ignore
    except Exception:
        return None

    try:
        resp = ollama.chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Describe this image for search indexing: objects, "
                    "visible text, context, anything distinctive. Be factual, concise.",
                    "images": [str(path)],
                }
            ],
        )
        return (resp.get("message") or {}).get("content") or None
    except Exception:
        return None


def parse(path: Path) -> ParseResult:
    ocr_text = _ocr_rapidocr(path)
    caption: Optional[str] = None
    vision_model = os.environ.get("MINION_VISION_MODEL", "").strip()
    if vision_model:
        caption = _caption_ollama(path, vision_model)

    parts: List[str] = []
    if caption:
        parts.append(f"[caption]\n{caption}")
    if ocr_text:
        parts.append(f"[ocr]\n{ocr_text}")
    combined = "\n\n".join(parts)

    chunks = [
        ParsedChunk(text=t, role=None, meta={"seq": i})
        for i, t in enumerate(chunk_text(combined))
    ]
    return ParseResult(
        chunks=chunks,
        source_meta={
            "ocr": bool(ocr_text),
            "caption_model": vision_model or None,
        },
        kind="image",
        parser="rapidocr" + ("+ollama" if caption else ""),
    )
