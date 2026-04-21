"""
Minion local HTTP API.

Purpose: give the Tauri desktop app (or any local client) a small, typed
surface over the same SQLite store + ingest pipeline the MCP uses. No auth —
binds to 127.0.0.1 only.

Endpoints:
  GET  /status                      -> counts, inbox path, db path, watcher
  GET  /sources                     -> list sources (kind / path_glob / since / limit)
  GET  /sources/{source_id}         -> source metadata
  DELETE /sources                   -> body: {"path": "..."} OR {"source_id": "..."}
  POST /search                      -> body: {"query", "top_k", "kind"?, "path_glob"?, "role"?}
  POST /ingest                      -> body: {"path": "..."}  (copies path into inbox if outside)
  WS   /events                      -> push: {"type": "source_added|updated|removed", ...}

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
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ingest import ingest_file
from parsers import supported_extensions
from store import (
    DB_FILENAME,
    connect,
    count_chunks,
    count_sources,
    delete_source,
    delete_source_by_path,
    get_source,
    list_sources,
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
    _conn: Optional[sqlite3.Connection] = None
    _conn_lock: threading.Lock = threading.Lock()
    # Per-connection ingest snapshots; keys are websocket ids.
    subscribers: Set[WebSocket] = set()
    subscribers_lock: asyncio.Lock = None  # initialised in lifespan

    @classmethod
    def conn(cls) -> sqlite3.Connection:
        with cls._conn_lock:
            if cls._conn is None:
                cls._conn = connect(cls.db_path)
            return cls._conn


def _resolve_paths() -> None:
    env = os.environ.get("MINION_DATA_DIR")
    if env:
        State.data_dir = Path(env).expanduser().resolve()
    else:
        here = Path(__file__).resolve()
        State.data_dir = here.parents[1] / "data" / "derived"
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


# ---------------------------------------------------------------------------
# Watcher integration — start the same watcher the MCP uses, but wire its
# per-file events into our websocket fanout so the UI updates live.
# ---------------------------------------------------------------------------


_watcher_thread: Optional[threading.Thread] = None
_watcher_poll_thread: Optional[threading.Thread] = None


def _start_watcher() -> None:
    if os.environ.get("MINION_DISABLE_WATCHER") in ("1", "true", "TRUE"):
        return
    try:
        from watcher import reconcile_once, start_background

        # Initial reconcile on the main thread, then broadcast the seed state.
        conn = State.conn()
        reconcile_once(conn, State.inbox)
        _schedule_broadcast({"type": "ready", "counts": _counts()})

        def _factory() -> sqlite3.Connection:
            return connect(State.db_path)

        global _watcher_thread, _watcher_poll_thread
        _watcher_thread = start_background(_factory, State.inbox)

        # Even with watchdog, we emit periodic heartbeats so the UI can show
        # a live count without polling the HTTP API.
        def _heartbeat() -> None:
            while True:
                time.sleep(5.0)
                try:
                    _schedule_broadcast({"type": "heartbeat", "counts": _counts()})
                except Exception:
                    pass

        _watcher_poll_thread = threading.Thread(
            target=_heartbeat, name="minion-api-heartbeat", daemon=True
        )
        _watcher_poll_thread.start()
    except Exception:
        log.exception("failed to start watcher")


def _counts() -> Dict[str, Any]:
    try:
        conn = State.conn()
        return {
            "sources": count_sources(conn),
            "chunks": count_chunks(conn),
        }
    except Exception:
        return {"sources": 0, "chunks": 0}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    State.loop = asyncio.get_running_loop()
    State.subscribers_lock = asyncio.Lock()
    _resolve_paths()
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


@app.get("/status")
def status() -> Dict[str, Any]:
    return {
        "data_dir": str(State.data_dir),
        "inbox": str(State.inbox),
        "db_path": str(State.db_path),
        "supported_extensions": supported_extensions(),
        "counts": _counts(),
        "watcher": {
            "running": _watcher_thread is not None and _watcher_thread.is_alive()
            if _watcher_thread
            else False,
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


@app.post("/search")
def search_endpoint(body: SearchBody) -> Dict[str, Any]:
    model = _get_query_model()
    vec = np.asarray(next(iter(model.embed([body.query]))), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    hits = store_search(
        State.conn(),
        vec,
        top_k=body.top_k,
        kind=body.kind,
        path_glob=body.path_glob,
        since=body.since,
        role=body.role,
    )
    results = []
    for h in hits:
        text = h.text
        if len(text) > body.max_chars:
            text = text[: body.max_chars - 1].rstrip() + "…"
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
    return {"results": results}


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
    """Mirror src_dir into dest_root under the inbox, skipping junk dirs."""
    dest_root.mkdir(parents=True, exist_ok=True)
    copied: List[Path] = []
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
            target = dest_root / rel
            if p.is_dir():
                if p.name in SKIP_DIR_NAMES:
                    continue
                target.mkdir(parents=True, exist_ok=True)
                stack.append(p)
            elif p.is_file():
                try:
                    shutil.copy2(str(p), str(target))
                    copied.append(target)
                except OSError:
                    log.exception("copy failed: %s", p)
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

        files = _iter_files_in_tree(inbox_root)

        async def _run_tree() -> None:
            await _broadcast({"type": "ingest_started", "path": str(inbox_root), "count": len(files)})
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

            added = 0
            skipped = 0
            for p in files:
                res = await loop.run_in_executor(None, _work_one, p)
                if res.get("source_id"):
                    added += 1
                    await _broadcast({
                        "type": "source_updated",
                        "result": res,
                        "counts": _counts(),
                    })
                else:
                    skipped += 1
                    await _broadcast({"type": "ingest_skipped", "result": res})
            await _broadcast({
                "type": "tree_done",
                "root": str(inbox_root),
                "added": added,
                "skipped": skipped,
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
        await ws.send_json({"type": "snapshot", "counts": _counts()})
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

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    # Tauri's sidecar looks at stdout for readiness; print a single line so
    # the Rust shell can flip from "starting" to "connected".
    print(f"[minion-api] listening http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
