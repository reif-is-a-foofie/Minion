"""Image parser.

Pipeline:
1) Preflight with PIL — rejects Mac aliases, truncated files, and non-images
   fast with a clear reason.
2) OCR via rapidocr-onnxruntime (pure Python; no tesseract system dep).
3) Optional local caption via Ollama vision model (llava/llama3.2-vision/
   moondream) when MINION_VISION_MODEL is set. Gives semantic text even when
   the image has no legible words. Retries once on a runner crash (500) since
   the ollama runner auto-respawns after a crash.

Error handling is explicit so the ingest layer can surface actionable
skip reasons (missing-deps vs no-text vs genuinely-empty vs bad-file).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import ParsedChunk, ParseResult
from ._common import chunk_text


log = logging.getLogger("minion.parsers.image")


class EmptyParse(ValueError):
    """Raised so ingest.py can show a clean skip reason without a stack trace."""


def _preflight_image(path: Path) -> Optional[str]:
    """Return a skip reason if `path` is not a real image we can decode, else None.

    Catches macOS Finder aliases/bookmarks (common in ChatGPT exports and
    screenshot folders), truncated downloads, and zero-byte files before
    handing them to OCR/caption pipelines that would fail opaquely.
    """
    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore
    except Exception:
        return None  # PIL missing is not our problem here; rapidocr will error
    try:
        with Image.open(path) as im:
            im.verify()
        return None
    except UnidentifiedImageError:
        # Check for Apple alias magic so we can give a pinpointed reason.
        try:
            with open(path, "rb") as f:
                head = f.read(16)
            if head.startswith(b"book") and b"mark" in head:
                return (
                    "not an image file: macOS Finder alias/bookmark "
                    "(the .png extension is misleading; resolve the alias to its target)"
                )
        except Exception:
            pass
        return f"not a valid image file (cannot decode {path.suffix or 'payload'})"
    except Exception as e:
        return f"image unreadable ({type(e).__name__}: {e})"


_OCR_SINGLETON = None
_OCR_IMPORT_FAILED: Optional[str] = None


def _get_ocr():
    """Lazy-initialise RapidOCR once per process (weights are ~50MB)."""
    global _OCR_SINGLETON, _OCR_IMPORT_FAILED
    if _OCR_SINGLETON is not None:
        return _OCR_SINGLETON
    if _OCR_IMPORT_FAILED is not None:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception as e:
        _OCR_IMPORT_FAILED = f"rapidocr-onnxruntime not installed ({e})"
        return None
    try:
        _OCR_SINGLETON = RapidOCR()
    except Exception as e:
        _OCR_IMPORT_FAILED = f"rapidocr init failed ({e})"
        return None
    return _OCR_SINGLETON


def _ocr_rapidocr(path: Path) -> Tuple[str, Optional[str]]:
    """Return (text, error). error=None means OCR ran successfully (may be empty)."""
    ocr = _get_ocr()
    if ocr is None:
        return "", _OCR_IMPORT_FAILED or "rapidocr unavailable"
    try:
        result, _ = ocr(str(path))
    except Exception as e:
        return "", f"rapidocr runtime error: {e}"
    if not result:
        return "", None
    lines = [seg[1] for seg in result if seg and len(seg) >= 2]
    return "\n".join(s for s in lines if s and s.strip()), None


def _caption_ollama(path: Path, model: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (caption, error). error=None on success (caption may still be empty).

    Retries once on a runner crash (HTTP 500) since the ollama runner
    auto-respawns; the first request after a crash often succeeds.
    """
    try:
        import ollama  # type: ignore
    except Exception as e:
        return None, f"ollama python client not installed ({e})"
    messages = [
        {
            "role": "user",
            "content": (
                "Describe this image for search indexing: objects, "
                "visible text, context, anything distinctive. "
                "Be factual, concise."
            ),
            "images": [str(path)],
        }
    ]
    last_err: Optional[str] = None
    for attempt in range(2):
        try:
            resp = ollama.chat(model=model, messages=messages)
            text = (resp.get("message") or {}).get("content")
            return (text or None), None
        except Exception as e:
            last_err = str(e)
            # Runner crashes surface as 500; give ollama a moment to respawn.
            if "500" in last_err or "runner" in last_err.lower():
                time.sleep(1.5)
                continue
            break
    return None, f"ollama call failed ({last_err})"


def parse(path: Path) -> ParseResult:
    # Fast-fail on non-images (Mac aliases, truncated downloads, etc.) before
    # firing up heavy OCR/caption pipelines that would error opaquely.
    bad = _preflight_image(path)
    if bad:
        raise EmptyParse(bad)

    ocr_text, ocr_err = _ocr_rapidocr(path)

    vision_model = os.environ.get("MINION_VISION_MODEL", "").strip()
    caption: Optional[str] = None
    caption_err: Optional[str] = None
    if vision_model:
        caption, caption_err = _caption_ollama(path, vision_model)

    parts: List[str] = []
    if caption:
        parts.append(f"[caption]\n{caption}")
    if ocr_text:
        parts.append(f"[ocr]\n{ocr_text}")
    combined = "\n\n".join(parts)

    if not combined.strip():
        # Nothing to embed. The messages below are deliberately infrastructural
        # -- never tell the user to run a CLI command. The app owns vision
        # setup; its job is to spin up ollama + pull the model. If that's in
        # flight, we tag the skip as `deferred:` so the watcher can re-attempt
        # once the sidecar is restarted with MINION_VISION_MODEL wired up.
        if not vision_model:
            # No vision backend yet -> not a real failure, just early.
            raise EmptyParse("deferred: awaiting vision model (no text in image yet)")
        if caption_err:
            # Vision is supposed to be on, but the call failed. Still deferred
            # -- a transient runner crash / still-downloading model shouldn't
            # burn the source permanently.
            raise EmptyParse(f"deferred: vision model '{vision_model}' not ready ({caption_err})")
        # Vision ran and produced nothing meaningful: that's a real empty.
        raise EmptyParse(f"image: no readable text and no caption from '{vision_model}'")

    chunks = [
        ParsedChunk(text=t, role=None, meta={"seq": i})
        for i, t in enumerate(chunk_text(combined))
    ]
    parser_name = "rapidocr" if ocr_text else ""
    if caption:
        parser_name = f"{parser_name}+ollama" if parser_name else "ollama"
    return ParseResult(
        chunks=chunks,
        source_meta={
            "ocr": bool(ocr_text),
            "caption_model": vision_model or None,
        },
        kind="image",
        parser=parser_name or "image",
    )
