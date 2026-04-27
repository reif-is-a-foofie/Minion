#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

# Ensure no warnings are emitted to stdout (breaks JSON-RPC framing over stdio).
# Must run before importing libraries that may trigger warnings at import time.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*LibreSSL.*",
)

from fastembed import TextEmbedding

from store import (
    DB_FILENAME,
    browse_chunks_chronological as store_browse_chronological,
    connect,
    count_chunks,
    count_sources,
    fts_available,
    get_chunk as store_get_chunk,
    get_conversation_chunks as store_get_conversation_chunks,
    get_meta,
    get_source,
    identity_claim_get,
    keyword_search as store_keyword_search,
    list_conversations as store_list_conversations,
    list_sources as store_list_sources,
    search as store_search,
)
import telemetry
import identity
from embed_runtime import should_unload_embedding, touch_embedding_use
from settings import load_settings, merge_identity_defaults
from process_title import apply_mcp_title, data_dir_sha8
from version import __version__
from retrieval_bias import apply_identity_rerank, rrf_fuse
from build_voice import (
    AUTO_DRAFT_SENTINEL,
    USER_EDITS_SENTINEL,
    VOICE_SECTIONS,
    append_to_voice_file,
    build_skeleton as voice_build_skeleton,
    is_voice_built,
    write_auto_draft as voice_write_auto_draft,
)


APP_NAME = "Minion"
TOP_K_CAP = 12
DEFAULT_TOP_K = 8
DEFAULT_MAX_CHARS = 900
DEFAULT_MAX_CHARS_FULL = 2000
PROTOCOL_VERSION = "2025-11-25"
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_INSTRUCTIONS_FALLBACK = (
    "Minion is the user's digital identity and long-term memory: their "
    "chat history, notes, documents, scans, photos, voice, video, code, "
    "decisions, beliefs, relationships, projects — in their own words, "
    "continuous across every session. Consult it the way you would consult "
    "the person themselves. Before answering a question about the user, ask "
    "whether the answer lives in their experience — history, relationships, "
    "decisions, preferences, work, writing, faith, health, anything tied to "
    "their identity. If so, call `ask_minion` first, then speak from what "
    "you find. Start with mode='relevance'; if hits feel weak or miss a "
    "specific name, retry in mode='keyword' (scans OCR with noise and "
    "embeddings underrank rare proper nouns). Use mode='oldest' or "
    "mode='newest' for first/last/time-scoped questions."
)

# Injected when voice.md is not built yet (keeps initialize smaller than the old inlined essay).
_VOICE_BOOTSTRAP_INSTRUCTIONS = (
    "\n\n---\n\n"
    "# Voice bootstrap required (one-time, silent self-setup)\n\n"
    "The user's voice profile has not been built yet. On your **first** substantive "
    "turn this session, run this **silently** before answering their question. "
    "Persist to `voice.md` via `commit_voice`; afterward it is auto-injected every session.\n\n"
    "## 1. Evidence (semantic first)\n\n"
    "`ask_minion` with `mode='relevance'`, `role='user'`, `top_k=12`. Query in the "
    "**shape of a user utterance** (imperatives they would type), not meta-requests. "
    "Run these **8** probes (one call each):\n"
    "```\n"
    "ask_minion(query=\"don't use emojis, em dashes, or ellipses in your responses\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"respond in short paragraphs, no bullet points or headers\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"keep it short, minimum words, compress this down\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"shorter sentences, punchier, less wordy\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"this sounds like an AI wrote it, rewrite in a human voice\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"I love how this writer writes, capture that voice\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"write like Hemingway, Didion, Paul Graham, or a specific author I named\", mode='relevance', role='user', top_k=12)\n"
    "ask_minion(query=\"more casual, less formal, how I would say it to a friend\", mode='relevance', role='user', top_k=12)\n"
    "```\n\n"
    "If hits are thin: `browse_conversations(limit=15, order='newest')`. "
    "`conversation_chunks` only when one thread needs full context. "
    "**Keyword mode:** only after a specific proper noun appears in semantic hits; "
    "run `mode='keyword'` on that token — never as a generic semantic fallback.\n\n"
    "## 2. Synthesize\n\n"
    "Markdown voice profile with **exact** H3 headings: Typography, Formatting, "
    "Length and density, Tone and register, Style references, Hard nos, Voice sample. "
    "Use `_(insufficient signal)_` when a section lacks evidence. "
    "Voice sample = one 60–150 word paragraph in their voice (synthesized, not quoted).\n\n"
    "## 3. Commit\n\n"
    "`commit_voice(voice_markdown=...)` — 400–1800 chars; tool enforces headings/size.\n\n"
    "## Rules\n\n"
    "- **Voice vs content:** layout/tone/register rules are voice; one-off creative "
    "tasks are not. When in doubt, omit.\n"
    "- **Evidence only:** no invented rules from model priors.\n"
    "- **Domain:** noisy verticals (code, scripture, etc.) — voice cues only.\n"
    "- **Silent:** do not narrate bootstrap; no draft approval step.\n"
    "- After `commit_voice` succeeds, answer the user's question using that voice.\n\n"
    "## Later sessions\n\n"
    "Durable in-session preferences (`save this`, `never X again`, author as style "
    "target): one-sentence confirmation, then `append_to_voice`. Never persist without "
    "explicit user confirmation."
)

log = logging.getLogger("minion.mcp")


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _instructions_max_chars() -> int:
    raw = _env_first("MINION_INSTRUCTIONS_MAX_CHARS", "CHATGPT_MCP_INSTRUCTIONS_MAX_CHARS", default="20000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 20000


def _brief_max_chars() -> int:
    raw = _env_first("MINION_BRIEF_MAX_CHARS", default="4000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 4000


def _voice_max_chars() -> int:
    raw = _env_first("MINION_VOICE_MAX_CHARS", default="5000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 5000


def _data_dir() -> Path:
    env = _env_first("MINION_DATA_DIR", "CHATGPT_MCP_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    repo_guess = here.parents[1]
    candidate = repo_guess / "data" / "derived"
    if candidate.exists():
        return candidate
    exe = Path(sys.argv[0]).resolve()
    return exe.parent.parent / "data" / "derived"


def _inbox_dir() -> Path:
    env = _env_first("MINION_INBOX")
    if env:
        return Path(env).expanduser().resolve()
    return _data_dir().parent / "inbox"


def _load_retrieval_instructions() -> str:
    explicit = _env_first("MINION_RETRIEVAL_POLICY", "CHATGPT_MCP_RETRIEVAL_POLICY")
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())
    data_dir = _data_dir()
    candidates.append(data_dir / "retrieval_policy.md")
    candidates.append(data_dir.parent / "retrieval_policy.md")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            cap = _instructions_max_chars()
            if len(text) > cap:
                text = text[: cap - 30].rstrip() + "\n\n… [truncated for MCP instructions size cap]"
            return (
                "Minion memory MCP — follow this retrieval policy when using ask_minion / get_chunk:\n\n"
                + text
            )
    return _INSTRUCTIONS_FALLBACK


def _voice_path() -> Path:
    """Return the resolved path to voice.md (may not exist yet)."""
    explicit = _env_first("MINION_VOICE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _data_dir() / "voice.md"


def _load_voice() -> Optional[str]:
    """Return the durable user-voice directives, if present.

    Search order:
        1. $MINION_VOICE (explicit path)
        2. <data_dir>/voice.md
        3. <data_dir>/../voice.md
    """
    explicit = _env_first("MINION_VOICE")
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())
    data_dir = _data_dir()
    for base in (data_dir, data_dir.parent):
        candidates.append(base / "voice.md")

    cap = _voice_max_chars()
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > cap:
            text = text[: cap - 30].rstrip() + "\n\n_…truncated…_\n"
        return text
    return None


def _voice_is_built() -> bool:
    """True if voice.md exists and has non-stub content in either block."""
    path = _voice_path()
    try:
        if not path.is_file():
            return False
        return is_voice_built(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _merge_identity_into_brief(base: str, cap: int) -> str:
    db_path = _data_dir() / DB_FILENAME
    extra = ""
    if db_path.is_file():
        try:
            c = connect(db_path)
            try:
                extra = identity.build_identity_summary(
                    c, max_claims=25, max_clusters=5
                ).strip()
            finally:
                c.close()
        except Exception:
            extra = ""
    merged = base
    if extra:
        merged = base.rstrip() + "\n\n---\n\n" + extra
    merged = merged.strip()
    if len(merged) > cap:
        merged = merged[: cap - 30].rstrip() + "\n\n_…truncated…_\n"
    return merged


def _load_profile_brief() -> Optional[str]:
    explicit = _env_first("MINION_PROFILE")
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())
    data_dir = _data_dir()
    for base in (data_dir, data_dir.parent):
        for name in ("identity_profile.md", "core_profile.md", "brief.md"):
            candidates.append(base / name)

    cap = _brief_max_chars()
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        return _merge_identity_into_brief(text, cap)
    return None


# ---------------------------------------------------------------------------
# Index load (with legacy auto-migration)
# ---------------------------------------------------------------------------


_INDEX_LOCK = threading.RLock()  # reentrant: _get_model acquires then calls _get_conn
_CONN: Optional[sqlite3.Connection] = None
_MODEL: Optional[TextEmbedding] = None
_MODEL_NAME: Optional[str] = None

_SESSION_STATE: Dict[str, Any] = {"brief_sent": False}
_MCP_BOOT_LOGGED = False


def _maybe_auto_migrate(data_dir: Path) -> None:
    """If memory.db is missing but legacy chunks.jsonl+embeddings.npy exist, migrate in-place."""
    db_path = data_dir / DB_FILENAME
    if db_path.exists():
        return
    legacy_chunks = data_dir / "chunks.jsonl"
    legacy_emb = data_dir / "embeddings.npy"
    if legacy_chunks.exists() and legacy_emb.exists():
        try:
            from migrate_to_sqlite import migrate

            migrate(data_dir)
            log.warning("auto-migrated legacy index to %s", db_path)
        except Exception:  # pragma: no cover
            log.exception("auto-migration failed")


def _get_conn() -> sqlite3.Connection:
    global _CONN
    with _INDEX_LOCK:
        if _CONN is not None:
            return _CONN
        data_dir = _data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        _maybe_auto_migrate(data_dir)
        telemetry.configure(data_dir)
        db_path = data_dir / DB_FILENAME
        _CONN = connect(db_path)
        _maybe_start_watcher(db_path)
        return _CONN


def _new_conn() -> sqlite3.Connection:
    """Fresh connection for the watcher thread (sqlite3 is not thread-safe by default)."""
    db_path = _data_dir() / DB_FILENAME
    return connect(db_path)


def _maybe_start_watcher(db_path: Path) -> None:
    """Auto-start the inbox watcher unless disabled by env."""
    if _env_first("MINION_DISABLE_WATCHER") in ("1", "true", "TRUE"):
        return
    try:
        from watcher import reconcile_once, start_background

        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        conn = _CONN
        if conn is not None:
            try:
                reconcile_once(conn, inbox, data_dir=_data_dir())
            except Exception:
                log.exception("startup reconcile failed")
        start_background(_new_conn, inbox, data_dir=_data_dir())
    except Exception:
        log.exception("failed to start watcher")


def _get_model() -> TextEmbedding:
    global _MODEL, _MODEL_NAME
    with _INDEX_LOCK:
        if should_unload_embedding():
            _MODEL = None
            _MODEL_NAME = None
        conn = _get_conn()
        name = (
            get_meta(conn, "model_name")
            or os.environ.get("MINION_EMBED_MODEL")
            or DEFAULT_EMBED_MODEL
        )
        if _MODEL is not None and _MODEL_NAME == name:
            touch_embedding_use()
            return _MODEL
        _MODEL = TextEmbedding(model_name=name)
        _MODEL_NAME = name
        touch_embedding_use()
        return _MODEL


def _embed_query(query: str) -> np.ndarray:
    model = _get_model()
    vec = np.asarray(next(iter(model.embed([query]))), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec


def _cap_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


_VALID_MODES = ("relevance", "oldest", "newest", "keyword")

def _content_fingerprint(text: str) -> str:
    """Cheap hash of the first ~400 meaningful chars.

    Two chunks with the same fingerprint are near-duplicates (same OCR pass,
    same conversation re-ingested from a second ChatGPT export, etc.). We
    drop later occurrences from results so retrieval slots aren't burned on
    redundant text.
    """
    import hashlib
    # Normalize whitespace + case so "  foo\n BAR " == "foo bar".
    norm = " ".join((text or "").split()).lower()[:400]
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def _file_url(path: Optional[str]) -> Optional[str]:
    """Turn a local absolute path into a `file://` URI suitable for a
    markdown link. Claude Desktop on macOS renders these as clickable;
    cmd-click opens the file in its default app (Finder for folders,
    Preview for PDFs, etc.).
    """
    if not path:
        return None
    # Handle ChatGPT export directories and regular files the same way.
    from urllib.parse import quote
    # Preserve `/` so the URL stays readable; encode everything else.
    return "file://" + quote(path, safe="/")


def _hit_to_result(hit: Any, max_chars: int) -> Dict[str, Any]:
    meta = hit.meta or {}
    return {
        "score": round(hit.score, 4),
        "chunk_id": hit.chunk_id,
        "role": hit.role,
        "source_id": hit.source_id,
        "path": hit.path,
        "file_url": _file_url(hit.path),
        "kind": hit.kind,
        "mtime": hit.mtime,
        "conversation_id": meta.get("conversation_id"),
        "conversation_title": meta.get("conversation_title"),
        "create_time": meta.get("create_time"),
        "text": _cap_text(hit.text, max_chars),
    }


def _tool_ask_minion(arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
    query = str(arguments.get("query") or "").strip()
    mode = str(arguments.get("mode") or "relevance").lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")

    top_k = int(arguments.get("top_k") or DEFAULT_TOP_K)
    role = arguments.get("role")
    role = str(role) if role is not None else None
    max_chars = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS)
    dedupe_by_source = bool(arguments.get("dedupe_by_source", True))
    kind = arguments.get("kind")
    kind = str(kind) if kind else None
    path_glob = arguments.get("path_glob")
    path_glob = str(path_glob) if path_glob else None
    since = arguments.get("since")
    since_f: Optional[float] = float(since) if since is not None else None
    before = arguments.get("before")
    before_f: Optional[float] = float(before) if before is not None else None
    after = arguments.get("after")
    after_f: Optional[float] = float(after) if after is not None else None

    if top_k < 1:
        top_k = 1
    if top_k > TOP_K_CAP:
        top_k = TOP_K_CAP

    if mode == "relevance" and not query:
        raise ValueError("query is required when mode='relevance'")
    if mode == "keyword" and not query:
        raise ValueError("query is required when mode='keyword'")

    conn = _get_conn()

    if mode in ("oldest", "newest"):
        hits = store_browse_chronological(
            conn,
            order=mode,
            role=role,
            kind=kind,
            path_glob=path_glob,
            before=before_f,
            after=after_f,
            query_substring=query or None,
            limit=top_k * 3 if dedupe_by_source else top_k,
        )
    elif mode == "keyword":
        hits = store_keyword_search(
            conn,
            query,
            top_k=top_k * 3 if dedupe_by_source else top_k,
            role=role,
            kind=kind,
            path_glob=path_glob,
            before=before_f,
            after=after_f,
        )
    else:  # relevance
        qvec = _embed_query(query)
        # Fetch 3x the user's top_k as candidates: gives dedup + fusion room
        # to promote under-ranked scanned docs without starving the final list.
        internal_k = max(top_k * 3, top_k + 8)
        relevance_hits = store_search(
            conn,
            qvec,
            top_k=internal_k,
            kind=kind,
            path_glob=path_glob,
            since=since_f,
            role=role,
        )
        # Hybrid rerank: fuse semantic cosine with FTS5 BM25 via Reciprocal
        # Rank Fusion. Fixes the classic failure mode where a short chat turn
        # echoing the query ("ok and how does this fit the patriarchal
        # blessing") outranks the OCR'd source document. FTS5 rewards exact
        # phrase hits that embeddings underweight. Skip fusion when FTS is
        # unavailable (SQLite build w/o FTS5) or the query is empty.
        hits = relevance_hits
        rerank_used = "none"
        if query and fts_available(conn):
            try:
                keyword_hits = store_keyword_search(
                    conn,
                    query,
                    top_k=internal_k,
                    role=role,
                    kind=kind,
                    path_glob=path_glob,
                    before=before_f,
                    after=after_f,
                )
                if keyword_hits:
                    hits = rrf_fuse(relevance_hits, keyword_hits)
                    rerank_used = "rrf"
            except Exception:
                log.exception("RRF fusion failed; falling back to relevance-only")
        # Bookkeeping for telemetry below.
        _SESSION_STATE["_last_rerank"] = rerank_used
        _SESSION_STATE["_last_candidates"] = len(relevance_hits)

    if mode in ("relevance", "keyword") and hits:
        hits, bias_meta = apply_identity_rerank(conn, hits)
        _SESSION_STATE["_bias_meta"] = bias_meta
    else:
        _SESSION_STATE["_bias_meta"] = {}

    results: List[Dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_content: set[str] = set()
    content_dropped = 0
    for h in hits:
        if dedupe_by_source and h.source_id in seen_sources:
            continue
        # Content-dedup catches near-identical chunks across different
        # source_ids — e.g. two copies of the same ChatGPT export. This is
        # separate from source-dedup because same-source duplicates are
        # already handled above; this catches cross-source duplication.
        fp = _content_fingerprint(h.text)
        if fp in seen_content:
            content_dropped += 1
            continue
        seen_sources.add(h.source_id)
        seen_content.add(fp)
        results.append(_hit_to_result(h, max_chars))
        if len(results) >= top_k:
            break

    # Telemetry: one line per search, so future improvements (retrieval bugs,
    # chronic weak hits, queries that always fall back to keyword) can be
    # spotted by tailing the JSONL. Never blocks or raises.
    try:
        top = results[0] if results else {}
        bias_meta = _SESSION_STATE.pop("_bias_meta", None) or {}
        telemetry.log_event(
            "search",
            mode=mode,
            query=query or None,
            top_k=top_k,
            returned=len(results),
            top_score=top.get("score"),
            top_path=top.get("path"),
            top_kind=top.get("kind"),
            rerank=_SESSION_STATE.pop("_last_rerank", "none"),
            candidates=_SESSION_STATE.pop("_last_candidates", None),
            content_dropped=content_dropped,
            hit_kinds=[r.get("kind") for r in results],
            kind_filter=kind,
            path_glob=path_glob,
            role=role,
            bias_clusters=bias_meta.get("bias_clusters"),
            bias_claims=bias_meta.get("bias_claims"),
            bias_run_at=bias_meta.get("bias_run_at"),
            adjustments_applied=bias_meta.get("adjustments_applied"),
        )
    except Exception:
        pass

    return results


def _tool_get_chunk(arguments: Dict[str, Any]) -> Dict[str, Any]:
    chunk_id = str(arguments.get("chunk_id") or "")
    max_chars = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS_FULL)

    conn = _get_conn()
    chunk = store_get_chunk(conn, chunk_id)
    if chunk is None:
        raise ValueError(f"chunk_id not found: {chunk_id}")

    meta = chunk.get("meta") or {}
    return {
        "chunk_id": chunk["chunk_id"],
        "role": chunk.get("role"),
        "source_id": chunk["source_id"],
        "path": chunk.get("path"),
        "kind": chunk.get("kind"),
        "mtime": chunk.get("mtime"),
        "conversation_id": meta.get("conversation_id"),
        "conversation_title": meta.get("conversation_title"),
        "create_time": meta.get("create_time"),
        "page": meta.get("page"),
        "start": meta.get("start"),
        "end": meta.get("end"),
        "language": meta.get("language"),
        "start_line": meta.get("start_line"),
        "text": _cap_text(chunk["text"] or "", max_chars),
    }


def _tool_list_sources(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """List indexed sources, optionally filtered; or return full detail for one source.

    If `source_id` is provided, returns `{source: {...full metadata + chunk_count...}}`
    with parser, sha256, meta, bytes, updated_at — the single-source detail view.
    Otherwise returns `{sources: [...], count: n}` with large fields stripped
    for token discipline. Filters (`kind`, `path_glob`, `since`, `limit`) only
    apply to the list view.
    """
    conn = _get_conn()

    source_id = arguments.get("source_id")
    if source_id:
        src = get_source(conn, str(source_id))
        if src is None:
            raise ValueError(f"source_id not found: {source_id}")
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (str(source_id),)
        ).fetchone()["n"]
        return {
            "source": {
                "source_id": src.source_id,
                "path": src.path,
                "kind": src.kind,
                "sha256": src.sha256,
                "mtime": src.mtime,
                "bytes": src.bytes,
                "parser": src.parser,
                "updated_at": src.updated_at,
                "chunk_count": int(chunk_count),
                "meta": src.meta,
            }
        }

    kind = arguments.get("kind")
    kind = str(kind) if kind else None
    path_glob = arguments.get("path_glob")
    path_glob = str(path_glob) if path_glob else None
    since = arguments.get("since")
    since_f: Optional[float] = float(since) if since is not None else None
    limit = int(arguments.get("limit") or 100)
    limit = max(1, min(1000, limit))

    rows = store_list_sources(
        conn, kind=kind, path_glob=path_glob, since=since_f, limit=limit
    )
    # Strip large meta from list view for token discipline.
    for r in rows:
        r.pop("meta", None)
        r.pop("sha256", None)
    return {"sources": rows, "count": len(rows)}


def _tool_browse_conversations(arguments: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    title_like = arguments.get("title_like")
    title_like = str(title_like) if title_like else None
    since = arguments.get("since")
    since_f: Optional[float] = float(since) if since is not None else None
    until = arguments.get("until")
    until_f: Optional[float] = float(until) if until is not None else None
    order = str(arguments.get("order") or "newest").lower()
    limit = int(arguments.get("limit") or 50)
    limit = max(1, min(500, limit))

    rows = store_list_conversations(
        conn,
        title_like=title_like,
        since=since_f,
        until=until_f,
        order=order,
        limit=limit,
    )
    return {"conversations": rows, "count": len(rows)}


def _tool_conversation_chunks(arguments: Dict[str, Any]) -> Dict[str, Any]:
    conversation_id = str(arguments.get("conversation_id") or "").strip()
    if not conversation_id:
        raise ValueError("conversation_id is required")
    max_chars = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS)
    limit = int(arguments.get("limit") or 100)
    limit = max(1, min(500, limit))

    conn = _get_conn()
    rows = store_get_conversation_chunks(conn, conversation_id, limit=limit)
    out = []
    for r in rows:
        r2 = dict(r)
        r2["text"] = _cap_text(r.get("text") or "", max_chars)
        out.append(r2)
    return {"conversation_id": conversation_id, "chunks": out, "count": len(out)}


_COMMIT_MIN_CHARS = 200
_COMMIT_MAX_CHARS = 6000


def _tool_commit_voice(args: Dict[str, Any]) -> Dict[str, Any]:
    """Persist Claude's synthesized voice block to voice.md.

    Overwrites the AUTO_DRAFT section; preserves USER_EDITS verbatim.
    """
    markdown = (args.get("voice_markdown") or "").strip()
    if not markdown:
        return {"status": "error", "error": "voice_markdown is required and must be non-empty"}
    if len(markdown) < _COMMIT_MIN_CHARS:
        return {
            "status": "error",
            "error": f"voice_markdown too short ({len(markdown)} chars; min {_COMMIT_MIN_CHARS}). "
                     "Synthesize more substantive directives.",
        }
    if len(markdown) > _COMMIT_MAX_CHARS:
        return {
            "status": "error",
            "error": f"voice_markdown too long ({len(markdown)} chars; max {_COMMIT_MAX_CHARS}). "
                     "Tighten the synthesis.",
        }
    if not any(h in markdown for h in ("###", "##")):
        return {
            "status": "error",
            "error": "voice_markdown must contain at least one markdown heading (## or ###).",
        }

    path = _voice_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    chunks_path = _data_dir() / "chunks.jsonl"
    n_chunks = 0
    if chunks_path.is_file():
        with chunks_path.open("r", encoding="utf-8") as f:
            for _ in f:
                n_chunks += 1

    voice_write_auto_draft(path, markdown, n_chunks=n_chunks)
    new_text = path.read_text(encoding="utf-8")

    return {
        "status": "ok",
        "voice_path": str(path),
        "bytes_written": path.stat().st_size,
        "built": is_voice_built(new_text),
        "note": (
            "Voice profile committed. It is injected into `initialize.instructions` "
            "on every future session. User edits go below the USER_EDITS sentinel "
            "and survive future re-bootstraps."
        ),
    }


_APPEND_MIN_CHARS = 3
_APPEND_MAX_CHARS = 800


def _tool_append_to_voice(args: Dict[str, Any]) -> Dict[str, Any]:
    """Append a single durable directive to one section of the voice profile.

    Intended for mid-session capture: when the user explicitly signals a new
    voice preference ("save this", "write like Didion", "never do Y again"),
    Claude asks the user to confirm, then calls this tool to persist the
    directive to the named H3 section of voice.md's AUTO_DRAFT block.

    Idempotent: content that already exists (normalized) in the section is a
    no-op. Preserves every other section and the USER_EDITS block.
    """
    section = (args.get("section") or "").strip()
    content = (args.get("content") or "").strip()

    if not section:
        return {
            "status": "error",
            "error": f"section is required. Expected one of: {list(VOICE_SECTIONS)}",
        }
    if section not in VOICE_SECTIONS:
        return {
            "status": "error",
            "error": f"unknown section {section!r}. Expected one of: {list(VOICE_SECTIONS)}",
        }
    if len(content) < _APPEND_MIN_CHARS:
        return {
            "status": "error",
            "error": f"content too short ({len(content)} chars; min {_APPEND_MIN_CHARS}).",
        }
    if len(content) > _APPEND_MAX_CHARS:
        return {
            "status": "error",
            "error": f"content too long ({len(content)} chars; max {_APPEND_MAX_CHARS}). "
                     "Append short, durable directives, not long prose.",
        }

    path = _voice_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    chunks_path = _data_dir() / "chunks.jsonl"
    n_chunks = 0
    if chunks_path.is_file():
        with chunks_path.open("r", encoding="utf-8") as f:
            for _ in f:
                n_chunks += 1

    try:
        appended, new_bytes = append_to_voice_file(
            path, section, content, n_chunks=n_chunks
        )
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    return {
        "status": "ok",
        "appended": appended,
        "section": section,
        "voice_path": str(path),
        "bytes": new_bytes,
        "note": (
            "Directive appended to voice.md AUTO_DRAFT block. It will be "
            "injected into `initialize.instructions` on every future session."
            if appended
            else "Content already present in this section (by normalized match); no change written."
        ),
    }


def _tool_index_info(_: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    return {
        "data_dir": str(_data_dir()),
        "inbox_dir": str(_inbox_dir()),
        "db_path": str(_data_dir() / DB_FILENAME),
        "chunk_count": count_chunks(conn),
        "source_count": count_sources(conn),
        "model_name": get_meta(conn, "model_name") or DEFAULT_EMBED_MODEL,
    }


def _tool_propose_identity_update(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    layer_arg = args.get("layer")
    layer_i: Optional[int] = None
    if layer_arg is not None:
        try:
            layer_i = int(layer_arg)
        except (TypeError, ValueError):
            return {"status": "error", "error": "layer must be an integer 1..7"}
    payload, err = identity.propose_identity_update(
        conn,
        kind=str(args.get("kind") or ""),
        text=str(args.get("text") or ""),
        source_agent=args.get("source_agent"),
        confidence=args.get("confidence"),
        evidence_chunk_ids=args.get("evidence_chunk_ids"),
        evidence_rationales=args.get("evidence_rationales"),
        meta=args.get("meta") if isinstance(args.get("meta"), dict) else None,
        layer=layer_i,
        field=args.get("field") if args.get("field") is not None else None,
    )
    if err:
        return {"status": "error", "error": err}
    assert payload is not None
    return {"status": "ok", **payload}


def _tool_list_identity_claims(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    limit = args.get("limit")
    try:
        lim = int(limit) if limit is not None else 100
    except (TypeError, ValueError):
        lim = 100
    layer_arg = args.get("layer")
    layer_i: Optional[int] = None
    if layer_arg is not None:
        try:
            layer_i = int(layer_arg)
        except (TypeError, ValueError):
            return {"status": "error", "error": "layer must be an integer 1..7"}
    rows, err = identity.list_claims(
        conn,
        status=args.get("status"),
        kind=args.get("kind"),
        layer=layer_i,
        limit=lim,
    )
    if err:
        return {"status": "error", "error": err}
    return {"status": "ok", "claims": rows, "count": len(rows)}


def _tool_set_identity_claim_status(args: Dict[str, Any]) -> Dict[str, Any]:
    claim_id = str(args.get("claim_id") or "").strip()
    if not claim_id:
        return {"status": "error", "error": "claim_id required"}
    status = str(args.get("status") or "").strip().lower()
    if not status:
        return {"status": "error", "error": "status required"}
    sb = args.get("superseded_by")
    superseded = str(sb).strip() if sb not in (None, "") else None
    conn = _get_conn()
    ok, err = identity.set_claim_status(
        conn, claim_id, status=status, superseded_by=superseded
    )
    if not ok:
        return {"status": "error", "error": err or "unknown error"}
    row = identity_claim_get(conn, claim_id)
    return {"status": "ok", "claim": row}


def _session_layer_grants_from_args(raw: Any) -> Set[int]:
    out: Set[int] = set()
    if not isinstance(raw, list):
        return out
    for x in raw:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 7:
            out.add(n)
    return out


def _persisted_session_layer_grants() -> Set[int]:
    try:
        data = load_settings(_data_dir())
        ident = merge_identity_defaults(data.get("identity"))
        out: Set[int] = set()
        for x in ident.get("session_layer_grants") or []:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 7:
                out.add(n)
        return out
    except Exception:
        return set()


def _merge_tool_and_persisted_grants(args_raw: Any, persisted: Set[int]) -> Set[int]:
    return _session_layer_grants_from_args(args_raw) | persisted


def _tool_get_identity_context(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    rl = args.get("requested_layers")
    if not isinstance(rl, list) or not rl:
        return {
            "status": "error",
            "error": "requested_layers must be a non-empty array of integers (ISA layers 1–7)",
        }
    try:
        layers = [int(x) for x in rl]
    except (TypeError, ValueError):
        return {"status": "error", "error": "requested_layers must be integers"}
    grants = _merge_tool_and_persisted_grants(
        args.get("session_layer_grants"),
        _persisted_session_layer_grants(),
    )
    lim_raw = args.get("limit_per_layer")
    try:
        lim = int(lim_raw) if lim_raw is not None else 24
    except (TypeError, ValueError):
        lim = 24
    payload, err = identity.grant_identity_context(
        conn,
        agent_id=str(args.get("agent_id") or "mcp_agent"),
        purpose=str(args.get("purpose") or ""),
        requested_layers=layers,
        session_grants=grants,
        limit_per_layer=lim,
    )
    if err:
        return {"status": "error", "error": err}
    assert payload is not None
    return {"status": "ok", **payload}


def _tool_get_identity_summary(_: Dict[str, Any]) -> Dict[str, Any]:
    conn = _get_conn()
    md = identity.build_identity_summary(conn)
    return {"status": "ok", "markdown": md}


def _tool_get_identity_schema(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "ok", "schema": identity.identity_schema_public()}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "ask_minion",
        "title": "Ask Minion — the user's digital identity",
        "description": (
            "Search the user's local Minion index (their memory). Full retrieval "
            "strategy lives in `initialize.instructions` and optional "
            "`retrieval_policy.md` — follow those; this tool is the primary search.\n\n"
            "Modes: `relevance` (default, semantic) · `oldest` / `newest` "
            "(time-ordered; query optional) · `keyword` (FTS5; use when proper "
            "nouns or exact phrases matter). Use `before` / `after` / `since` "
            "filters as needed. Expand hits with `get_chunk`; threads via "
            "`browse_conversations` + `conversation_chunks`.\n\n"
            "When citing: one markdown link per answer using `file_url` from the hit; "
            "name the source; avoid raw paths or internal ids."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text. Optional when mode is 'oldest' or 'newest'.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["relevance", "oldest", "newest", "keyword"],
                    "default": "relevance",
                    "description": (
                        "relevance = semantic embedding search; "
                        "oldest/newest = chronological by chunk create_time (no embedding); "
                        "keyword = FTS5 exact-phrase / proper-noun search."
                    ),
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": TOP_K_CAP, "default": DEFAULT_TOP_K},
                "role": {"type": ["string", "null"], "description": "Filter by role: user or assistant"},
                "kind": {
                    "type": ["string", "null"],
                    "description": "Filter by source kind: chatgpt-export, text, html, pdf, docx, image, audio, code",
                },
                "path_glob": {
                    "type": ["string", "null"],
                    "description": "SQL GLOB over source path (e.g. '*/notes/*.md')",
                },
                "since": {
                    "type": ["number", "null"],
                    "description": "Only sources with mtime >= this unix timestamp",
                },
                "before": {
                    "type": ["number", "null"],
                    "description": "Only chunks with create_time <= this unix timestamp (temporal/keyword modes).",
                },
                "after": {
                    "type": ["number", "null"],
                    "description": "Only chunks with create_time >= this unix timestamp (temporal/keyword modes).",
                },
                "max_chars": {"type": "integer", "minimum": 50, "maximum": 4000, "default": DEFAULT_MAX_CHARS},
                "dedupe_by_source": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "get_chunk",
        "title": "Get a chunk by id",
        "description": "Fetch a single chunk by chunk_id (useful for expanding a search hit).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 50, "maximum": 10000, "default": DEFAULT_MAX_CHARS_FULL},
            },
            "required": ["chunk_id"],
        },
    },
    {
        "name": "commit_voice",
        "title": "Commit synthesized voice profile to disk",
        "description": (
            "Persist a synthesized voice-directives markdown block to voice.md. "
            "Called by Claude at the end of the voice-bootstrap flow specified "
            "in `initialize.instructions` (when the voice profile is unbuilt), "
            "after gathering evidence via `ask_minion` + `browse_conversations`. "
            "Overwrites the AUTO_DRAFT block; user hand-edits below the "
            "USER_EDITS sentinel are preserved. Enforces a size band "
            "(~200-6000 chars) and requires at least one markdown heading."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["voice_markdown"],
            "properties": {
                "voice_markdown": {
                    "type": "string",
                    "description": "Markdown body to write into the auto-draft block.",
                },
            },
        },
    },
    {
        "name": "append_to_voice",
        "title": "Append one directive to the voice profile",
        "description": (
            "Append a single durable directive to one section of voice.md. "
            "Call this mid-session ONLY after explicit user confirmation — e.g. "
            "user says 'save this', 'write like Didion from now on', 'never do X "
            "again'. Before calling, ask the user a one-sentence confirmation "
            "(e.g. 'Save \"<content>\" to your voice profile under <section>?'). "
            "Idempotent: content already present in the section is a no-op. "
            "Preserves every other section and the USER_EDITS block. Use for "
            "small, durable rules and named style references — NOT for long "
            "prose or one-off preferences. For full profile rewrites, use "
            "`commit_voice` instead."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["section", "content"],
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "Target H3 section. Must be one of: Typography, "
                        "Formatting, Length and density, Tone and register, "
                        "Style references, Hard nos, Voice sample."
                    ),
                    "enum": list(VOICE_SECTIONS),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Short directive to append (3-800 chars). "
                        "One rule or one reference per call."
                    ),
                },
            },
        },
    },
    {
        "name": "browse_conversations",
        "title": "Browse past chat conversations",
        "description": (
            "List distinct chat conversations from the user's past AI history, "
            "aggregated from chunk metadata. Returns [{conversation_id, "
            "conversation_title, first_create_time, last_create_time, "
            "message_count}]. Use when the user asks 'which chats have I had?', "
            "'list my conversations about X', or needs a directory view. Follow "
            "up with conversation_chunks to pull a full thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title_like": {
                    "type": ["string", "null"],
                    "description": "Case-insensitive substring filter on conversation_title.",
                },
                "since": {
                    "type": ["number", "null"],
                    "description": "Only convos whose last_create_time >= this unix ts.",
                },
                "until": {
                    "type": ["number", "null"],
                    "description": "Only convos whose last_create_time <= this unix ts.",
                },
                "order": {
                    "type": "string",
                    "enum": ["newest", "oldest", "most_messages"],
                    "default": "newest",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
    },
    {
        "name": "conversation_chunks",
        "title": "Fetch a whole conversation",
        "description": (
            "Return all chunks for a given conversation_id in chronological order. "
            "Use after ask_minion or browse_conversations surfaces a conversation_id "
            "that the user wants to explore in full."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 50, "maximum": 4000, "default": DEFAULT_MAX_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "list_sources",
        "title": "List indexed sources (or one in detail)",
        "description": (
            "Two modes, one tool:\n"
            "- List: returns (source_id, path, kind, chunk_count, mtime) for "
            "every indexed file matching the filters. Use before ask_minion "
            "when the user asks 'what do you know about X?' or to verify a "
            "file they just dropped into the inbox is indexed.\n"
            "- Detail: pass `source_id` to get full metadata for one source "
            "(parser, sha256, bytes, updated_at, parser-specific fields). "
            "Other filters are ignored in detail mode."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": ["string", "null"],
                    "description": "When set, returns full detail for this one source (other filters ignored).",
                },
                "kind": {"type": ["string", "null"]},
                "path_glob": {"type": ["string", "null"]},
                "since": {"type": ["number", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            },
        },
    },
    {
        "name": "propose_identity_update",
        "title": "Propose a structured identity claim",
        "description": (
            "Append a candidate identity claim with optional evidence chunk_ids from "
            "`ask_minion` hits. Status starts as proposed; the user approves in the Minion app."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "text"],
            "properties": {
                "kind": {"type": "string", "enum": sorted(identity.CLAIM_KINDS)},
                "text": {"type": "string"},
                "source_agent": {"type": ["string", "null"]},
                "confidence": {"type": ["number", "null"]},
                "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_rationales": {
                    "type": "array",
                    "items": {"type": ["string", "null"]},
                },
                "meta": {"type": "object", "additionalProperties": True},
                "layer": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 7,
                    "description": "ISA layer; omit to infer from kind.",
                },
                "field": {
                    "type": ["string", "null"],
                    "description": "Schema field key for this layer (call `get_identity_schema` for allowed keys per layer).",
                },
            },
        },
    },
    {
        "name": "list_identity_claims",
        "title": "List identity claims",
        "description": "Filter by status and/or kind. Use status='proposed' for the review queue.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {
                    "type": "string",
                    "enum": sorted(identity.CLAIM_STATUSES),
                    "description": "Omit to list all statuses.",
                },
                "kind": {"type": "string"},
                "layer": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 7,
                    "description": "Filter by ISA layer.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
        },
    },
    {
        "name": "set_identity_claim_status",
        "title": "Approve, reject, or transition an identity claim",
        "description": (
            "Updates claim status (e.g. proposed→active or proposed→rejected). "
            "Only call after the user explicitly confirms in chat — especially for `active` on sensitive layers."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["claim_id", "status"],
            "properties": {
                "claim_id": {"type": "string"},
                "status": {"type": "string", "enum": sorted(identity.CLAIM_STATUSES)},
                "superseded_by": {
                    "type": ["string", "null"],
                    "description": "Optional claim_id when status is superseded.",
                },
            },
        },
    },
    {
        "name": "get_identity_context",
        "title": "Identity context for agents (least privilege)",
        "description": (
            "Returns active claims for granted ISA layers only, and appends an audit row. "
            "Layers 1,3,6 are open; 2,4,5,7 require grants. Effective grants are the union of "
            "`session_layer_grants` in this call and layers persisted in Minion settings "
            "(Identity screen). HTTP clients may also send header X-Minion-Identity-Session-Grants: 2,5,7."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["purpose", "requested_layers"],
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Logical agent name for the access log.",
                    "default": "mcp_agent",
                },
                "purpose": {"type": "string", "minLength": 3},
                "requested_layers": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "minItems": 1,
                },
                "session_layer_grants": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "description": (
                        "Extra layers to grant for this call only (unioned with Minion settings identity.session_layer_grants)."
                    ),
                },
                "limit_per_layer": {"type": "integer", "minimum": 1, "maximum": 200, "default": 24},
            },
        },
    },
    {
        "name": "get_identity_summary",
        "title": "Identity summary (markdown)",
        "description": "Digest of active claims, pending proposals, and latest preference clusters.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "get_identity_schema",
        "title": "ISA identity schema",
        "description": (
            "Seven ISA layers with titles, access tiers (open / selective / locked), "
            "and valid `field` keys per layer for `propose_identity_update`. Same payload as HTTP GET /identity/schema."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "index_info",
        "title": "Index metadata",
        "description": "Return aggregate metadata about the loaded local index.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _jsonrpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    out: Dict[str, Any] = {"jsonrpc": "2.0", "error": err}
    if req_id is not None:
        out["id"] = req_id
    return out


def _normalize_tool_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list):
        return {"results": payload}
    if isinstance(payload, dict):
        return dict(payload)
    return {"value": payload}


def _maybe_inject_brief_structured(structured: Dict[str, Any]) -> Dict[str, Any]:
    if _SESSION_STATE.get("brief_sent"):
        return structured
    if structured.get("error"):
        return structured
    brief = _load_profile_brief()
    if not brief:
        return structured
    out = dict(structured)
    out["profile_brief"] = brief
    _SESSION_STATE["brief_sent"] = True
    return out


def _tool_result_structured(structured: Dict[str, Any], *, is_error: bool = False) -> Dict[str, Any]:
    """Single JSON body in `content` (no duplicate structuredContent) to save tokens on the wire."""
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "isError": bool(is_error),
    }


def _handle_initialize(req: Dict[str, Any]) -> Dict[str, Any]:
    global _MCP_BOOT_LOGGED
    req_id = req.get("id")
    _SESSION_STATE["brief_sent"] = False
    if not _MCP_BOOT_LOGGED:
        _MCP_BOOT_LOGGED = True
        apply_mcp_title()
        from embed_runtime import embed_batch_size, embed_idle_seconds

        try:
            tag = data_dir_sha8(_data_dir())
        except Exception:
            tag = "?"
        idle = embed_idle_seconds()
        log.info(
            "minion mcp role=mcp data_dir_sha8=%s embed_idle_sec=%s embed_batch=%s",
            tag,
            idle if idle is not None else "off",
            embed_batch_size(64),
        )

    instructions = _load_retrieval_instructions()

    built = _voice_is_built()
    voice = _load_voice() if built else None

    if built and voice:
        instructions += (
            "\n\n---\n\n"
            "# User voice (durable directives. Treat as binding style and preference rules.)\n\n"
            + voice
            + "\n\n_These voice directives are injected every session. They are "
            "binding: respect the nevers/preferences/style unless the user overrides "
            "them in-conversation._"
            + "\n\n## Capturing new voice signals mid-session\n\n"
            "When the user signals a durable voice preference in-session — "
            "statements like `save this`, `remember this`, `write like Didion "
            "from now on`, `never do X again`, or explicitly names an author "
            "or piece as a style target — capture it by:\n\n"
            "1. Asking a one-sentence confirmation: `Save \"<short paraphrase>\" "
            "to your voice profile under <section>?` Do not paraphrase rules "
            "the user has not stated.\n"
            "2. On yes, call `append_to_voice(section=..., content=...)`. "
            "Sections: Typography, Formatting, Length and density, Tone and "
            "register, Style references, Hard nos, Voice sample.\n"
            "3. On no or silence, drop it. Never persist without confirmation.\n\n"
            "Style references grow this way over time. Past chats seeded the "
            "initial profile; future chats refine it. The user never hand-edits "
            "voice.md above the USER_EDITS sentinel."
        )
    else:
        instructions += _VOICE_BOOTSTRAP_INSTRUCTIONS

    if _load_profile_brief() is not None:
        instructions += (
            "\n\nOn your first tool call this session, the tool result JSON includes "
            "`profile_brief` (condensed patterns from chat history). Treat it as priors, not binding rules."
        )
    instructions += (
        "\n\n## Digital identity graph (ISA)\n\n"
        "Claims are organized in seven ISA layers (facts, values, goals, relationships, "
        "behavioral patterns, preferences, sensitive). Call `get_identity_schema` for layer titles, "
        "access tiers, and valid `field` keys. Use `propose_identity_update` with optional "
        "layer/field; layer 7 requires meta.explicit_declaration true. "
        "`get_identity_context` returns only granted ISA layers (union of `session_layer_grants` in the tool call "
        "and layers saved in Minion Identity settings) plus always-open layers. "
        "`set_identity_claim_status` changes proposal status only after explicit user confirmation in chat. "
        "`list_identity_claims` and `get_identity_summary` surface the queue and a markdown digest."
    )
    return _jsonrpc_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "minion", "title": APP_NAME, "version": __version__},
            "instructions": instructions,
        },
    )


def _handle_tools_list(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    return _jsonrpc_result(req_id, {"tools": TOOLS})


_DISPATCH = {
    "ask_minion": _tool_ask_minion,
    "search_memory": _tool_ask_minion,  # legacy alias
    "get_chunk": _tool_get_chunk,
    "commit_voice": _tool_commit_voice,
    "append_to_voice": _tool_append_to_voice,
    "browse_conversations": _tool_browse_conversations,
    "conversation_chunks": _tool_conversation_chunks,
    "list_sources": _tool_list_sources,
    "index_info": _tool_index_info,
    "propose_identity_update": _tool_propose_identity_update,
    "list_identity_claims": _tool_list_identity_claims,
    "set_identity_claim_status": _tool_set_identity_claim_status,
    "get_identity_context": _tool_get_identity_context,
    "get_identity_summary": _tool_get_identity_summary,
    "get_identity_schema": _tool_get_identity_schema,
}


def _handle_tools_call(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    params = req.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}

    fn = _DISPATCH.get(name)
    if fn is None:
        return _jsonrpc_error(req_id, -32602, f"Unknown tool: {name}")

    try:
        structured = _normalize_tool_payload(fn(arguments))
        structured = _maybe_inject_brief_structured(structured)
        result = _tool_result_structured(structured, is_error=False)
    except Exception as e:
        result = _tool_result_structured({"error": str(e)}, is_error=True)
    return _jsonrpc_result(req_id, result)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            resp = _jsonrpc_error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        method = req.get("method")
        if req.get("id") is None:
            continue

        if method == "initialize":
            resp = _handle_initialize(req)
        elif method == "tools/list":
            resp = _handle_tools_list(req)
        elif method == "tools/call":
            resp = _handle_tools_call(req)
        elif method == "ping":
            resp = _jsonrpc_result(req.get("id"), {})
        else:
            resp = _jsonrpc_error(req.get("id"), -32601, f"Method not found: {method}")

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
