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
from typing import Any, Dict, List, Optional

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
    get_chunk as store_get_chunk,
    get_conversation_chunks as store_get_conversation_chunks,
    get_meta,
    get_source,
    keyword_search as store_keyword_search,
    list_conversations as store_list_conversations,
    list_sources as store_list_sources,
    search as store_search,
)
from build_voice import (
    AUTO_DRAFT_SENTINEL,
    USER_EDITS_SENTINEL,
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
    "Minion is the authoritative archive of the user's past AI chat history. "
    "Call ask_minion for any question about prior chats, first/earliest/latest "
    "messages (mode='oldest'|'newest'), or rare proper nouns (mode='keyword'). "
    "Use get_chunk to expand a hit; browse_conversations to list chats; "
    "conversation_chunks to pull a whole thread; list_sources for the file index."
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
        if len(text) > cap:
            text = text[: cap - 30].rstrip() + "\n\n_…truncated…_\n"
        return text
    return None


# ---------------------------------------------------------------------------
# Index load (with legacy auto-migration)
# ---------------------------------------------------------------------------


_INDEX_LOCK = threading.RLock()  # reentrant: _get_model acquires then calls _get_conn
_CONN: Optional[sqlite3.Connection] = None
_MODEL: Optional[TextEmbedding] = None
_MODEL_NAME: Optional[str] = None

_SESSION_STATE: Dict[str, Any] = {"brief_sent": False}


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
                reconcile_once(conn, inbox)
            except Exception:
                log.exception("startup reconcile failed")
        start_background(_new_conn, inbox)
    except Exception:
        log.exception("failed to start watcher")


def _get_model() -> TextEmbedding:
    global _MODEL, _MODEL_NAME
    with _INDEX_LOCK:
        conn = _get_conn()
        name = (
            get_meta(conn, "model_name")
            or os.environ.get("MINION_EMBED_MODEL")
            or DEFAULT_EMBED_MODEL
        )
        if _MODEL is not None and _MODEL_NAME == name:
            return _MODEL
        _MODEL = TextEmbedding(model_name=name)
        _MODEL_NAME = name
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


def _hit_to_result(hit: Any, max_chars: int) -> Dict[str, Any]:
    meta = hit.meta or {}
    return {
        "score": round(hit.score, 4),
        "chunk_id": hit.chunk_id,
        "role": hit.role,
        "source_id": hit.source_id,
        "path": hit.path,
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
        internal_k = top_k * 3 if dedupe_by_source else top_k
        hits = store_search(
            conn,
            qvec,
            top_k=internal_k,
            kind=kind,
            path_glob=path_glob,
            since=since_f,
            role=role,
        )

    results: List[Dict[str, Any]] = []
    seen_sources: set[str] = set()
    for h in hits:
        if dedupe_by_source and h.source_id in seen_sources:
            continue
        seen_sources.add(h.source_id)
        results.append(_hit_to_result(h, max_chars))
        if len(results) >= top_k:
            break

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
    conn = _get_conn()
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


def _tool_source_info(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(arguments.get("source_id") or "")
    conn = _get_conn()
    src = get_source(conn, source_id)
    if src is None:
        raise ValueError(f"source_id not found: {source_id}")
    chunk_count = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (source_id,)
    ).fetchone()["n"]
    return {
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


def _tool_get_voice(_: Dict[str, Any]) -> Dict[str, Any]:
    """Return the durable user-voice directives verbatim.

    Also injected into initialize.instructions every session; this tool lets
    callers re-read it mid-session or in clients that don't surface the
    initialize payload to the model.
    """
    text = _load_voice()
    return {
        "present": bool(text),
        "built": _voice_is_built(),
        "voice": text or "",
        "char_count": len(text or ""),
    }


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


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "ask_minion",
        "title": "Ask Minion (user's past AI chat archive)",
        "description": (
            "Authoritative archive of the user's past AI chat history (imported "
            "ChatGPT export + local notes/docs/code/PDFs/images/audio transcripts). "
            "CALL THIS for any question about prior chats: what the user asked/"
            "said/decided before, first/earliest/latest messages, date-ranged "
            "history, recurring topics, or any question where private history "
            "beats general reasoning. Modes: "
            "`relevance` (semantic similarity, default), "
            "`oldest`/`newest` (chronological by message create_time; query optional), "
            "`keyword` (FTS5 exact-phrase / proper-noun recall). "
            "Use `before`/`after` to bound by chunk create_time. "
            "Call proactively at the start of substantive threads. "
            "Expand a hit with get_chunk; list chats with browse_conversations; "
            "fetch a whole thread with conversation_chunks."
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
        "name": "get_voice",
        "title": "Get user voice directives",
        "description": (
            "Return the user's durable voice profile: preferences, nevers, style "
            "and tone directives, and characteristic writing samples. Auto-"
            "injected into initialize.instructions every session, but call this "
            "any time you need to re-read the rules verbatim (e.g. before "
            "drafting long-form text in the user's voice)."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False},
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
        "title": "List indexed sources",
        "description": (
            "List files currently in memory with (path, kind, chunk_count, mtime). "
            "Use before ask_minion when the user asks 'what do you know about X?' "
            "or to verify a file they just dropped into the inbox is indexed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": ["string", "null"]},
                "path_glob": {"type": ["string", "null"]},
                "since": {"type": ["number", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            },
        },
    },
    {
        "name": "source_info",
        "title": "Source metadata",
        "description": "Full metadata for one source (path, parser, sha, bytes, chunk_count, parser-specific fields).",
        "inputSchema": {
            "type": "object",
            "properties": {"source_id": {"type": "string"}},
            "required": ["source_id"],
        },
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


def _tool_result(payload: Any, *, is_error: bool = False) -> Dict[str, Any]:
    if isinstance(payload, list):
        structured: Dict[str, Any] = {"results": payload}
    elif isinstance(payload, dict):
        structured = payload
    else:
        structured = {"value": payload}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": bool(is_error),
    }


def _maybe_inject_brief(result: Dict[str, Any]) -> Dict[str, Any]:
    if _SESSION_STATE.get("brief_sent"):
        return result
    if result.get("isError"):
        return result
    brief = _load_profile_brief()
    if not brief:
        return result
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        structured["profile_brief"] = brief
    content = result.get("content")
    if isinstance(content, list):
        content.insert(
            0,
            {
                "type": "text",
                "text": (
                    "# Minion profile brief (auto-injected, first tool call of session)\n\n"
                    + brief
                ),
            },
        )
    _SESSION_STATE["brief_sent"] = True
    return result


def _handle_initialize(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    _SESSION_STATE["brief_sent"] = False

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
            "them in-conversation. Call `get_voice` to re-read them verbatim._"
        )
    else:
        instructions += (
            "\n\n---\n\n"
            "# Voice bootstrap required (one-time, silent self-setup)\n\n"
            "The user's voice profile has not been built yet. On your FIRST "
            "substantive turn this session, run this self-setup silently "
            "BEFORE answering the user's question. It is a one-time action; "
            "the result is persisted to `voice.md` and auto-injected into "
            "every future session.\n\n"
            "## Procedure\n\n"
            "1. **Gather evidence via semantic retrieval.** `ask_minion` with "
            "`mode='relevance'` is an NLP semantic search over the user's own "
            "utterances (MiniLM embeddings over ~22k chunks). Trust it. Query "
            "in the *shape of the answer*, not the shape of the question. "
            "Users don't narrate their preferences (`I want responses "
            "formatted as...`); they state imperatives (`no emojis`, `be "
            "direct`). Phrase your queries as hypothetical user utterances. "
            "Always filter `role='user'` so you see only the user's own "
            "directives, not assistant replies. 3-5 queries is the right "
            "budget. Good seeds:\n\n"
            "```\n"
            "ask_minion(query=\"don't use emojis, em dashes, or ellipses in your responses\",\n"
            "           mode='relevance', role='user', top_k=12)\n"
            "ask_minion(query=\"respond in short paragraphs, no bullet points or headers\",\n"
            "           mode='relevance', role='user', top_k=12)\n"
            "ask_minion(query=\"be direct, no preamble, no hedging, cut the filler\",\n"
            "           mode='relevance', role='user', top_k=12)\n"
            "ask_minion(query=\"write in the voice of a specific author or register\",\n"
            "           mode='relevance', role='user', top_k=12)\n"
            "ask_minion(query=\"keep your tone confident and analytical, not apologetic\",\n"
            "           mode='relevance', role='user', top_k=12)\n"
            "```\n\n"
            "   Supplement with `browse_conversations(limit=15, order='newest')` "
            "for recent work context. Use `conversation_chunks(...)` to pull a "
            "whole thread only if a single hit needs more context. Reserve "
            "`mode='keyword'` for one narrow job: if a style reference surfaces "
            "(a name like `Paul Graham`, `Hemingway`), follow up with "
            "`mode='keyword'` to find all occurrences of that specific token. "
            "Do not use keyword mode as a semantic fallback.\n\n"
            "2. **Synthesize** a short, binding voice profile in Markdown. "
            "Use these exact H3 headings; mark any section without support "
            "as `_(insufficient signal)_` rather than inventing rules:\n\n"
            "   - `### Typography` - hard character rules (em dashes, "
            "emojis, ellipses, quotes).\n"
            "   - `### Formatting` - paragraphs vs bullets, headers, tables, "
            "code blocks.\n"
            "   - `### Length and density` - compression, preamble, hedging, "
            "word-count targets.\n"
            "   - `### Tone and register` - 3-5 specific phrases "
            "(`diagnostic, time-pressed, systems-minded`). Avoid generic "
            "adjectives like `professional` or `clear`.\n"
            "   - `### Style references` - writers, works, or registers the "
            "user explicitly references. Leave empty if none surface.\n"
            "   - `### Hard nos` - things the user rejects about responses.\n"
            "   - `### Voice sample` - ONE short paragraph (60-150 words) "
            "in the user's own voice, synthesized from patterns. Not a "
            "direct quote.\n\n"
            "3. **Commit** by calling `commit_voice` with the markdown as "
            "`voice_markdown`. Total length 400-1800 chars. The tool "
            "enforces size and heading checks.\n\n"
            "## Critical distinctions\n\n"
            "- **Voice directives vs content directives.** A user asking "
            "Claude to `write the words Enter and Exit on a sign` is about "
            "a generated asset, NOT voice. A user saying `respond in "
            "paragraphs` or `don't hedge` IS voice. When in doubt, omit.\n"
            "- **Evidence only.** Infer rules only from what the retrieved "
            "hits support. Do not import defaults or your general priors.\n"
            "- **Domain filtering.** If the user's chats are dominated by a "
            "domain (real estate, code, scripture), extract voice cues only; "
            "do not encode domain content as rules.\n\n"
            "## Housekeeping\n\n"
            "- Run silently. Do not narrate the bootstrap to the user.\n"
            "- Do not show the draft for approval. The user can always edit "
            "`voice.md` below the USER_EDITS sentinel afterward.\n"
            "- After `commit_voice` returns `status=ok`, proceed with the "
            "user's actual question and apply the voice you just synthesized "
            "from here onward in the conversation."
        )

    if _load_profile_brief() is not None:
        instructions += (
            "\n\nOn your first tool call this session, Minion also attaches a "
            "condensed user brief (observed patterns from chat history) under "
            "`structuredContent.profile_brief`. Treat it as priors, not binding rules."
        )
    return _jsonrpc_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "minion", "title": APP_NAME, "version": "0.2.0"},
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
    "get_voice": _tool_get_voice,
    "commit_voice": _tool_commit_voice,
    "browse_conversations": _tool_browse_conversations,
    "conversation_chunks": _tool_conversation_chunks,
    "list_sources": _tool_list_sources,
    "source_info": _tool_source_info,
    "index_info": _tool_index_info,
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
        result = _tool_result(fn(arguments))
    except Exception as e:
        result = _tool_result({"error": str(e)}, is_error=True)
    return _jsonrpc_result(req_id, _maybe_inject_brief(result))


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
