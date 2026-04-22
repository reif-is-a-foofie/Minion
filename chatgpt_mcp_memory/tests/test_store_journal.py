"""Unit tests for SQLite journal mode selection in store.connect."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import _apply_journal_mode  # noqa: E402


def _cursor_row(value: str) -> MagicMock:
    cur = MagicMock()
    cur.fetchone.return_value = (value,)
    return cur


def test_apply_journal_mode_falls_back_when_wal_raises() -> None:
    conn = MagicMock()
    db_path = Path("/tmp/fake-memory.db")

    def execute(sql: str, *a, **kw):
        s = str(sql).upper()
        if "JOURNAL_MODE=WAL" in s:
            raise sqlite3.OperationalError("disk I/O error")
        if "JOURNAL_MODE=DELETE" in s:
            return _cursor_row("delete")
        raise AssertionError(f"unexpected SQL: {sql!r}")

    conn.execute = execute  # type: ignore[method-assign]
    mode = _apply_journal_mode(conn, db_path)
    assert mode.lower() == "delete"


def test_apply_journal_mode_wal_success() -> None:
    conn = MagicMock()
    db_path = Path("/tmp/x.db")

    def execute(sql: str, *a, **kw):
        s = str(sql).upper()
        if "JOURNAL_MODE=WAL" in s:
            return _cursor_row("wal")
        raise AssertionError(f"unexpected SQL: {sql!r}")

    conn.execute = execute  # type: ignore[method-assign]
    mode = _apply_journal_mode(conn, db_path)
    assert mode.lower() == "wal"
