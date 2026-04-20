"""
Inbox watcher + reconciler.

Two entry points:
- `reconcile_once(conn, inbox)`: scan inbox, add/update/delete to match disk.
  Called on startup and as a one-shot from the CLI.
- `start_background(conn_factory, inbox)`: start a daemon thread that
  watches the inbox with `watchdog` and debounces events to a reconciliation
  pass. Callers are expected to hand in a `conn_factory()` so each background
  operation can open its own SQLite connection (sqlite3 connections are not
  thread-safe by default).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from ingest import IngestResult, ingest_file
from parsers import choose_parser
from store import delete_source_by_path, iter_source_ids, sha256_of_file


log = logging.getLogger("minion.watcher")

DEFAULT_DEBOUNCE_SEC = 2.0


@dataclass
class ReconcileReport:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    details: List[IngestResult] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []


def _iter_inbox_files(inbox: Path) -> Iterable[Path]:
    for p in inbox.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if name.startswith(".") or name.endswith(".tmp") or name.endswith(".partial"):
            continue
        yield p


def reconcile_once(
    conn: sqlite3.Connection,
    inbox: Path,
    *,
    force: bool = False,
    on_event: Optional[Callable[[str, IngestResult], None]] = None,
) -> ReconcileReport:
    """
    Walk the inbox, sync each file to DB, delete DB rows for files that
    no longer exist under the inbox.
    """
    inbox = Path(inbox).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    report = ReconcileReport()

    on_disk: Dict[str, Path] = {}
    for p in _iter_inbox_files(inbox):
        if choose_parser(p) is None:
            continue
        on_disk[str(p)] = p

    # Drop tracked sources that no longer exist (within the inbox only).
    inbox_str = str(inbox)
    for source_id, path, _sha, _mtime in list(iter_source_ids(conn)):
        if not path.startswith(inbox_str):
            continue
        if path not in on_disk:
            n = delete_source_by_path(conn, path)
            if n:
                report.deleted += 1
                log.info("removed source (file gone): %s (%d chunks)", path, n)

    for spath, p in on_disk.items():
        try:
            res = ingest_file(conn, p, force=force)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("ingest failed: %s", p)
            report.errors += 1
            continue
        if res.skipped:
            if res.reason == "unchanged":
                report.skipped += 1
            else:
                report.skipped += 1
                log.info("skipped %s (%s)", p, res.reason)
        else:
            if res.source_id:
                report.added += 1
                log.info("ingested %s kind=%s parser=%s chunks=%d",
                         p, res.kind, res.parser, res.chunk_count)
        if on_event:
            on_event("reconcile", res)
        report.details.append(res)

    return report


# ---------------------------------------------------------------------------
# Live watcher
# ---------------------------------------------------------------------------


class _Debouncer:
    """Coalesces rapid events into a single callback after `delay` seconds."""

    def __init__(self, delay: float, fn: Callable[[Set[str]], None]) -> None:
        self._delay = delay
        self._fn = fn
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._pending: Set[str] = set()

    def nudge(self, path: str) -> None:
        with self._lock:
            self._pending.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            batch = self._pending
            self._pending = set()
            self._timer = None
        if batch:
            try:
                self._fn(batch)
            except Exception:
                log.exception("debounced handler failed")


def start_background(
    conn_factory: Callable[[], sqlite3.Connection],
    inbox: Path,
    *,
    debounce: float = DEFAULT_DEBOUNCE_SEC,
) -> Optional[threading.Thread]:
    """
    Start the watcher in a background daemon thread.
    Returns the thread, or None if `watchdog` is unavailable (caller should
    fall back to periodic reconcile_once).
    """
    try:
        from watchdog.events import FileSystemEventHandler  # type: ignore
        from watchdog.observers import Observer  # type: ignore
    except Exception:
        log.warning("watchdog not installed; live watching disabled")
        return None

    inbox = Path(inbox).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)

    def _handle_batch(paths: Set[str]) -> None:
        conn = conn_factory()
        try:
            for path_str in paths:
                p = Path(path_str)
                if not p.exists():
                    delete_source_by_path(conn, path_str)
                    log.info("removed source (deleted): %s", path_str)
                    continue
                if not p.is_file() or choose_parser(p) is None:
                    continue
                try:
                    res = ingest_file(conn, p)
                    if not res.skipped:
                        log.info(
                            "live ingested %s kind=%s parser=%s chunks=%d",
                            p, res.kind, res.parser, res.chunk_count,
                        )
                except Exception:
                    log.exception("live ingest failed: %s", p)
        finally:
            conn.close()

    debouncer = _Debouncer(debounce, _handle_batch)

    class _Handler(FileSystemEventHandler):  # type: ignore[misc]
        def on_created(self, event):  # type: ignore[override]
            if not event.is_directory:
                debouncer.nudge(event.src_path)

        def on_modified(self, event):  # type: ignore[override]
            if not event.is_directory:
                debouncer.nudge(event.src_path)

        def on_deleted(self, event):  # type: ignore[override]
            if not event.is_directory:
                debouncer.nudge(event.src_path)

        def on_moved(self, event):  # type: ignore[override]
            if not event.is_directory:
                debouncer.nudge(event.src_path)
                dest = getattr(event, "dest_path", None)
                if dest:
                    debouncer.nudge(dest)

    def _run() -> None:
        observer = Observer()
        observer.schedule(_Handler(), str(inbox), recursive=True)
        observer.start()
        log.info("watching inbox %s", inbox)
        try:
            while True:
                time.sleep(3600)
        except Exception:
            log.exception("watcher loop exited")
        finally:
            observer.stop()
            observer.join(timeout=5)

    t = threading.Thread(target=_run, name="minion-watcher", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# CLI entrypoint (also used by `minion watch`)
# ---------------------------------------------------------------------------


def _default_inbox(data_dir: Path) -> Path:
    return data_dir.parent / "inbox"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from store import DB_FILENAME, connect

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-dir",
        default=os.environ.get("MINION_DATA_DIR")
        or str(Path(__file__).resolve().parents[1] / "data" / "derived"),
        help="Directory holding memory.db",
    )
    p.add_argument("--inbox", default=None, help="Inbox directory to watch (defaults to <data_dir>/../inbox)")
    p.add_argument("--once", action="store_true", help="Reconcile and exit")
    p.add_argument("--force", action="store_true", help="Re-ingest even if sha matches")
    p.add_argument("--verbose", action="store_true", help="Enable INFO logs")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / DB_FILENAME
    inbox = Path(args.inbox).expanduser().resolve() if args.inbox else _default_inbox(data_dir)

    conn = connect(db_path)
    report = reconcile_once(conn, inbox, force=args.force)
    sys.stderr.write(
        f"reconcile: added={report.added} deleted={report.deleted} "
        f"skipped={report.skipped} errors={report.errors}\n"
    )
    if args.once:
        return 0

    def _factory() -> sqlite3.Connection:
        return connect(db_path)

    t = start_background(_factory, inbox)
    if t is None:
        sys.stderr.write(
            "watchdog unavailable; running periodic reconcile every 30s. "
            "Install watchdog for live updates.\n"
        )
        try:
            while True:
                time.sleep(30)
                reconcile_once(conn, inbox)
        except KeyboardInterrupt:
            return 0
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
