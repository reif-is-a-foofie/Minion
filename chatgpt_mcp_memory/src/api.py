"""
Minion local HTTP API.

Purpose: give the Tauri desktop app (or any local client) a small, typed
surface over the same SQLite store + ingest pipeline the MCP uses. Binds to
127.0.0.1 only; optional `MINION_API_TOKEN` enforces Bearer auth on mutating
routes (see GET /capabilities).

Endpoints:
  GET  /status                      -> counts, inbox path, db path, watcher
  GET  /sources                     -> list sources (kind / path_glob / since / limit)
  GET  /sources/{source_id}         -> source metadata
  DELETE /sources                   -> body: {"path": "..."} OR {"source_id": "..."}
  POST /search                      -> body: {"query", "top_k", "kind"?, "path_glob"?, "role"?}
  GET  /search/stream               -> SSE: events `meta`, `hit` (JSON per line), `done`, optional `error`
  GET  /identity/claims             -> list identity claims (optional ?status=&kind=)
  POST /identity/claims/propose     -> same shape as MCP propose_identity_update
  PATCH /identity/claims/{claim_id} -> {"status": "active"|"rejected"|...}
  GET  /identity/claims/{claim_id}/edges
  GET  /identity/summary            -> { "markdown": "..." }
  GET  /identity/clusters
  POST /identity/clusters/rebuild   -> run embedding clustering job
  POST /identity/export             -> write zip under data_dir/exports/
  GET  /chunks/{chunk_id}           -> one chunk for evidence drill-down
  GET  /capabilities                -> stable feature flags for local agent integrations
  POST /ingest                      -> body: {"path": "..."}  (copies path into inbox if outside)
  POST /reconcile                   -> body: {"force": bool}  rescan inbox → DB (optional re-embed all)
  WS   /events                      -> push ingest + heartbeat (see handler for `type` values)

Run:
  python src/api.py --host 127.0.0.1 --port 8765
  # or
  uvicorn api:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ingest import ingest_file, _looks_like_chatgpt_export
from parsers import ALL_KINDS, supported_extensions
from settings import apply_settings, load_settings, save_settings
import telemetry
import identity
from export_bundle import write_identity_export_zip
from preference_cluster import run_preference_clustering
from retrieval_bias import apply_identity_rerank, rrf_fuse
from store import (
    DB_FILENAME,
    connect,
    count_chunks,
    count_sources,
    delete_source,
    delete_source_by_path,
    fts_available,
    get_chunk,
    get_source,
    identity_claim_get,
    identity_edges_for_claim,
    keyword_search as store_keyword_search,
    list_sources,
    preference_clusters_list,
    search as store_search,
)
import numpy as np


log = logging.getLogger("minion.api")


# ---------------------------------------------------------------------------
# Shared state (one connection per thread; the asyncio loop gets its own)
# ---------------------------------------------------------------------------


class State:
    data_dir: Path
    inbox: Path
    db_path: Path
    loop: Optional[asyncio.AbstractEventLoop] = None
    # sqlite3 connections are single-thread; FastAPI dispatches sync handlers
    # onto a threadpool, so we stash one connection per thread.
    _tls: threading.local = threading.local()
    subscribers: Set[WebSocket] = set()
    subscribers_lock: asyncio.Lock = None  # initialised in lifespan
    # Active-ingest snapshot (for UI progress card) + lock guarding it.
    active: Dict[str, Any] = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
    active_lock: threading.Lock = threading.Lock()
    # Set when connect/query fails; cleared on successful /status probe.
    db_error: Optional[str] = None

    @classmethod
    def conn(cls) -> sqlite3.Connection:
        c = getattr(cls._tls, "conn", None)
        if c is None:
            c = connect(cls.db_path)
            cls._tls.conn = c
        return c


def _resolve_paths() -> None:
    env = os.environ.get("MINION_DATA_DIR")
    if env:
        State.data_dir = Path(env).expanduser().resolve()
    else:
        # Default to a user-level data directory.
        # The desktop shell always sets MINION_DATA_DIR, but this keeps the
        # sidecar consistent when run standalone.
        if sys.platform == "darwin":
            State.data_dir = Path.home() / "Library" / "Application Support" / "Minion" / "data"
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            State.data_dir = Path(appdata) / "Minion" / "data" if appdata else Path.home() / ".minion" / "data"
        else:
            State.data_dir = Path.home() / ".minion" / "data"
    State.data_dir.mkdir(parents=True, exist_ok=True)

    inbox_env = os.environ.get("MINION_INBOX")
    State.inbox = (
        Path(inbox_env).expanduser().resolve()
        if inbox_env
        else State.data_dir.parent / "inbox"
    )
    State.inbox.mkdir(parents=True, exist_ok=True)
    State.db_path = State.data_dir / DB_FILENAME


# ---------------------------------------------------------------------------
# WebSocket fanout — any ingest (from the watcher or the API) emits an event.
# ---------------------------------------------------------------------------


async def _broadcast(event: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    async with State.subscribers_lock:
        targets = list(State.subscribers)
    for ws in targets:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    if dead:
        async with State.subscribers_lock:
            for ws in dead:
                State.subscribers.discard(ws)


def _schedule_broadcast(event: Dict[str, Any]) -> None:
    """Thread-safe entry point for background threads."""
    loop = State.loop
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast(event), loop)


def _watcher_event_bridge(kind: str, payload: Dict[str, Any]) -> None:
    """Translate watcher/reconcile events into the WebSocket schema the UI expects."""
    if kind == "batch_started":
        with State.active_lock:
            State.active = {
                "root": "watcher",
                "total": int(payload.get("total", 0)),
                "done": 0,
                "added": 0,
                "skipped": 0,
            }
        _schedule_broadcast({
            "type": "ingest_started",
            "source": "watcher",
            "count": payload.get("total", 0),
            "active": dict(State.active),
        })
    elif kind == "file_started":
        _schedule_broadcast({
            "type": "ingest_progress",
            "path": payload.get("path"),
            "index": payload.get("index"),
            "total": payload.get("total"),
        })
    elif kind == "file_progress":
        _schedule_broadcast({
            "type": "file_progress",
            **{k: v for k, v in payload.items() if k != "type"},
        })
    elif kind == "file_done":
        skipped = bool(payload.get("skipped"))
        with State.active_lock:
            State.active["done"] = int(payload.get("index", State.active["done"]))
            if skipped:
                State.active["skipped"] += 1
            elif payload.get("source_id"):
                State.active["added"] += 1
        _schedule_broadcast({
            "type": "ingest_skipped" if skipped else "source_updated",
            "result": payload,
            "counts": _counts(),
            "active": dict(State.active),
        })
    elif kind == "file_failed":
        with State.active_lock:
            State.active["done"] = int(payload.get("index", State.active["done"]))
            State.active["skipped"] += 1
        _schedule_broadcast({
            "type": "ingest_failed",
            "path": payload.get("path"),
            "active": dict(State.active),
        })
    elif kind == "batch_done":
        snap = None
        with State.active_lock:
            snap = dict(State.active)
            State.active = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
        _schedule_broadcast({
            "type": "tree_done",
            "root": "watcher",
            "added": snap.get("added", 0),
            "skipped": snap.get("skipped", 0),
            "counts": _counts(),
        })
    elif kind == "removed":
        _schedule_broadcast({
            "type": "source_removed",
            "key": payload.get("path"),
            "counts": _counts(),
        })
    elif kind == "error":
        msg = str(payload.get("message") or "watcher error")
        if len(msg) > 800:
            msg = msg[:800] + "…"
        State.db_error = msg
        _schedule_broadcast({"type": "db_error", "message": msg})


# ---------------------------------------------------------------------------
# Watcher integration — start the same watcher the MCP uses, but wire its
# per-file events into our websocket fanout so the UI updates live.
# ---------------------------------------------------------------------------


_watcher_thread: Optional[threading.Thread] = None
_heartbeat_thread: Optional[threading.Thread] = None
_watcher_mode: str = "disabled"
_manual_reconcile_lock = threading.Lock()


def _start_watcher() -> None:
    global _watcher_thread, _heartbeat_thread, _watcher_mode
    _watcher_mode = "disabled"
    if os.environ.get("MINION_DISABLE_WATCHER") in ("1", "true", "TRUE"):
        return
    try:
        from watcher import reconcile_once, start_background, start_polling_watcher

        def _factory() -> sqlite3.Connection:
            return connect(State.db_path)

        # Reconcile in a background thread so lifespan startup finishes
        # immediately -- a large pre-existing inbox shouldn't block the
        # socket from binding. We broadcast "ready" once it's done.
        def _reconcile_bg() -> None:
            try:
                bg_conn = connect(State.db_path)
                try:
                    reconcile_once(bg_conn, State.inbox, on_event=_watcher_event_bridge)
                finally:
                    bg_conn.close()
                State.db_error = None
                _schedule_broadcast({"type": "ready", "counts": _counts()})
            except Exception as e:
                log.exception("startup reconcile failed")
                msg = str(e)
                if len(msg) > 800:
                    msg = msg[:800] + "…"
                State.db_error = msg
                _schedule_broadcast({"type": "db_error", "message": msg})

        threading.Thread(
            target=_reconcile_bg, name="minion-api-reconcile", daemon=True
        ).start()

        _watcher_thread = start_background(
            _factory, State.inbox, on_event=_watcher_event_bridge
        )
        if _watcher_thread is not None:
            _watcher_mode = "watchdog"
        else:
            _watcher_thread = start_polling_watcher(
                _factory, State.inbox, on_event=_watcher_event_bridge
            )
            _watcher_mode = "polling"

        # Even with watchdog, we emit periodic heartbeats so the UI can show
        # a live count without polling the HTTP API.
        def _heartbeat() -> None:
            while True:
                time.sleep(5.0)
                try:
                    _schedule_broadcast({"type": "heartbeat", "counts": _counts()})
                except Exception:
                    pass

        _heartbeat_thread = threading.Thread(
            target=_heartbeat, name="minion-api-heartbeat", daemon=True
        )
        _heartbeat_thread.start()
    except Exception:
        log.exception("failed to start watcher")
        _watcher_mode = "disabled"


def _counts() -> Dict[str, Any]:
    try:
        conn = State.conn()
        return {
            "sources": count_sources(conn),
            "chunks": count_chunks(conn),
        }
    except Exception:
        return {"sources": 0, "chunks": 0}


def _database_status() -> Dict[str, Any]:
    """Cheap DB health for GET /status (per-request thread may open first connection)."""
    try:
        conn = State.conn()
        conn.execute("SELECT 1").fetchone()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]) if row else None
        State.db_error = None
        return {"ok": True, "error": None, "journal_mode": mode}
    except Exception as e:
        msg = str(e)
        if len(msg) > 500:
            msg = msg[:500] + "…"
        State.db_error = msg
        return {"ok": False, "error": msg, "journal_mode": None}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    State.loop = asyncio.get_running_loop()
    State.subscribers_lock = asyncio.Lock()
    _resolve_paths()
    telemetry.configure(State.data_dir)
    # Load & apply user preferences before the watcher starts scanning the
    # inbox — otherwise a reconcile pass could ingest kinds the user has
    # already turned off.
    try:
        apply_settings(load_settings(State.data_dir))
    except Exception:
        log.exception("failed to load settings")
    _start_watcher()
    # Nudge Claude Desktop to re-read our tool descriptions + retrieval policy
    # whenever the MCP-relevant sources have changed since last launch. No-op
    # if Claude's config file doesn't exist (user hasn't opted in yet).
    _refresh_mcp_on_launch()
    yield


app = FastAPI(title="Minion Local API", version="0.1.0", lifespan=_lifespan)
# Allow Vite dev server (different port) to hit the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _mutation_bearer_auth(request: Request, call_next):
    """Optional MINION_API_TOKEN: require Bearer on mutating routes (GET stays open)."""
    tok = os.environ.get("MINION_API_TOKEN", "").strip()
    if not tok or request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)
    path = request.url.path
    if request.method == "POST" and path in ("/search",):
        return await call_next(request)
    auth = (request.headers.get("authorization") or "").strip()
    if auth != f"Bearer {tok}":
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


class SearchBody(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=20)
    kind: Optional[str] = None
    path_glob: Optional[str] = None
    role: Optional[str] = None
    since: Optional[float] = None
    max_chars: int = Field(default=600, ge=50, le=4000)


class IngestBody(BaseModel):
    path: str
    move: bool = False  # if True, move into inbox; else copy
    recursive: bool = True  # used when `path` is a directory


SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", "target", "build", "dist",
    "__pycache__", ".svelte-kit", ".next", ".nuxt",
    ".cache", ".DS_Store",
}


def _iter_files_in_tree(root: Path) -> List[Path]:
    """Walk a directory, skipping common build/cache dirs and dotfiles."""
    out: List[Path] = []
    stack: List[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                if p.name in SKIP_DIR_NAMES:
                    continue
                stack.append(p)
            elif p.is_file():
                out.append(p)
    return out


class DeleteBody(BaseModel):
    path: Optional[str] = None
    source_id: Optional[str] = None


class ConnectBody(BaseModel):
    server_name: str = "minion"
    config_path: Optional[str] = None


class SettingsBody(BaseModel):
    disabled_kinds: Optional[List[str]] = None


class ReconcileBody(BaseModel):
    force: bool = False


class IdentityProposeBody(BaseModel):
    kind: str
    text: str
    source_agent: Optional[str] = None
    confidence: Optional[float] = None
    evidence_chunk_ids: Optional[List[str]] = None
    evidence_rationales: Optional[List[Optional[str]]] = None
    meta: Optional[Dict[str, Any]] = None


class IdentityPatchBody(BaseModel):
    status: str
    superseded_by: Optional[str] = None


class ClusterRebuildBody(BaseModel):
    sample_limit: int = Field(default=1500, ge=100, le=5000)
    k: int = Field(default=8, ge=2, le=32)
    use_llm: bool = True


class IdentityExportBody(BaseModel):
    """Optional path; default writes to `<data_dir>/exports/minion-identity-<ts>.zip`."""

    out_path: Optional[str] = None
    include_chunk_index: bool = True
    include_voice_files: bool = True


@app.post("/nuke")
def nuke_db() -> Dict[str, Any]:
    """Delete the local memory database and related runtime artifacts.

    Intended for "factory reset" / clean-slate behaviour. The desktop app
    should restart the sidecar after calling this.
    """
    removed: List[str] = []
    missing: List[str] = []

    candidates = [
        State.db_path,
        State.data_dir / "telemetry.jsonl",
        State.data_dir / "telemetry.jsonl.1",
        State.data_dir / ".staging",
    ]
    for p in candidates:
        try:
            if not p.exists():
                missing.append(str(p))
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to remove {p}: {e.__class__.__name__}: {e}")

    # Ensure the directory still exists (so the next boot can recreate db).
    try:
        State.data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to ensure data_dir: {e.__class__.__name__}: {e}")

    # Drop any cached per-thread sqlite connection so a future request
    # can't keep using a deleted DB file handle.
    try:
        c = getattr(State._tls, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            State._tls.conn = None
    except Exception:
        pass

    return {"removed": removed, "missing": missing, "db_path": str(State.db_path)}


@app.post("/factory-reset")
def factory_reset() -> Dict[str, Any]:
    """More aggressive reset than /nuke.

    Deletes the database *and* clears the inbox directory contents.
    The desktop app should restart the sidecar after calling this.
    """
    result = nuke_db()
    inbox_removed: List[str] = []
    inbox_missing: List[str] = []

    try:
        if not State.inbox.exists():
            inbox_missing.append(str(State.inbox))
        else:
            # Remove children, not the inbox dir itself (so watchers/UX stay stable).
            for child in list(State.inbox.iterdir()):
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    inbox_removed.append(str(child))
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"failed to clear inbox item {child}: {e.__class__.__name__}: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to clear inbox: {e.__class__.__name__}: {e}")

    return {
        **result,
        "inbox": str(State.inbox),
        "inbox_removed": inbox_removed,
        "inbox_missing": inbox_missing,
    }


@app.get("/settings")
def settings_endpoint() -> Dict[str, Any]:
    data = load_settings(State.data_dir)
    return {"settings": data, "all_kinds": list(ALL_KINDS)}


@app.put("/settings")
def update_settings(body: SettingsBody) -> Dict[str, Any]:
    current = load_settings(State.data_dir)
    if body.disabled_kinds is not None:
        current["disabled_kinds"] = body.disabled_kinds
    saved = save_settings(State.data_dir, current)
    apply_settings(saved)
    return {"settings": saved, "all_kinds": list(ALL_KINDS)}


@app.post("/reconcile")
def reconcile_endpoint(body: ReconcileBody) -> Dict[str, Any]:
    """Full inbox scan → DB (and optional force re-embed). Runs in the background."""
    if not _manual_reconcile_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="reconcile already running")
    force = body.force

    def _run() -> None:
        try:
            from watcher import reconcile_once

            conn = connect(State.db_path)
            try:
                reconcile_once(
                    conn,
                    State.inbox,
                    force=force,
                    on_event=_watcher_event_bridge,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("manual reconcile failed")
        finally:
            _manual_reconcile_lock.release()

    threading.Thread(target=_run, name="minion-api-reconcile-manual", daemon=True).start()
    return {"started": True, "force": force}


@app.get("/status")
def status() -> Dict[str, Any]:
    with State.active_lock:
        active = dict(State.active)
    return {
        "data_dir": str(State.data_dir),
        "inbox": str(State.inbox),
        "db_path": str(State.db_path),
        "supported_extensions": supported_extensions(),
        "counts": _counts(),
        "active": active,
        "database": _database_status(),
        "watcher": {
            "running": _watcher_thread is not None and _watcher_thread.is_alive()
            if _watcher_thread
            else False,
            "mode": _watcher_mode,
        },
    }


@app.get("/capabilities")
def capabilities() -> Dict[str, Any]:
    """Lightweight contract for non-MCP local clients (same host as this API)."""
    tok_on = bool(os.environ.get("MINION_API_TOKEN", "").strip())
    return {
        "service": "minion-api",
        "version": "0.2.0",
        "schema_version": 1,
        "auth": {
            "mutation_bearer": tok_on,
            "scheme": "Bearer",
            "header": "Authorization",
            "policy": "GET and POST /search require no token; other POST/PUT/PATCH/DELETE require Authorization: Bearer <MINION_API_TOKEN> when set.",
        },
        "retrieval": {
            "identity_bias": True,
            "rrf_fusion": True,
        },
        "endpoints": {
            "search": "POST /search",
            "search_stream": "GET /search/stream",
            "ingest": "POST /ingest",
            "reconcile": "POST /reconcile",
            "events_ws": "WS /events",
            "identity_claims": "GET /identity/claims",
            "identity_propose": "POST /identity/claims/propose",
            "identity_export": "POST /identity/export",
            "clusters_rebuild": "POST /identity/clusters/rebuild",
        },
    }


@app.get("/sources")
def list_sources_endpoint(
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    rows = list_sources(
        State.conn(), kind=kind, path_glob=path_glob, since=since, limit=limit
    )
    return {"sources": rows, "counts": _counts()}


@app.get("/sources/{source_id}")
def source_info(source_id: str) -> Dict[str, Any]:
    src = get_source(State.conn(), source_id)
    if src is None:
        raise HTTPException(status_code=404, detail=f"source_id not found: {source_id}")
    conn = State.conn()
    cc = conn.execute(
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
        "chunk_count": int(cc),
        "meta": src.meta,
    }


@app.delete("/sources")
def delete_endpoint(body: DeleteBody) -> Dict[str, Any]:
    if not body.path and not body.source_id:
        raise HTTPException(status_code=400, detail="path or source_id required")
    if body.source_id:
        n = delete_source(State.conn(), body.source_id)
        key = body.source_id
    else:
        p = str(Path(body.path).expanduser().resolve())
        n = delete_source_by_path(State.conn(), p)
        key = p
    _schedule_broadcast({"type": "source_removed", "key": key, "counts": _counts()})
    return {"removed_chunks": n}


_query_model = None
_query_model_lock = threading.Lock()


def _get_query_model():
    global _query_model
    with _query_model_lock:
        if _query_model is not None:
            return _query_model
        from fastembed import TextEmbedding
        from store import get_meta

        name = (
            get_meta(State.conn(), "model_name")
            or os.environ.get("MINION_EMBED_MODEL")
            or "sentence-transformers/all-MiniLM-L6-v2"
        )
        _query_model = TextEmbedding(model_name=name)
        return _query_model


def _embed_search_results(
    query: str,
    top_k: int,
    kind: Optional[str],
    path_glob: Optional[str],
    since: Optional[float],
    role: Optional[str],
    max_chars: int,
) -> List[Dict[str, Any]]:
    conn = State.conn()
    model = _get_query_model()
    vec = np.asarray(next(iter(model.embed([query]))), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    internal_k = max(top_k * 3, top_k + 8)
    relevance_hits = store_search(
        conn,
        vec,
        top_k=internal_k,
        kind=kind,
        path_glob=path_glob,
        since=since,
        role=role,
    )
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
            )
            if keyword_hits:
                hits = rrf_fuse(relevance_hits, keyword_hits)
                rerank_used = "rrf"
        except Exception:
            log.exception("RRF fusion failed; relevance-only")
    hits, bias_meta = apply_identity_rerank(conn, hits)
    hits = hits[:top_k]

    results: List[Dict[str, Any]] = []
    for h in hits:
        text = h.text
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        results.append(
            {
                "score": round(h.score, 4),
                "chunk_id": h.chunk_id,
                "role": h.role,
                "source_id": h.source_id,
                "path": h.path,
                "kind": h.kind,
                "mtime": h.mtime,
                "text": text,
                "meta": h.meta,
            }
        )
    try:
        top = results[0] if results else {}
        telemetry.log_event(
            "search",
            mode="relevance",
            query=query or None,
            top_k=top_k,
            returned=len(results),
            top_score=top.get("score"),
            top_path=top.get("path"),
            top_kind=top.get("kind"),
            rerank=rerank_used,
            candidates=len(relevance_hits),
            content_dropped=None,
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


@app.post("/search")
def search_endpoint(body: SearchBody) -> Dict[str, Any]:
    return {
        "results": _embed_search_results(
            body.query,
            body.top_k,
            body.kind,
            body.path_glob,
            body.since,
            body.role,
            body.max_chars,
        )
    }


@app.get("/search/stream")
def search_stream(
    query: str,
    top_k: int = 8,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    role: Optional[str] = None,
    since: Optional[float] = None,
    max_chars: int = 600,
) -> StreamingResponse:
    """Server-Sent Events stream of semantic search hits (one `hit` event per result)."""

    def gen():
        try:
            rows = _embed_search_results(
                query, top_k, kind, path_glob, since, role, max_chars
            )
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
            return
        yield f"event: meta\ndata: {json.dumps({'count': len(rows), 'query': query})}\n\n"
        for row in rows:
            yield f"event: hit\ndata: {json.dumps(row)}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/identity/claims")
def identity_claims_list(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    rows, err = identity.list_claims(State.conn(), status=status, kind=kind, limit=limit)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"claims": rows, "count": len(rows)}


@app.get("/identity/claims/{claim_id}")
def identity_claim_detail(claim_id: str) -> Dict[str, Any]:
    row = identity_claim_get(State.conn(), claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return {"claim": row}


@app.get("/identity/claims/{claim_id}/edges")
def identity_claim_edges(claim_id: str) -> Dict[str, Any]:
    if identity_claim_get(State.conn(), claim_id) is None:
        raise HTTPException(status_code=404, detail="claim not found")
    edges = identity_edges_for_claim(State.conn(), claim_id)
    return {"edges": edges, "count": len(edges)}


@app.post("/identity/claims/propose")
def identity_propose(body: IdentityProposeBody) -> Dict[str, Any]:
    payload, err = identity.propose_identity_update(
        State.conn(),
        kind=body.kind,
        text=body.text,
        source_agent=body.source_agent,
        confidence=body.confidence,
        evidence_chunk_ids=body.evidence_chunk_ids,
        evidence_rationales=body.evidence_rationales,
        meta=body.meta,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    assert payload is not None
    telemetry.log_event("identity_propose", claim_id=payload.get("claim_id"))
    return payload


@app.patch("/identity/claims/{claim_id}")
def identity_patch_claim(claim_id: str, body: IdentityPatchBody) -> Dict[str, Any]:
    ok, err = identity.set_claim_status(
        State.conn(),
        claim_id,
        status=body.status,
        superseded_by=body.superseded_by,
    )
    if err:
        raise HTTPException(status_code=404 if "not found" in (err or "") else 400, detail=err)
    if not ok:
        raise HTTPException(status_code=404, detail="claim not found")
    State.conn().commit()
    telemetry.log_event(
        "identity_status",
        claim_id=claim_id,
        status=body.status,
    )
    row = identity_claim_get(State.conn(), claim_id)
    return {"claim": row}


@app.get("/identity/summary")
def identity_summary() -> Dict[str, Any]:
    return {"markdown": identity.build_identity_summary(State.conn())}


@app.get("/identity/clusters")
def identity_clusters() -> Dict[str, Any]:
    rows = preference_clusters_list(State.conn())
    return {"clusters": rows, "count": len(rows)}


@app.post("/identity/clusters/rebuild")
def identity_clusters_rebuild(body: ClusterRebuildBody) -> Dict[str, Any]:
    try:
        out = run_preference_clustering(
            State.conn(),
            sample_limit=body.sample_limit,
            k=body.k,
            use_llm=body.use_llm,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    State.conn().commit()
    return out


@app.post("/identity/export")
def identity_export(body: IdentityExportBody) -> Dict[str, Any]:
    if body.out_path:
        out = Path(body.out_path).expanduser().resolve()
    else:
        exp = State.data_dir / "exports"
        exp.mkdir(parents=True, exist_ok=True)
        out = exp / f"minion-identity-{int(time.time())}.zip"
    try:
        meta = write_identity_export_zip(
            State.conn(),
            out_path=out,
            data_dir=State.data_dir,
            include_chunk_index=body.include_chunk_index,
            include_voice_files=body.include_voice_files,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    return meta


@app.get("/chunks/{chunk_id}")
def chunk_detail(chunk_id: str, max_chars: int = 4000) -> Dict[str, Any]:
    row = get_chunk(State.conn(), chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    text = row["text"]
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return {
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "role": row["role"],
        "path": row["path"],
        "kind": row["kind"],
        "mtime": row["mtime"],
        "text": text,
        "meta": row["meta"],
    }


def _resolve_file_dest(src_path: Path) -> Path:
    """Single-file destination under the inbox with collision-dedupe."""
    dest = State.inbox / src_path.name
    if not dest.exists() or dest.resolve() == src_path:
        return dest
    stem, suf = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = State.inbox / f"{stem} ({i}){suf}"
        if not candidate.exists():
            return candidate
        i += 1


def _resolve_dir_dest(src_dir: Path) -> Path:
    """Directory destination under the inbox with collision-dedupe."""
    dest = State.inbox / src_dir.name
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = State.inbox / f"{src_dir.name} ({i})"
        if not candidate.exists():
            return candidate
        i += 1


def _copy_tree_into_inbox(src_dir: Path, dest_root: Path) -> List[Path]:
    """Mirror src_dir into dest_root under the inbox, skipping junk dirs.

    Implementation note: we stage the copy into a sibling dir OUTSIDE the
    inbox first, then atomically rename it into place. Without this, the
    watcher's fs-event debouncer can flush on the first copied file - long
    before the rest of the tree arrives - and mis-detect a ChatGPT export
    as a single loose JSON. The rename guarantees the tree materializes
    under the inbox as one consistent burst of events.
    """
    copied: List[Path] = []
    staging_parent = State.data_dir / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="ingest-", dir=str(staging_parent)))
    stage_target = staging_dir / dest_root.name
    stage_target.mkdir(parents=True, exist_ok=True)

    try:
        stack: List[Path] = [src_dir]
        while stack:
            cur = stack.pop()
            try:
                entries = list(cur.iterdir())
            except OSError:
                continue
            for p in entries:
                if p.name.startswith("."):
                    continue
                rel = p.relative_to(src_dir)
                target = stage_target / rel
                if p.is_dir():
                    if p.name in SKIP_DIR_NAMES:
                        continue
                    target.mkdir(parents=True, exist_ok=True)
                    stack.append(p)
                elif p.is_file():
                    try:
                        shutil.copy2(str(p), str(target))
                    except OSError:
                        log.exception("copy failed: %s", p)

        # Atomic-ish move into the inbox. Same filesystem (data_dir and
        # data_dir/inbox share a parent), so os.rename is a metadata op
        # and the watcher sees the tree appear as one event burst.
        dest_root.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(stage_target), str(dest_root))
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    for root, _, files in os.walk(dest_root):
        for name in files:
            copied.append(Path(root) / name)
    return copied


@app.post("/ingest")
async def ingest_endpoint(body: IngestBody) -> Dict[str, Any]:
    """Bring a file or directory into the inbox and ingest it. The HTTP call
    returns as soon as the copy is done; ingestion runs in the background and
    streams progress over the /events WebSocket."""
    src_path = Path(body.path).expanduser().resolve()
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {src_path}")

    # -------- Directory path: recurse, then ingest every file in the tree ----
    if src_path.is_dir():
        if not body.recursive:
            raise HTTPException(status_code=400, detail="path is a directory; set recursive=true")
        # Preserve tree structure under inbox/<dirname>/ so dropping two
        # 'notes' folders doesn't collapse their contents together.
        try:
            src_path.relative_to(State.inbox)
            # Already inside the inbox -- the watcher is already seeing it.
            inbox_root = src_path
        except ValueError:
            inbox_root = _resolve_dir_dest(src_path)
            if body.move:
                shutil.move(str(src_path), str(inbox_root))
            else:
                _copy_tree_into_inbox(src_path, inbox_root)

        # ChatGPT export directories are a single logical source, not a
        # pile of loose JSONs. Hand the entire tree to the watcher: it
        # detects export dirs in `_find_chatgpt_export_dirs` and ingests
        # them via the chatgpt_export parser in one atomic pass. Running
        # a duplicate dir-ingest from here would race the watcher on the
        # same source_id and blank out the DB on commit-collision.
        if _looks_like_chatgpt_export(inbox_root):
            await _broadcast({
                "type": "ingest_started",
                "path": str(inbox_root),
                "count": 1,
                "kind_hint": "chatgpt-export",
                "note": "watcher will ingest as a single source",
            })
            return {"queued": str(inbox_root), "kind": "chatgpt-export", "file_count": 1}

        files = _iter_files_in_tree(inbox_root)

        async def _run_tree() -> None:
            with State.active_lock:
                State.active = {
                    "root": str(inbox_root),
                    "total": len(files),
                    "done": 0,
                    "added": 0,
                    "skipped": 0,
                }
                snap = dict(State.active)
            await _broadcast({
                "type": "ingest_started",
                "path": str(inbox_root),
                "count": len(files),
                "active": snap,
            })
            loop = asyncio.get_running_loop()

            def _work_one(p: Path) -> Dict[str, Any]:
                conn = connect(State.db_path)
                try:
                    res = ingest_file(conn, p)
                    return {
                        "path": res.path,
                        "source_id": res.source_id,
                        "kind": res.kind,
                        "parser": res.parser,
                        "chunk_count": res.chunk_count,
                        "skipped": res.skipped,
                        "reason": res.reason,
                    }
                finally:
                    conn.close()

            for i, p in enumerate(files, 1):
                await _broadcast({
                    "type": "ingest_progress",
                    "path": str(p),
                    "index": i,
                    "total": len(files),
                })
                res = await loop.run_in_executor(None, _work_one, p)
                with State.active_lock:
                    State.active["done"] = i
                    if res.get("source_id"):
                        State.active["added"] += 1
                    else:
                        State.active["skipped"] += 1
                    snap = dict(State.active)
                if res.get("source_id"):
                    await _broadcast({
                        "type": "source_updated",
                        "result": res,
                        "counts": _counts(),
                        "active": snap,
                    })
                else:
                    await _broadcast({
                        "type": "ingest_skipped",
                        "result": res,
                        "active": snap,
                    })
            with State.active_lock:
                final = dict(State.active)
                State.active = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
            await _broadcast({
                "type": "tree_done",
                "root": str(inbox_root),
                "added": final.get("added", 0),
                "skipped": final.get("skipped", 0),
                "counts": _counts(),
            })

        asyncio.create_task(_run_tree())
        return {"queued": str(inbox_root), "kind": "directory", "file_count": len(files)}

    # -------- Single file path ---------------------------------------------
    if not src_path.is_file():
        raise HTTPException(status_code=400, detail=f"unsupported path type: {src_path}")

    try:
        src_path.relative_to(State.inbox)
        dest = src_path
    except ValueError:
        dest = _resolve_file_dest(src_path)
        if body.move:
            shutil.move(str(src_path), str(dest))
        else:
            shutil.copy2(str(src_path), str(dest))

    async def _run_ingest() -> Dict[str, Any]:
        await _broadcast({"type": "ingest_started", "path": str(dest)})
        loop = asyncio.get_running_loop()

        def _work() -> Dict[str, Any]:
            conn = connect(State.db_path)
            try:
                res = ingest_file(conn, dest)
                return {
                    "path": res.path,
                    "source_id": res.source_id,
                    "kind": res.kind,
                    "parser": res.parser,
                    "chunk_count": res.chunk_count,
                    "skipped": res.skipped,
                    "reason": res.reason,
                }
            finally:
                conn.close()

        res = await loop.run_in_executor(None, _work)
        await _broadcast(
            {
                "type": "source_updated" if res.get("source_id") else "ingest_skipped",
                "result": res,
                "counts": _counts(),
            }
        )
        return res

    asyncio.create_task(_run_ingest())
    return {"queued": str(dest), "kind": "file"}


# ---------------------------------------------------------------------------
# Claude Desktop MCP registration
#
# Two entry points share the same upserter:
#   1. /connect/claude-desktop       — UI "Connect" button; creates config if
#                                      missing (explicit user opt-in).
#   2. _refresh_mcp_on_launch()      — called from lifespan startup; only
#                                      updates an existing entry so we never
#                                      auto-install for users who don't run
#                                      Claude.
#
# We stash a short content hash of the MCP-relevant sources under
# env.MINION_BUILD_SHA. Claude Desktop watches claude_desktop_config.json and
# reconnects any server whose entry mutates, so a hash bump forces it to
# re-read tools/list and initialize.instructions — exactly what "uninstall +
# reinstall" would do, minus the race window where the server goes missing.
# ---------------------------------------------------------------------------


def _default_claude_cfg_path() -> Optional[Path]:
    env = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _mcp_build_sha() -> str:
    """Short content hash of everything that shapes Claude's view of Minion:
    tool descriptions (mcp_server.py) and the retrieval policy (injected into
    initialize.instructions). Changes here are the signal we need Claude to
    reconnect for."""
    import hashlib

    h = hashlib.sha256()
    mcp_script = Path(__file__).resolve().parent / "mcp_server.py"
    try:
        h.update(mcp_script.read_bytes())
    except OSError:
        pass
    for candidate in (
        State.data_dir / "retrieval_policy.md",
        State.data_dir.parent / "retrieval_policy.md",
    ):
        try:
            h.update(candidate.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _build_mcp_entry() -> Dict[str, Any]:
    mcp_script = Path(__file__).resolve().parent / "mcp_server.py"
    return {
        "command": sys.executable,
        "args": [str(mcp_script)],
        "env": {
            "MINION_DATA_DIR": str(State.data_dir),
            "MINION_BUILD_SHA": _mcp_build_sha(),
        },
    }


def _upsert_mcp_entry(
    cfg_path: Path,
    server_name: str,
    *,
    create_if_missing: bool,
) -> Dict[str, Any]:
    """Idempotently merge Minion's MCP entry into Claude Desktop's config.

    Returns: {"action": one of "created"|"refreshed"|"noop"|"skipped_missing_config",
              "config_path": ..., "backup_path": ..., "server_name": ...,
              "build_sha": ...}
    """
    entry = _build_mcp_entry()
    build_sha = entry["env"]["MINION_BUILD_SHA"]

    if not cfg_path.exists():
        if not create_if_missing:
            return {
                "action": "skipped_missing_config",
                "config_path": str(cfg_path),
                "server_name": server_name,
                "build_sha": build_sha,
                "backup_path": None,
            }
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        config: Dict[str, Any] = {}
        raw_existed = False
    else:
        raw = cfg_path.read_text(encoding="utf-8")
        config = json.loads(raw) if raw.strip() else {}
        raw_existed = True

    if not isinstance(config, dict):
        raise ValueError("config JSON root must be an object")
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError('"mcpServers" must be an object')

    existing = servers.get(server_name)
    if existing == entry:
        return {
            "action": "noop",
            "config_path": str(cfg_path),
            "server_name": server_name,
            "build_sha": build_sha,
            "backup_path": None,
        }

    backup: Optional[Path] = None
    if raw_existed:
        backup = cfg_path.with_suffix(cfg_path.suffix + ".minion.bak")
        shutil.copy2(cfg_path, backup)

    servers[server_name] = entry
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(cfg_path)

    return {
        "action": "created" if existing is None else "refreshed",
        "config_path": str(cfg_path),
        "backup_path": str(backup) if backup else None,
        "server_name": server_name,
        "build_sha": build_sha,
    }


def _refresh_mcp_on_launch() -> None:
    """Called from lifespan startup. Refresh the Minion MCP entry if Claude
    Desktop already has a config — never auto-create one. Silent on any
    failure; this is a nicety, never a blocker."""
    if os.environ.get("MINION_SKIP_MCP_REFRESH"):
        return
    cfg_path = _default_claude_cfg_path()
    if cfg_path is None:
        return
    try:
        result = _upsert_mcp_entry(cfg_path, "minion", create_if_missing=False)
    except Exception:
        log.exception("mcp: auto-refresh failed")
        return
    if result["action"] in ("created", "refreshed"):
        log.info(
            "mcp: %s %s (sha=%s) — Claude Desktop will reconnect",
            result["action"], cfg_path, result.get("build_sha"),
        )


@app.post("/connect/claude-desktop")
def connect_claude_desktop(body: ConnectBody) -> Dict[str, Any]:
    """Merge the Minion MCP entry into Claude Desktop's config. Same behaviour
    as `minion mcp-config` — lets the UI do it with one click."""
    if body.config_path:
        cfg_path = Path(body.config_path).expanduser().resolve()
    else:
        cfg_path = _default_claude_cfg_path()
        if cfg_path is None:
            raise HTTPException(status_code=400, detail="could not resolve Claude Desktop config path")

    try:
        result = _upsert_mcp_entry(cfg_path, body.server_name, create_if_missing=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        detail = f"cannot write {cfg_path}: {e.strerror or 'os error'}"
        raise HTTPException(status_code=403, detail=detail)
    except Exception as e:
        # Avoid leaking stack traces into the UI; keep it actionable.
        raise HTTPException(status_code=500, detail=f"connect failed: {e.__class__.__name__}: {e}")

    return {
        "config_path": result["config_path"],
        "backup_path": result.get("backup_path"),
        "server_name": result["server_name"],
        "restart_required": result["action"] != "noop",
    }


@app.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    async with State.subscribers_lock:
        State.subscribers.add(ws)
    # Send a snapshot on connect so the UI hydrates without a separate fetch.
    try:
        with State.active_lock:
            active = dict(State.active)
        await ws.send_json({
            "type": "snapshot",
            "counts": _counts(),
            "active": active,
        })
        while True:
            # We don't expect client messages; drain to keep the connection alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with State.subscribers_lock:
            State.subscribers.discard(ws)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("MINION_API_PORT", "8765")))
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    # Default: log to stderr. If MINION_LOG_FILE is set (desktop app), also
    # write to a rotating file so users can debug first-launch issues.
    log_path = os.environ.get("MINION_LOG_FILE", "").strip()
    file_audit = bool(log_path)
    stream_h = logging.StreamHandler()
    if args.verbose:
        stream_h.setLevel(logging.INFO)
    else:
        stream_h.setLevel(logging.WARNING)
    handlers: List[logging.Handler] = [stream_h]
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler

            Path(log_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            file_h = RotatingFileHandler(
                filename=str(Path(log_path).expanduser()),
                maxBytes=10 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            file_h.setLevel(logging.INFO)
            handlers.append(file_h)
        except Exception:
            # Never crash startup due to logging.
            pass

    # Root must allow INFO when a file handler needs it, even if stderr stays WARNING-only.
    root_level = logging.INFO if (args.verbose or file_audit) else logging.WARNING
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    import uvicorn

    # Tauri's sidecar looks at stdout for readiness; print a single line so
    # the Rust shell can flip from "starting" to "connected".
    print(f"[minion-api] listening http://{args.host}:{args.port}", flush=True)
    if file_audit:
        log.info("listening http://%s:%s (file log=%s)", args.host, args.port, log_path)
    uvicorn_log_level = "info" if (args.verbose or file_audit) else "warning"
    uvicorn.run(app, host=args.host, port=args.port, log_level=uvicorn_log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
