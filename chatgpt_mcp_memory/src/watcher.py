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

from ingest import _ARCHIVE_EXTS, IngestResult, _looks_like_chatgpt_export, ingest_file
from parsers import choose_parser
from store import delete_source_by_path, iter_source_ids, sha256_of_file

_watcher_err_lock = threading.Lock()
_watcher_err_last: float = 0.0


def _emit_watcher_error_bounded(
    on_event: Optional[Callable[[str, Dict[str, object]], None]],
    message: str,
    interval_sec: float = 30.0,
) -> None:
    """Rate-limit error events so a burst of file notifications does not spam the UI."""
    global _watcher_err_last
    if on_event is None:
        return
    now = time.monotonic()
    with _watcher_err_lock:
        if now - _watcher_err_last < interval_sec:
            return
        _watcher_err_last = now
    try:
        on_event("error", {"message": message})
    except Exception:
        log.exception("on_event failed for error payload")


def _is_ingestable(p: Path) -> bool:
    """A file is ingestable if a parser claims it OR it's an archive we unpack."""
    if choose_parser(p) is not None:
        return True
    return p.suffix.lower() in _ARCHIVE_EXTS


# Parser-cost tiers for priority-ordered ingest. Text-bearing parsers land
# first so search becomes useful within seconds; slow media parsers trail.
# Tier 0 ~ sub-second per file (plain text extraction).
# Tier 1 ~ seconds per file  (OCR over rapidocr / ollama vision).
# Tier 2 ~ tens of seconds   (faster-whisper transcription; grows with duration).
_PARSER_TIER: Dict[str, int] = {
    "text": 0, "html": 0, "code": 0, "pdf": 0, "docx": 0, "chatgpt-export": 0,
    "image": 1,
    "audio": 2,
    "video": 3,  # transcript + per-scene frame OCR/caption; slowest of all
}


def _parser_tier(p: Path) -> int:
    """Return cost tier for sorting; unknown kinds sort to the end."""
    # Archives are unpacked into the inbox; let them run after text but
    # before slow media so their contents surface quickly.
    if p.suffix.lower() in _ARCHIVE_EXTS:
        return 0
    chosen = choose_parser(p)
    if not chosen:
        return 3
    kind = chosen[0]
    return _PARSER_TIER.get(kind, 3)


def _find_chatgpt_export_dirs(inbox: Path) -> List[Path]:
    """Return any directory under `inbox` that looks like a ChatGPT export.

    We walk one level deep only (inbox children + grandchildren) to avoid a
    full-tree scan on every reconcile. ChatGPT exports are always dropped as
    a single top-level folder; we don't expect them nested deeply.
    """
    if not inbox.exists():
        return []
    found: List[Path] = []
    if _looks_like_chatgpt_export(inbox):
        found.append(inbox)
        return found
    for child in inbox.iterdir():
        if child.is_dir() and _looks_like_chatgpt_export(child):
            found.append(child)
    return found


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_owned_by_chatgpt_export(path: Path, export_root: Path) -> bool:
    """True if `path` is a file the chatgpt_export parser already handles,
    so the per-file dispatcher should skip it.

    Claimed:
    - `<export>/conversations*.json` (native OpenAI layout)
    - `<export>/json/*.json`          (per-conversation layout)
    - `<export>/markdown/*.md`        (markdown twins; duplicative)
    - `<export>/conversation-index.json`, `<export>/.export-progress.json`

    NOT claimed (fall through to per-file parsers so OCR/audio/etc. work):
    - `<export>/files/*`             (attachments)
    - anything else inside the export root
    """
    try:
        rel = path.resolve().relative_to(export_root.resolve())
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) == 1:
        name = parts[0]
        if name.startswith("conversations") and name.endswith(".json"):
            return True
        if name in {"conversation-index.json", ".export-progress.json"}:
            return True
        return False
    top = parts[0]
    if top == "json" and rel.suffix.lower() == ".json":
        return True
    if top == "markdown" and rel.suffix.lower() in {".md", ".markdown"}:
        return True
    return False


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
    on_event: Optional[Callable[[str, Dict[str, object]], None]] = None,
) -> ReconcileReport:
    """
    Walk the inbox, sync each file to DB, delete DB rows for files that
    no longer exist under the inbox.
    """
    inbox = Path(inbox).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    report = ReconcileReport()

    # ChatGPT export directories are single logical sources. Ingest each one
    # as a whole, then exclude their internal files from the per-file walk so
    # the generic text parser doesn't shred the per-conversation JSONs.
    export_dirs: List[Path] = [d.resolve() for d in _find_chatgpt_export_dirs(inbox)]
    for export_dir in export_dirs:
        try:
            res = ingest_file(conn, export_dir, force=force)
        except Exception:
            log.exception("chatgpt-export ingest failed: %s", export_dir)
            report.errors += 1
            continue
        if res.skipped:
            report.skipped += 1
            log.info("skipped export dir %s (%s)", export_dir, res.reason)
        elif res.source_id:
            report.added += 1
            log.info(
                "ingested export dir %s kind=%s parser=%s chunks=%d",
                export_dir, res.kind, res.parser, res.chunk_count,
            )
        report.details.append(res)

    on_disk: Dict[str, Path] = {}
    for p in _iter_inbox_files(inbox):
        if not _is_ingestable(p):
            continue
        # Only skip files the chatgpt_export parser already claims
        # (JSON manifests + markdown twins). Attachments under `files/`
        # still flow through their per-file parsers so images get OCR'd.
        if any(_is_owned_by_chatgpt_export(p, d) for d in export_dirs):
            continue
        on_disk[str(p)] = p

    # Drop tracked sources that no longer exist (within the inbox only).
    # Keep directory-level ChatGPT-export sources: their path is the export
    # root, not a file in on_disk, so naive file-presence check would wipe
    # the text index on every startup.
    inbox_str = str(inbox)
    live_export_paths = {str(d) for d in export_dirs}
    for source_id, path, _sha, _mtime in list(iter_source_ids(conn)):
        if not path.startswith(inbox_str):
            continue
        if path in live_export_paths:
            continue
        if path not in on_disk:
            n = delete_source_by_path(conn, path)
            if n:
                report.deleted += 1
                log.info("removed source (file gone): %s (%d chunks)", path, n)

    # Surface reconcile as a single batch so the UI can show
    # a progress card on startup just like a drag-drop. Priority-order
    # the items so a restart with mixed media surfaces text-based search
    # hits long before OCR/whisper finish.
    items = sorted(on_disk.items(), key=lambda kv: (_parser_tier(kv[1]), kv[0]))
    total = len(items)
    if on_event and total:
        try:
            on_event("batch_started", {"total": total})  # type: ignore[arg-type]
        except Exception:
            pass

    for i, (spath, p) in enumerate(items, 1):
        if on_event:
            try:
                on_event("file_started", {"path": spath, "index": i, "total": total})  # type: ignore[arg-type]
            except Exception:
                pass

            def _file_progress(stage: str, info: Dict[str, object], _p=spath, _i=i, _t=total) -> None:
                payload = {"path": _p, "index": _i, "total": _t, "stage": stage}
                payload.update(info)
                try:
                    on_event("file_progress", payload)  # type: ignore[arg-type]
                except Exception:
                    pass
        else:
            _file_progress = None  # type: ignore[assignment]

        try:
            kwargs = {"force": force}
            if on_event:
                kwargs["on_progress"] = _file_progress  # type: ignore[assignment]
            res = ingest_file(conn, p, **kwargs)
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
            try:
                on_event("file_done", {
                    "path": res.path,
                    "source_id": res.source_id,
                    "kind": res.kind,
                    "parser": res.parser,
                    "chunk_count": res.chunk_count,
                    "skipped": res.skipped,
                    "reason": res.reason,
                    "index": i,
                    "total": total,
                })  # type: ignore[arg-type]
            except Exception:
                pass
        report.details.append(res)

    if on_event and total:
        try:
            on_event("batch_done", {"total": total})  # type: ignore[arg-type]
        except Exception:
            pass

    return report


# ---------------------------------------------------------------------------
# Live watcher
# ---------------------------------------------------------------------------


class _Debouncer:
    """Coalesces rapid events into a single callback after `delay` seconds."""

    def __init__(
        self,
        delay: float,
        fn: Callable[[Set[str]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._delay = delay
        self._fn = fn
        self._on_error = on_error
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
            except Exception as e:
                log.exception("debounced handler failed")
                if self._on_error is not None:
                    try:
                        self._on_error(e)
                    except Exception:
                        log.exception("debouncer on_error failed")


def start_background(
    conn_factory: Callable[[], sqlite3.Connection],
    inbox: Path,
    *,
    debounce: float = DEFAULT_DEBOUNCE_SEC,
    on_event: Optional[Callable[[str, Dict[str, object]], None]] = None,
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

    def _emit(kind: str, payload: Dict[str, object]) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            log.exception("on_event callback failed")

    def _handle_batch(paths: Set[str]) -> None:
        conn = conn_factory()
        try:
            # Snapshot ChatGPT export dirs present in the inbox right now; any
            # file event inside one collapses to a single dir-level ingest.
            export_dirs: List[Path] = [d.resolve() for d in _find_chatgpt_export_dirs(inbox)]

            # Filter to actionable paths up front so batch_total is accurate.
            actionable: List[Path] = []
            seen_actionable: Set[str] = set()
            export_dirs_to_ingest: Set[Path] = set()
            for path_str in paths:
                raw = Path(path_str)
                if not raw.exists():
                    n = delete_source_by_path(conn, path_str)
                    if n:
                        log.info("removed source (deleted): %s", path_str)
                        _emit("removed", {"path": path_str, "chunks": n})
                    continue
                try:
                    p = raw.expanduser().resolve()
                except OSError:
                    log.warning("could not resolve inbox path %s", raw)
                    continue
                # Match ingest_file's stored path and WS `source_updated` keys
                # (always resolved). Watchdog often gives non-canonical paths; if
                # we emit progress under one string and done under another, the
                # UI leaves a stale row that later becomes a spurious "failed".
                # Also dedupe: /var/... and /private/var/... can both land in one
                # batch and double-ingest the same file (race → bogus failure).
                # Files under an export dir split two ways: the text-bearing
                # manifests are owned by the dir-level chatgpt_export source
                # (redirect), everything else (attachments) flows through
                # its own parser so OCR/whisper/etc. still run.
                owning_export = next(
                    (d for d in export_dirs if _is_owned_by_chatgpt_export(p, d)),
                    None,
                )
                if owning_export is not None:
                    export_dirs_to_ingest.add(owning_export)
                    continue
                if not p.is_file() or not _is_ingestable(p):
                    continue
                key = str(p)
                if key in seen_actionable:
                    continue
                seen_actionable.add(key)
                actionable.append(p)

            # Ingest any affected export dirs as single sources. Each one is
            # its own batch-of-one so the UI progress card treats it the same
            # as a file drop (batch_started -> file_progress -> file_done ->
            # batch_done).
            for export_dir in export_dirs_to_ingest:
                _emit("batch_started", {"total": 1})
                _emit("file_started", {"path": str(export_dir), "index": 1, "total": 1})

                def _export_progress(stage: str, info: Dict[str, object], _p=export_dir) -> None:
                    payload = {"path": str(_p), "index": 1, "total": 1, "stage": stage}
                    payload.update(info)
                    _emit("file_progress", payload)

                try:
                    res = ingest_file(conn, export_dir, on_progress=_export_progress)
                    if not res.skipped:
                        log.info(
                            "live ingested export dir %s kind=%s parser=%s chunks=%d",
                            export_dir, res.kind, res.parser, res.chunk_count,
                        )
                    _emit("file_done", {
                        "path": res.path,
                        "source_id": res.source_id,
                        "kind": res.kind,
                        "parser": res.parser,
                        "chunk_count": res.chunk_count,
                        "skipped": res.skipped,
                        "reason": res.reason,
                        "index": 1,
                        "total": 1,
                    })
                except Exception:
                    log.exception("live export-dir ingest failed: %s", export_dir)
                    _emit("file_failed", {"path": str(export_dir), "index": 1, "total": 1})
                _emit("batch_done", {"total": 1})

            if not actionable:
                return

            # Priority-order: text first, then OCR, then transcription.
            # Stable secondary sort on path so runs are deterministic.
            actionable.sort(key=lambda p: (_parser_tier(p), str(p)))

            total = len(actionable)
            _emit("batch_started", {"total": total})
            for i, p in enumerate(actionable, 1):
                _emit("file_started", {"path": str(p), "index": i, "total": total})

                def _file_progress(stage: str, info: Dict[str, object], _p=p, _i=i, _t=total) -> None:
                    payload = {"path": str(_p), "index": _i, "total": _t, "stage": stage}
                    payload.update(info)
                    _emit("file_progress", payload)

                try:
                    res = ingest_file(conn, p, on_progress=_file_progress)
                    if not res.skipped:
                        log.info(
                            "live ingested %s kind=%s parser=%s chunks=%d",
                            p, res.kind, res.parser, res.chunk_count,
                        )
                    _emit(
                        "file_done",
                        {
                            "path": res.path,
                            "source_id": res.source_id,
                            "kind": res.kind,
                            "parser": res.parser,
                            "chunk_count": res.chunk_count,
                            "skipped": res.skipped,
                            "reason": res.reason,
                            "index": i,
                            "total": total,
                        },
                    )
                except Exception:
                    log.exception("live ingest failed: %s", p)
                    _emit("file_failed", {"path": str(p), "index": i, "total": total})
            _emit("batch_done", {"total": total})
        finally:
            conn.close()

    debouncer = _Debouncer(
        debounce,
        _handle_batch,
        on_error=lambda e: _emit_watcher_error_bounded(on_event, str(e)),
    )

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


def start_polling_watcher(
    conn_factory: Callable[[], sqlite3.Connection],
    inbox: Path,
    *,
    interval_sec: float = 30.0,
    on_event: Optional[Callable[[str, Dict[str, object]], None]] = None,
) -> threading.Thread:
    """Scan the inbox on a fixed interval when live filesystem watching is unavailable."""
    inbox = Path(inbox).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)

    def _run() -> None:
        log.warning(
            "watchdog unavailable; polling inbox %s every %.0fs",
            inbox,
            interval_sec,
        )
        while True:
            try:
                conn = conn_factory()
                try:
                    reconcile_once(conn, inbox, on_event=on_event)
                finally:
                    conn.close()
            except Exception as e:
                log.exception("polling reconcile failed")
                _emit_watcher_error_bounded(on_event, str(e))
            time.sleep(interval_sec)

    t = threading.Thread(target=_run, name="minion-poll-watcher", daemon=True)
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
