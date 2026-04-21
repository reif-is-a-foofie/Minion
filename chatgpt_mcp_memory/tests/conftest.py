"""
Shared pytest fixtures: spin up a real sidecar on a free port against a
scratch data dir / inbox, expose a tiny typed client, tear down cleanly.

We spawn the same `api.py` the Tauri shell spawns so the tests exercise the
whole HTTP + WebSocket stack (uvicorn, FastAPI, pydantic, CORS, lifespan) --
not a TestClient mock. Slightly slower, but catches wiring bugs that only
show up over the wire.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
API_SCRIPT = REPO_ROOT / "chatgpt_mcp_memory" / "src" / "api.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _pick_free_port() -> int:
    """Ask the kernel for a port, then close the socket so the sidecar can bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SidecarClient:
    base: str
    port: int
    data_dir: Path
    inbox: Path
    process: subprocess.Popen
    claude_cfg_path: Path

    @property
    def ws_url(self) -> str:
        return self.base.replace("http://", "ws://") + "/events"

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(self.base + path, timeout=15.0, **kwargs)

    def post(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.post(self.base + path, json=json_body, timeout=30.0, **kwargs)

    def delete(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.request(
            "DELETE", self.base + path, json=json_body, timeout=15.0, **kwargs
        )

    def wait_for_sources(self, expected_min: int, timeout: float = 30.0) -> List[Dict[str, Any]]:
        """Poll /sources until we see at least `expected_min` sources."""
        deadline = time.monotonic() + timeout
        last: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            r = self.get("/sources")
            r.raise_for_status()
            last = r.json()["sources"]
            if len(last) >= expected_min:
                return last
            time.sleep(0.25)
        return last


def _spawn_sidecar(
    data_dir: Path,
    inbox: Path,
    port: int,
    claude_cfg_path: Path,
) -> subprocess.Popen:
    """Spawn api.py with the test env. Use the interpreter running pytest so
    we inherit the same venv (which must have fastapi/uvicorn installed)."""
    env = os.environ.copy()
    env["MINION_DATA_DIR"] = str(data_dir)
    env["MINION_INBOX"] = str(inbox)
    env["MINION_DISABLE_WATCHER"] = "1"  # tests drive ingest explicitly
    env["CLAUDE_DESKTOP_CONFIG"] = str(claude_cfg_path)
    env["PYTHONPATH"] = str(API_SCRIPT.parent) + os.pathsep + env.get("PYTHONPATH", "")

    return subprocess.Popen(
        [sys.executable, str(API_SCRIPT), "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _wait_ready(base: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            # The sidecar died -- dump whatever it emitted before giving up.
            out = process.stdout.read() if process.stdout else ""
            raise RuntimeError(
                f"sidecar exited early (code={process.returncode}) before /status was ready.\n--- output ---\n{out}"
            )
        try:
            r = httpx.get(base + "/status", timeout=1.0)
            if r.status_code == 200:
                return
            last_err = f"status={r.status_code}"
        except Exception as e:  # pragma: no cover - timing dependent
            last_err = str(e)
        time.sleep(0.25)
    raise RuntimeError(f"sidecar not ready after {timeout}s: {last_err}")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sidecar(tmp_path: Path) -> SidecarClient:
    """Per-test sidecar -- each test gets a fresh DB, inbox, and Claude config.

    Scoped per-test (not session) because several tests mutate the DB in
    ways that would cross-contaminate (e.g. delete removes chunks the next
    test needs). With MINION_DISABLE_WATCHER=1 + a small fixture set the
    startup cost (~2s) is acceptable.
    """
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox"
    data_dir.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    claude_cfg = tmp_path / "claude_desktop_config.json"
    port = _pick_free_port()
    base = f"http://127.0.0.1:{port}"

    process = _spawn_sidecar(data_dir, inbox, port, claude_cfg)
    try:
        _wait_ready(base, process)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise

    client = SidecarClient(
        base=base,
        port=port,
        data_dir=data_dir,
        inbox=inbox,
        process=process,
        claude_cfg_path=claude_cfg,
    )

    try:
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


@pytest.fixture
def staged_note(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy note.md out to a tmp path so /ingest's copy-into-inbox step has
    a source outside the inbox to work with (same shape as real user input)."""
    src = fixtures_dir / "note.md"
    dest = tmp_path / "staged-note.md"
    shutil.copy2(src, dest)
    return dest


@pytest.fixture
def staged_project(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy the fixture project tree out to a tmp dir so each test gets a
    pristine source (we copy-into-inbox; we don't want to touch the
    committed fixtures)."""
    src = fixtures_dir / "project"
    dest = tmp_path / "staged-project"
    shutil.copytree(src, dest)
    return dest
