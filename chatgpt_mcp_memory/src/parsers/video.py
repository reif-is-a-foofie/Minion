"""Video parser: scene-aware fusion of transcript + keyframe OCR + caption.

Pipeline per file:
  1. Transcribe the audio track once (faster-whisper) -> timestamped segments.
  2. Detect shot boundaries with PySceneDetect (ContentDetector).
     Fallback: fixed 30s windows when scenedetect/opencv aren't installed.
  3. For each scene:
       - bucket the whisper segments whose [start, end] falls inside the scene
       - extract one midpoint keyframe via ffmpeg
       - OCR the keyframe with rapidocr (reuses parsers.image helpers)
       - caption the keyframe with ollama vision model if MINION_VISION_MODEL set
       - emit one ParsedChunk: `[transcript]\n...\n[ocr]\n...\n[caption]\n...`

Each chunk's `meta` carries (start, end, keyframe_t) so the retriever can
jump to the right moment. If every branch fails for a scene we still emit
the transcript alone, so a video with no detectable visuals still indexes.

Deps:
  - faster-whisper              (required; shared with parsers.audio)
  - ffmpeg on PATH              (required; faster-whisper needs it too)
  - scenedetect[opencv]         (optional; fallback: fixed windows)
  - rapidocr-onnxruntime        (optional; OCR skipped if missing)
  - ollama + $MINION_VISION_MODEL  (optional; captioning skipped if unset)
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import ParsedChunk, ParseResult
from ._common import chunk_text
from .image import _caption_ollama, _ocr_rapidocr  # reuse the OCR/caption path


log = logging.getLogger("minion.parsers.video")


# Fallback scene length when PySceneDetect isn't available. 30s is short
# enough that you don't lose a slide change inside one chunk, long enough
# that typical talking-head video isn't shredded into pointless micro-chunks.
_FALLBACK_SCENE_SEC = 30.0


# ---------------------------------------------------------------------------
# Whisper (shared with parsers.audio, but re-imported here so this module
# stands alone). Lazy-loaded; weights are cached.
# ---------------------------------------------------------------------------
_WHISPER_MODEL = None
_WHISPER_NAME: Optional[str] = None


def _load_whisper():
    global _WHISPER_MODEL, _WHISPER_NAME
    name = os.environ.get("MINION_WHISPER_MODEL", "tiny.en")
    if _WHISPER_MODEL is not None and _WHISPER_NAME == name:
        return _WHISPER_MODEL
    from faster_whisper import WhisperModel  # type: ignore

    _WHISPER_MODEL = WhisperModel(name, device="cpu", compute_type="int8")
    _WHISPER_NAME = name
    return _WHISPER_MODEL


def _transcribe_all(path: Path) -> Tuple[List[Tuple[float, float, str]], dict]:
    """Return (segments, info_meta). segments: [(start, end, text), ...]."""
    model = _load_whisper()
    segments_iter, info = model.transcribe(str(path), vad_filter=True)
    segs: List[Tuple[float, float, str]] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segs.append((float(seg.start or 0.0), float(seg.end or 0.0), text))
    return segs, {
        "language": getattr(info, "language", None),
        "duration": getattr(info, "duration", None),
        "model": os.environ.get("MINION_WHISPER_MODEL", "tiny.en"),
    }


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------
def _detect_scenes_scenedetect(path: Path) -> Optional[List[Tuple[float, float]]]:
    """Return [(start, end), ...] in seconds, or None if scenedetect missing/failed."""
    try:
        from scenedetect import detect, ContentDetector  # type: ignore
    except Exception:
        return None
    try:
        scene_list = detect(str(path), ContentDetector())
    except Exception as e:
        log.warning("scenedetect failed on %s: %s", path, e)
        return None
    if not scene_list:
        return None
    return [(s[0].get_seconds(), s[1].get_seconds()) for s in scene_list]


def _detect_scenes_fallback(duration: Optional[float]) -> List[Tuple[float, float]]:
    """Fixed-window scenes when scenedetect is unavailable or returns nothing."""
    if not duration or duration <= 0:
        # Single-scene degenerate case: one chunk covering the whole file.
        return [(0.0, float("inf"))]
    out: List[Tuple[float, float]] = []
    t = 0.0
    while t < duration:
        out.append((t, min(t + _FALLBACK_SCENE_SEC, duration)))
        t += _FALLBACK_SCENE_SEC
    return out


def _detect_scenes(path: Path, duration: Optional[float]) -> List[Tuple[float, float]]:
    scenes = _detect_scenes_scenedetect(path)
    if scenes:
        return scenes
    return _detect_scenes_fallback(duration)


# ---------------------------------------------------------------------------
# Keyframe extraction (ffmpeg subprocess; faster-whisper already requires ffmpeg)
# ---------------------------------------------------------------------------
_FFMPEG = shutil.which("ffmpeg")


def _extract_keyframe(path: Path, at_sec: float, out_path: Path) -> bool:
    """Grab one frame at `at_sec` into `out_path` as PNG. Returns True on success."""
    if _FFMPEG is None:
        return False
    try:
        # -ss before -i seeks fast (uses keyframe index); acceptable precision
        # for OCR/caption. 640px wide is enough for both.
        subprocess.run(
            [
                _FFMPEG,
                "-nostdin", "-loglevel", "error",
                "-ss", f"{max(0.0, at_sec):.3f}",
                "-i", str(path),
                "-frames:v", "1",
                "-vf", "scale=640:-2",
                "-y", str(out_path),
            ],
            check=True,
            timeout=30,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.debug("ffmpeg keyframe extract failed at %.1fs: %s", at_sec, e)
        return False


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
def parse(path: Path, *, on_progress: Optional[Callable] = None) -> ParseResult:
    def _emit(stage: str, **info):
        if on_progress is None:
            return
        try:
            on_progress(stage, info)
        except Exception:
            pass

    _emit("transcribe", done=0, total=1)
    segments, audio_meta = _transcribe_all(path)
    _emit("transcribe", done=1, total=1)

    duration = audio_meta.get("duration") or 0.0
    scenes = _detect_scenes(path, duration)
    # Clamp trailing inf (degenerate case) to duration or last segment end.
    if scenes and scenes[-1][1] == float("inf"):
        last_end = max((e for _, e, _ in segments), default=duration or 0.0)
        scenes[-1] = (scenes[-1][0], last_end or scenes[-1][0] + _FALLBACK_SCENE_SEC)

    vision_model = os.environ.get("MINION_VISION_MODEL", "").strip()
    total_scenes = len(scenes)
    ocr_ok = 0
    caption_ok = 0
    chunks: List[ParsedChunk] = []

    # All keyframes land under one temp dir; nuked at the end regardless of path.
    with tempfile.TemporaryDirectory(prefix="minion-vid-") as tmpdir:
        tmp = Path(tmpdir)
        for idx, (start, end) in enumerate(scenes):
            _emit("scene", done=idx, total=total_scenes)

            # 1. Transcript for this scene
            transcript_lines = [
                t for (s, e, t) in segments
                if s < end and e > start  # overlap test
            ]
            transcript = " ".join(transcript_lines).strip()

            # 2. Keyframe at scene midpoint
            mid = (start + end) / 2.0
            kf = tmp / f"scene_{idx:04d}.png"
            have_kf = _extract_keyframe(path, mid, kf)

            # 3. OCR + caption (both reuse image.py helpers)
            ocr_text = ""
            caption: Optional[str] = None
            if have_kf:
                ocr_text, _ = _ocr_rapidocr(kf)
                if ocr_text:
                    ocr_ok += 1
                if vision_model:
                    caption, _ = _caption_ollama(kf, vision_model)
                    if caption:
                        caption_ok += 1

            # 4. Merge. Labels are lowercase+bracketed so embeddings don't
            #    blow up on stray ALL-CAPS headings.
            parts: List[str] = []
            if transcript:
                parts.append(f"[transcript]\n{transcript}")
            if ocr_text:
                parts.append(f"[on-screen]\n{ocr_text}")
            if caption:
                parts.append(f"[visual]\n{caption}")
            if not parts:
                continue  # nothing extractable from this scene; drop it

            combined = "\n\n".join(parts)
            meta = {
                "seq": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "keyframe_t": round(mid, 3),
                "has_transcript": bool(transcript),
                "has_ocr": bool(ocr_text),
                "has_caption": bool(caption),
            }
            # Long-form scenes (rare) get split into sub-chunks; short ones emit
            # one chunk each. We preserve meta on every sub-chunk so jump-to-time
            # keeps working even after chunking.
            for sub_i, piece in enumerate(chunk_text(combined)):
                m = dict(meta)
                if sub_i:
                    m["sub_seq"] = sub_i
                chunks.append(ParsedChunk(text=piece, role=None, meta=m))

    _emit("scene", done=total_scenes, total=total_scenes)

    parser_bits = ["faster-whisper"]
    if ocr_ok:
        parser_bits.append("rapidocr")
    if caption_ok:
        parser_bits.append("ollama")
    parser_name = "+".join(parser_bits)

    return ParseResult(
        chunks=chunks,
        source_meta={
            **audio_meta,
            "scenes": total_scenes,
            "scenes_with_ocr": ocr_ok,
            "scenes_with_caption": caption_ok,
            "caption_model": vision_model or None,
        },
        kind="video",
        parser=parser_name,
    )
