"""
End-to-end smoke tests for the Minion sidecar API.

Each test spawns a fresh sidecar on a random port against a scratch data dir
(see conftest.py::sidecar). Tests exercise the real HTTP + WebSocket stack
so wiring bugs in CORS, uvicorn lifespan, pydantic bodies, etc. surface
here before they hit the UI.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import websockets


# ---------------------------------------------------------------------------
# 1. /status
# ---------------------------------------------------------------------------


def test_status_ready(sidecar) -> None:
    r = sidecar.get("/status")
    assert r.status_code == 200
    body = r.json()

    assert body["counts"] == {"sources": 0, "chunks": 0}
    assert "database" in body
    assert body["database"]["ok"] is True
    assert body["database"]["error"] is None
    assert body["db_path"].endswith("memory.db")
    assert str(sidecar.data_dir) in body["data_dir"] or body["data_dir"].endswith(
        sidecar.data_dir.name
    )
    # Parser registry should advertise at least the core formats.
    exts = set(body["supported_extensions"])
    for required in (".md", ".py", ".pdf", ".txt"):
        assert required in exts, f"missing {required} in supported_extensions: {exts}"
    # Watcher is disabled in tests.
    assert body["watcher"]["running"] is False
    assert body["watcher"]["mode"] == "disabled"


def test_reconcile_picks_up_inbox_files(sidecar, staged_note: Path) -> None:
    """POST /reconcile should ingest files already on disk when the watcher is off."""
    dest = sidecar.inbox / "already-here.md"
    shutil.copy2(staged_note, dest)
    r = sidecar.post("/reconcile", {"force": False})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    sources = sidecar.wait_for_sources(1, timeout=45.0)
    assert len(sources) == 1
    assert sources[0]["path"].endswith("already-here.md")


# ---------------------------------------------------------------------------
# 2. POST /ingest (single file) -> /sources
# ---------------------------------------------------------------------------


def test_ingest_single_file(sidecar, staged_note: Path) -> None:
    r = sidecar.post("/ingest", {"path": str(staged_note)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "file"
    # The copy should have landed under the sidecar's inbox.
    assert str(sidecar.inbox) in body["queued"]

    sources = sidecar.wait_for_sources(1, timeout=45.0)
    assert len(sources) == 1, f"expected 1 source, got {sources}"
    src = sources[0]
    assert src["kind"] == "text"
    assert src["path"].endswith("staged-note.md")

    # /sources/{id} should return a chunk_count >= 1.
    info = sidecar.get(f"/sources/{src['source_id']}").json()
    assert info["chunk_count"] >= 1


# ---------------------------------------------------------------------------
# 3. POST /search
# ---------------------------------------------------------------------------


def test_search_returns_ingested_file(sidecar, staged_note: Path) -> None:
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)

    r = sidecar.post("/search", {"query": "Good Capital owns this repo", "top_k": 3})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "search returned no results"
    top = results[0]
    assert top["path"].endswith("staged-note.md")
    assert top["score"] > 0.3, f"top score too low: {top['score']}"
    assert "Good Capital" in top["text"]


# ---------------------------------------------------------------------------
# 4. POST /ingest (directory) prunes junk dirs, keeps real files
# ---------------------------------------------------------------------------


def test_ingest_directory_prunes_junk(sidecar, staged_project: Path) -> None:
    r = sidecar.post("/ingest", {"path": str(staged_project)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "directory"
    # The tree has README.md + src/main.py kept; node_modules, .git, .DS_Store dropped.
    assert body["file_count"] == 2, f"expected 2 kept files, got {body}"

    sources = sidecar.wait_for_sources(2, timeout=60.0)
    paths = sorted(s["path"] for s in sources)
    assert any(p.endswith("README.md") for p in paths), paths
    assert any(p.endswith("src/main.py") for p in paths), paths
    assert not any("node_modules" in p for p in paths), paths
    assert not any("/.git/" in p for p in paths), paths
    assert not any(p.endswith(".DS_Store") for p in paths), paths

    # Copied tree on disk should also be pruned.
    inbox_files = sorted(p.relative_to(sidecar.inbox) for p in sidecar.inbox.rglob("*") if p.is_file())
    assert not any("node_modules" in str(p) for p in inbox_files), inbox_files
    assert not any(".git" in str(p).split("/") for p in inbox_files), inbox_files


# ---------------------------------------------------------------------------
# 5. DELETE /sources
# ---------------------------------------------------------------------------


def test_delete_source_removes_chunks(sidecar, staged_note: Path) -> None:
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sources = sidecar.wait_for_sources(1, timeout=45.0)
    src = sources[0]

    r = sidecar.delete("/sources", {"source_id": src["source_id"]})
    assert r.status_code == 200, r.text
    removed = r.json()["removed_chunks"]
    assert removed >= 1

    after = sidecar.get("/sources").json()
    assert after["counts"]["sources"] == 0
    assert after["counts"]["chunks"] == 0

    hits = sidecar.post("/search", {"query": "Good Capital", "top_k": 3}).json()["results"]
    assert not any(h["source_id"] == src["source_id"] for h in hits)


# ---------------------------------------------------------------------------
# 6. POST /connect/claude-desktop writes a valid MCP config
# ---------------------------------------------------------------------------


def test_connect_claude_desktop_writes_config(sidecar) -> None:
    # CLAUDE_DESKTOP_CONFIG was set in the sidecar's env by conftest, pointing
    # at a tmp file that does NOT yet exist -- the endpoint should create it.
    assert not sidecar.claude_cfg_path.exists()

    r = sidecar.post("/connect/claude-desktop", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_name"] == "minion"
    assert body["restart_required"] is True
    assert Path(body["config_path"]).resolve() == sidecar.claude_cfg_path.resolve()

    cfg = json.loads(sidecar.claude_cfg_path.read_text())
    assert "mcpServers" in cfg
    entry = cfg["mcpServers"]["minion"]
    assert entry["command"].endswith("python") or entry["command"].endswith("python3") or entry["command"].endswith("python3.11")
    assert entry["args"][0].endswith("mcp_server.py")
    assert entry["env"]["MINION_DATA_DIR"] == str(sidecar.data_dir)


def test_connect_claude_desktop_merges_existing(sidecar) -> None:
    """An existing config with other servers must be preserved + backed up."""
    existing = {
        "mcpServers": {
            "other-server": {"command": "/usr/bin/other", "args": [], "env": {}}
        },
        "unrelated_key": 42,
    }
    sidecar.claude_cfg_path.write_text(json.dumps(existing, indent=2))

    r = sidecar.post("/connect/claude-desktop", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backup_path"] is not None
    assert Path(body["backup_path"]).exists()

    cfg = json.loads(sidecar.claude_cfg_path.read_text())
    assert cfg["unrelated_key"] == 42
    assert "other-server" in cfg["mcpServers"]
    assert "minion" in cfg["mcpServers"]


# ---------------------------------------------------------------------------
# 7. POST /factory-reset and /nuke exist + work
# ---------------------------------------------------------------------------


def test_nuke_and_factory_reset_exist(sidecar, staged_note: Path) -> None:
    # Ingest one file so we know the DB and inbox are non-empty.
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)
    assert any(sidecar.inbox.iterdir()), "expected inbox to have at least one file after ingest"

    # /nuke should exist.
    r = sidecar.post("/nuke", {})
    assert r.status_code == 200, r.text

    # DB should be empty after nuke.
    after = sidecar.get("/sources").json()
    assert after["counts"]["sources"] == 0
    assert after["counts"]["chunks"] == 0

    # /factory-reset should exist and should clear the inbox too.
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)
    assert any(sidecar.inbox.iterdir()), "expected inbox to be non-empty before factory reset"

    r2 = sidecar.post("/factory-reset", {})
    assert r2.status_code == 200, r2.text
    assert not any(sidecar.inbox.iterdir()), "expected inbox to be empty after factory reset"


# ---------------------------------------------------------------------------
# 7. WebSocket /events streams ingest lifecycle
# ---------------------------------------------------------------------------


def _collect_events_until(ws_url: str, stop_types: set, timeout: float) -> List[Dict[str, Any]]:
    """Consume events until we've seen at least one of each `stop_types` or timeout."""
    seen: List[Dict[str, Any]] = []

    async def _run() -> List[Dict[str, Any]]:
        remaining = stop_types.copy()
        async with websockets.connect(ws_url) as ws:
            try:
                while remaining:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    msg = json.loads(raw)
                    seen.append(msg)
                    remaining.discard(msg.get("type"))
            except asyncio.TimeoutError:
                pass
        return seen

    return asyncio.run(_run())


def test_events_ws_streams_ingest(sidecar, staged_note: Path) -> None:
    """Open the WS first, then trigger ingest, and assert the lifecycle events land."""
    import threading

    result: Dict[str, Any] = {}

    def _listen() -> None:
        # snapshot is sent on connect; then we expect ingest_started + source_updated
        result["events"] = _collect_events_until(
            sidecar.ws_url,
            stop_types={"snapshot", "ingest_started", "source_updated"},
            timeout=45.0,
        )

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    # Give the WS a moment to connect + receive the snapshot before we POST.
    time.sleep(0.5)

    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    t.join(timeout=60.0)

    types = [e["type"] for e in result.get("events", [])]
    assert "snapshot" in types, f"no snapshot event: {types}"
    assert "ingest_started" in types, f"no ingest_started event: {types}"
    assert "source_updated" in types, f"no source_updated event: {types}"
