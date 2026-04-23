"""
Ingestion pipeline: file path -> parser -> fastembed -> SQLite store.

This is the single choke-point every writer uses (watcher, `minion add`,
rebuild scripts). Keep it tiny and side-effect-free apart from DB writes
and model load.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from typing import Any, Callable, Dict

import numpy as np

from parsers import (
    ALL_KINDS,
    ParseResult,
    UnsupportedFile,
    disabled_kinds,
    is_disabled_kind,
    kind_for,
    parse_file,
)
from store import sha256_of_file, upsert_source
import telemetry
from embed_runtime import (
    embed_batch_size,
    touch_embedding_use,
    unload_shared_embedding_caches_if_idle,
)

log = logging.getLogger("minion.ingest")


def infer_data_dir_for_inbox(inbox: Path) -> Optional[Path]:
    """Resolve data_dir from MINION_DATA_DIR or default layout (inbox sibling ``data``)."""
    env = (os.environ.get("MINION_DATA_DIR") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() or p.parent.is_dir() else None
    inbox_r = Path(inbox).expanduser().resolve()
    cand = inbox_r.parent / "data"
    if cand.is_dir():
        return cand
    return None


def _chatgpt_export_manifest_paths(root: Path) -> List[Path]:
    """Return the set of JSON files that define this ChatGPT export.

    Covers both the native OpenAI layout (`conversations*.json` at root)
    and the third-party per-conversation layout (`json/YYYY-MM-DD_*.json`).
    Empty list means this directory is not a recognized export.
    """
    native = sorted(root.glob("conversations*.json"))
    if native:
        return native
    per_conv = sorted(root.glob("json/[12][0-9][0-9][0-9]-*.json"))
    return per_conv


def _looks_like_chatgpt_export(path: Path) -> bool:
    return path.is_dir() and bool(_chatgpt_export_manifest_paths(path))


def _chatgpt_export_digest(root: Path, manifests: List[Path]) -> str:
    """Deterministic digest over (relpath, size, mtime) for dedup.

    Cheap: no file reads, just stat. Invalidates cache when any manifest is
    added, removed, resized, or rewritten.
    """
    import hashlib

    h = hashlib.sha256()
    for p in manifests:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        st = p.stat()
        h.update(rel)
        h.update(b"\x00")
        h.update(str(st.st_size).encode("ascii"))
        h.update(b"\x00")
        h.update(f"{st.st_mtime:.6f}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


ProgressFn = Callable[[str, Dict[str, Any]], None]


def _noop(_stage: str, _info: Dict[str, Any]) -> None:
    pass


# Archive formats we unpack in-place. The contents are ingested individually
# by the watcher's next pass; we don't try to parse the archive itself.
_ARCHIVE_EXTS = (".zip",)

# Conservative cap so DALL-E-style 400-char basenames inside ChatGPT exports
# don't blow past macOS's 255-byte filename limit.
_MAX_BASENAME_BYTES = 200


def _unique_dir(parent: Path, name: str) -> Path:
    dest = parent / name
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = parent / f"{name} ({n})"
        if not cand.exists():
            return cand
        n += 1


def _unpack_zip(archive: Path, *, on_progress: ProgressFn = _noop) -> tuple[Path, int, int]:
    """Unpack `archive` into a sibling directory.

    Skips overlong basenames (media with 400+ char names from ChatGPT exports)
    and any zip entries that would escape the destination root (zip-slip).
    Returns (dest_dir, extracted_count, skipped_count).
    """
    import zipfile

    dest = _unique_dir(archive.parent, archive.stem)
    dest.mkdir(parents=True, exist_ok=False)
    on_progress("unpack_start", {"archive": str(archive), "dest": str(dest)})

    extracted = 0
    skipped = 0
    dest_root = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        infos = zf.infolist()
        total = len(infos)
        for i, info in enumerate(infos, 1):
            if info.is_dir():
                continue
            basename = Path(info.filename).name
            if not basename:
                skipped += 1
                continue
            if len(basename.encode("utf-8", "replace")) > _MAX_BASENAME_BYTES:
                skipped += 1
                continue
            target = (dest / info.filename).resolve()
            # Zip-slip guard: ensure the resolved path stays under dest.
            try:
                target.relative_to(dest_root)
            except ValueError:
                skipped += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    # Stream to avoid loading gigabyte files into RAM.
                    while True:
                        buf = src.read(1 << 20)  # 1 MiB chunks
                        if not buf:
                            break
                        out.write(buf)
                extracted += 1
            except OSError:
                skipped += 1
                continue
            if i % 50 == 0 or i == total:
                on_progress(
                    "unpack_progress",
                    {"done": i, "total": total, "extracted": extracted, "skipped": skipped},
                )
    on_progress("unpack_done", {"extracted": extracted, "skipped": skipped, "dest": str(dest)})
    return dest, extracted, skipped


def _maybe_unpack_archive(path: Path, *, on_progress: ProgressFn = _noop) -> Optional["IngestResult"]:
    """If `path` is an archive, unpack it and remove the original.

    Returns an IngestResult describing the unpack so the caller can record it
    in the feed. Returns None if `path` isn't an archive (normal ingest path).
    """
    suffix = path.suffix.lower()
    if suffix not in _ARCHIVE_EXTS:
        return None

    try:
        dest, extracted, skipped = _unpack_zip(path, on_progress=on_progress)
    except Exception as e:
        return IngestResult(
            str(path), None, "archive", "archive-unpack", 0, True,
            reason=f"unpack failed: {type(e).__name__}: {e}",
        )

    # Remove the archive so the watcher doesn't re-unpack it on every tick.
    try:
        path.unlink()
    except OSError:
        pass

    reason = f"unpacked {extracted} file(s) into {dest.name}/"
    if skipped:
        reason += f" (skipped {skipped} over-long or unsafe entries)"
    return IngestResult(
        str(path), None, "archive", "archive-unpack", 0, True,
        reason=reason,
    )


DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_NAME: Optional[str] = None


def _get_model(name: str):
    """Cache the fastembed model. Safe to call from multiple threads."""
    global _MODEL, _MODEL_NAME
    unload_shared_embedding_caches_if_idle()
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == name:
            touch_embedding_use()
            return _MODEL
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name=name)
        _MODEL_NAME = name
        touch_embedding_use()
        return _MODEL


@dataclass
class IngestResult:
    path: str
    source_id: Optional[str]
    kind: str
    parser: str
    chunk_count: int
    skipped: bool
    reason: Optional[str] = None


def _embed(
    model,
    texts: List[str],
    *,
    batch_size: Optional[int] = None,
    on_progress: ProgressFn = _noop,
) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    bs = embed_batch_size(64) if batch_size is None else max(1, min(int(batch_size), 256))
    out: List[np.ndarray] = []
    total = len(texts)
    i = 0
    on_progress("embed", {"done": 0, "total": total})
    while i < total:
        batch = texts[i : i + bs]
        vecs = list(model.embed(batch, batch_size=bs))
        out.append(np.asarray(vecs, dtype=np.float32))
        i += len(batch)
        on_progress("embed", {"done": i, "total": total})
    return np.concatenate(out, axis=0)


_MAX_WEBHOOK_CHUNKS = 2_000
_MAX_WEBHOOK_CHUNK_CHARS = 100_000
_MAX_WEBHOOK_TOTAL_CHARS = 4_000_000


def _stream_logical_path(data_dir: Path, source_key: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._@-]+", "_", source_key.strip())[:220].strip("._") or "stream"
    return str((Path(data_dir) / "__minion_stream__" / safe).resolve())


def _payload_digest(source_key: str, chunks_payload: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    h.update(source_key.encode("utf-8"))
    h.update(b"\0")
    h.update(json.dumps(chunks_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def ingest_webhook_payload(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    source_key: str,
    display_name: Optional[str],
    kind: str,
    parser: str,
    chunks: List[Dict[str, Any]],
    force: bool = False,
    on_progress: ProgressFn = _noop,
) -> IngestResult:
    """Parse-free ingest: embed JSON chunks (webhook / automation)."""
    sk = source_key.strip()
    if not sk:
        return IngestResult("", None, "?", "?", 0, True, reason="empty source_key")

    k = (kind or "external").strip()
    if k not in ALL_KINDS:
        return IngestResult("", None, k, parser, 0, True, reason=f"unknown kind {k!r}")
    if k in disabled_kinds():
        return IngestResult("", None, k, parser, 0, True, reason=f"disabled kind {k}")

    if len(chunks) > _MAX_WEBHOOK_CHUNKS:
        return IngestResult("", None, k, parser, 0, True, reason="too many chunks")

    total_chars = 0
    norm_chunks: List[tuple[str, Optional[str], Dict[str, Any]]] = []
    for i, c in enumerate(chunks):
        if not isinstance(c, dict):
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} not an object")
        text = c.get("text")
        if not isinstance(text, str) or not text.strip():
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} missing text")
        if len(text) > _MAX_WEBHOOK_CHUNK_CHARS:
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} too large")
        total_chars += len(text)
        if total_chars > _MAX_WEBHOOK_TOTAL_CHARS:
            return IngestResult("", None, k, parser, 0, True, reason="payload too large")
        role = c.get("role")
        if role is not None and not isinstance(role, str):
            role = str(role)
        meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
        norm_chunks.append((text, role, meta))

    logical = _stream_logical_path(data_dir, sk)
    digest = _payload_digest(sk, chunks)

    if not force:
        row = conn.execute("SELECT sha256 FROM sources WHERE path=?", (logical,)).fetchone()
        if row and row["sha256"] == digest:
            return IngestResult(logical, None, k, parser, 0, True, reason="unchanged")

    on_progress("parse_start", {"suffix": "(webhook)", "bytes": total_chars})
    on_progress("parsed", {"chunks": len(norm_chunks), "kind": k, "parser": parser})

    name = os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [t for t, _, _ in norm_chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    source_meta: Dict[str, Any] = {
        "stream_source_key": sk,
        "display_name": display_name or sk,
        "ingest_route": "webhook",
        "model_name": name,
    }

    source_id = upsert_source(
        conn,
        path=logical,
        kind=k,
        sha256=digest,
        mtime=time.time(),
        bytes_=total_chars,
        parser=parser,
        source_meta=source_meta,
        chunks=norm_chunks,
        embeddings=embeddings,
    )

    try:
        telemetry.log_event(
            "ingest",
            path=logical,
            file_kind=k,
            parser=parser,
            chunks=len(norm_chunks),
            skipped=False,
            reason=None,
            result="ingested",
            source_id=source_id,
        )
    except Exception:
        pass

    return IngestResult(
        path=logical,
        source_id=source_id,
        kind=k,
        parser=parser,
        chunk_count=len(norm_chunks),
        skipped=False,
    )


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str] = None,
    force: bool = False,
    on_progress: ProgressFn = _noop,
    data_dir: Optional[Path] = None,
    inbox: Optional[Path] = None,
) -> IngestResult:
    """Public ingest entrypoint. Wraps the pipeline with telemetry.

    Telemetry is recorded for every call (success, skip, error) so we have a
    single source of truth for how each file was handled, regardless of
    whether it was triggered by the watcher, the CLI, or `bin/minion add`.
    """
    result = _ingest_file_inner(
        conn,
        path,
        model_name=model_name,
        force=force,
        on_progress=on_progress,
        data_dir=data_dir,
        inbox=inbox,
    )
    try:
        telemetry.log_event(
            "ingest",
            path=result.path,
            file_kind=result.kind,
            parser=result.parser,
            chunks=result.chunk_count,
            skipped=result.skipped,
            reason=result.reason,
            result=(
                "ingested" if not result.skipped
                else (result.reason.split(":", 1)[0] if result.reason else "skipped")
            ),
            source_id=result.source_id,
        )
    except Exception:
        pass
    return result


def _ingest_file_inner(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str] = None,
    force: bool = False,
    on_progress: ProgressFn = _noop,
    data_dir: Optional[Path] = None,
    inbox: Optional[Path] = None,
) -> IngestResult:
    """Parse + embed + upsert. Skips unchanged files (same sha256) unless force=True."""
    path = Path(path).expanduser().resolve()
    spath = str(path)
    if not path.exists():
        return IngestResult(spath, None, "?", "?", 0, True, reason="missing")

    # Directories are not ingestable in general, but ChatGPT exports ship as
    # a directory of JSON manifests. Detect that shape and dispatch directly
    # to the chatgpt_export parser; fall through for everything else.
    if path.is_dir():
        if _looks_like_chatgpt_export(path):
            return _ingest_chatgpt_export_dir(
                conn,
                path,
                model_name=model_name,
                force=force,
                on_progress=on_progress,
                data_dir=data_dir,
                inbox=inbox,
            )
        return IngestResult(spath, None, "?", "?", 0, True, reason="directory (not a recognized export)")

    # Archives are containers, not parseable files. Unpack in place and let
    # the watcher's next pass ingest each contained file through its proper
    # parser. Keeps the dispatch generic -- no per-vendor assumptions.
    unpacked = _maybe_unpack_archive(path, on_progress=on_progress)
    if unpacked is not None:
        return unpacked

    # Respect user's file-type preferences (settings.json). Skip cleanly so
    # the UI logs it as a deliberate opt-out, not a parser failure.
    if is_disabled_kind(path):
        k = kind_for(path) or path.suffix.lstrip(".") or "?"
        return IngestResult(
            spath, None, k, "disabled", 0, True,
            reason=f"disabled: '{k}' parsing turned off in settings",
        )

    digest = sha256_of_file(path)
    if not force:
        row = conn.execute(
            "SELECT sha256 FROM sources WHERE path=?", (spath,)
        ).fetchone()
        if row and row["sha256"] == digest:
            return IngestResult(spath, None, "?", "?", 0, True, reason="unchanged")

    on_progress("parse_start", {"suffix": path.suffix.lower(), "bytes": path.stat().st_size if path.exists() else 0})
    try:
        # Parsers may optionally accept an on_progress kwarg; the dispatcher
        # forwards only the kwargs the target parser actually declares.
        result: ParseResult = parse_file(path, on_progress=on_progress)
    except UnsupportedFile as e:
        return IngestResult(spath, None, "?", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        # Parsers can raise domain-specific errors (EmptyParse, etc.) with a
        # human-readable reason; preserve that instead of burying it.
        name = type(e).__name__
        msg = str(e) or name
        # Strip our exception class name from common empty-text cases so the
        # UI gets a clean "image-only PDF: ..." rather than "EmptyParse: ...".
        if name in ("EmptyParse", "ValueError"):
            return IngestResult(spath, None, path.suffix.lstrip(".") or "?", "?", 0, True, reason=msg)
        return IngestResult(spath, None, "?", "?", 0, True, reason=f"parse-error: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind, result.parser, 0, True,
            reason="file parsed but produced no text (empty or unsupported content)",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    stat = path.stat()
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", path.suffix.lower())
    source_meta.setdefault("model_name", name)

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind,
        sha256=digest,
        mtime=stat.st_mtime,
        bytes_=stat.st_size,
        parser=result.parser,
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind,
        parser=result.parser,
        chunk_count=len(result.chunks),
        skipped=False,
    )


def _ingest_chatgpt_export_dir(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str],
    force: bool,
    on_progress: ProgressFn,
    data_dir: Optional[Path] = None,
    inbox: Optional[Path] = None,
) -> IngestResult:
    """Ingest a ChatGPT export directory as a single logical source.

    Shapes accepted:
    - Native OpenAI export root containing `conversations*.json`.
    - Third-party per-conversation exporter with `json/YYYY-MM-DD_*.json`.

    The whole export is represented by one `sources` row keyed by the
    directory path. sha256 is computed over the manifest (relpath, size,
    mtime) so re-running is a no-op unless a manifest changed.
    """
    spath = str(path)
    manifests = _chatgpt_export_manifest_paths(path)
    digest = _chatgpt_export_digest(path, manifests)

    if not force:
        row = conn.execute(
            "SELECT sha256 FROM sources WHERE path=?", (spath,)
        ).fetchone()
        if row and row["sha256"] == digest:
            return IngestResult(spath, None, "chatgpt-export", "chatgpt-export", 0, True, reason="unchanged")

    total_bytes = sum(p.stat().st_size for p in manifests)
    latest_mtime = max((p.stat().st_mtime for p in manifests), default=path.stat().st_mtime)

    on_progress("parse_start", {"suffix": "(dir)", "bytes": total_bytes, "manifests": len(manifests)})
    try:
        # Force the chatgpt_export parser since extension dispatch doesn't
        # apply to directories.
        result: ParseResult = parse_file(path, parser="parsers.chatgpt_export", on_progress=on_progress)
    except UnsupportedFile as e:
        return IngestResult(spath, None, "chatgpt-export", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        return IngestResult(spath, None, "chatgpt-export", "?", 0, True, reason=f"parse-error: {name}: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind or "chatgpt-export", result.parser or "chatgpt-export", 0, True,
            reason="export parsed but produced no user-message chunks",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", "(dir)")
    source_meta.setdefault("model_name", name)
    source_meta.setdefault("manifest_count", len(manifests))

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind or "chatgpt-export",
        sha256=digest,
        mtime=latest_mtime,
        bytes_=total_bytes,
        parser=result.parser or "chatgpt-export",
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
    )

    if data_dir is not None:
        try:
            from ambient_pipeline import maybe_seal_chatgpt_export_after_ingest

            maybe_seal_chatgpt_export_after_ingest(
                conn,
                Path(data_dir),
                path,
                source_id=source_id,
                ingest_digest=digest,
                inbox=inbox,
            )
        except Exception:
            log.exception("ambient seal hook failed for %s", spath)

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind or "chatgpt-export",
        parser=result.parser or "chatgpt-export",
        chunk_count=len(result.chunks),
        skipped=False,
    )
