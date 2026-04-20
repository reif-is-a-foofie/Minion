"""
SQLite + sqlite-vec backed storage for Minion memory.

Schema
------
- sources(source_id PK, path UNIQUE, kind, sha256, mtime, bytes, parser, meta_json, updated_at)
- chunks(chunk_id PK, source_id FK ON DELETE CASCADE, seq, role, text, meta_json)
- vec_chunks (sqlite-vec virtual table): rowid -> embedding float[dim]

Invariants
----------
- chunks.rowid == vec_chunks.rowid for every live chunk (paired insert/delete).
- All multi-row writes happen inside a single transaction in upsert_source().
- sha256+mtime on sources lets the watcher skip unchanged files cheaply.

sqlite-vec is loaded via the `sqlite_vec` Python package's loadable extension.
See: https://github.com/asg017/sqlite-vec
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

try:
    import sqlite_vec  # type: ignore
except Exception:  # pragma: no cover - import guarded so docs-only installs still import
    sqlite_vec = None  # type: ignore


DEFAULT_EMBED_DIM = 384
DB_FILENAME = "memory.db"


# ---------------------------------------------------------------------------
# Dataclasses (mirror table rows, used in DAO signatures and return values)
# ---------------------------------------------------------------------------


@dataclass
class Source:
    source_id: str
    path: str
    kind: str
    sha256: str
    mtime: float
    bytes: int
    parser: str
    meta: Dict[str, Any]
    updated_at: float


@dataclass
class ChunkRow:
    chunk_id: str
    source_id: str
    seq: int
    role: Optional[str]
    text: str
    meta: Dict[str, Any]


@dataclass
class Hit:
    chunk_id: str
    score: float
    text: str
    role: Optional[str]
    source_id: str
    path: str
    kind: str
    mtime: float
    meta: Dict[str, Any]
    source_meta: Dict[str, Any]


# ---------------------------------------------------------------------------
# Connection + schema bootstrap
# ---------------------------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    source_id   TEXT PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    bytes       INTEGER NOT NULL,
    parser      TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);
CREATE INDEX IF NOT EXISTS idx_sources_mtime ON sources(mtime);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    role        TEXT,
    text        TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_role ON chunks(role);

-- Expression indices over chunk meta_json for temporal + per-conversation queries.
-- Idempotent; applied on every connect() so old DBs upgrade in-place.
CREATE INDEX IF NOT EXISTS idx_chunks_create_time
    ON chunks(json_extract(meta_json, '$.create_time'));
CREATE INDEX IF NOT EXISTS idx_chunks_conv_id
    ON chunks(json_extract(meta_json, '$.conversation_id'));

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks
    USING fts5(text, content='chunks', content_rowid='rowid',
               tokenize='porter unicode61 remove_diacritics 2');

CREATE TRIGGER IF NOT EXISTS chunks_ai_fts AFTER INSERT ON chunks BEGIN
    INSERT INTO fts_chunks(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad_fts AFTER DELETE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au_fts AFTER UPDATE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO fts_chunks(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    """Create the sqlite-vec virtual table if missing. Dim is baked into the DDL."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    ).fetchone()
    if row:
        return
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])"
    )


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    if sqlite_vec is None:
        raise RuntimeError(
            "sqlite-vec is not installed. Install with `pip install sqlite-vec`."
        )
    if not hasattr(conn, "enable_load_extension"):
        raise RuntimeError(
            "This Python was built without SQLite extension loading "
            "(PEP 524 flag --enable-loadable-sqlite-extensions). "
            "macOS system Python 3.9 ships this way. Use uv's Python 3.11 "
            "(`uv python install 3.11 && uv venv --python 3.11`) or a "
            "Homebrew/pyenv Python built with extension loading enabled."
        )
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def connect(db_path: Path, *, embed_dim: int = DEFAULT_EMBED_DIM) -> sqlite3.Connection:
    """
    Open (creating if needed) the memory DB, load sqlite-vec, apply schema.

    `embed_dim` is only used when creating vec_chunks for the first time; once
    set, the DB is locked to that dimension until dropped/recreated.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _load_vec_extension(conn)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript(_SCHEMA_SQL)
    _ensure_vec_table(conn, embed_dim)
    _ensure_fts_table(conn)

    existing = conn.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", ("embed_dim", str(embed_dim))
        )
        conn.commit()
    return conn


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 table + triggers; backfill from existing chunks on first open.

    Contentless-linked FTS5 (content='chunks') so we never double-store text.
    Backfill is one-time: idempotent guard by row-count comparison.
    """
    try:
        conn.executescript(_FTS_SCHEMA_SQL)
    except sqlite3.OperationalError as e:
        # FTS5 not compiled in — log via meta and skip. Keyword mode will error
        # at query time with a clear message.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("fts_unavailable", str(e)),
        )
        conn.commit()
        return

    # Lazy backfill: if fts has fewer rows than chunks, rebuild.
    try:
        n_chunks = int(conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        n_fts = int(conn.execute("SELECT COUNT(*) AS n FROM fts_chunks").fetchone()["n"])
    except sqlite3.OperationalError:
        return
    if n_chunks > 0 and n_fts < n_chunks:
        with transaction(conn):
            conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")


def fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_chunks'"
    ).fetchone()
    return row is not None


def get_embed_dim(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
    return int(row["value"]) if row else DEFAULT_EMBED_DIM


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def source_id_for(path: str) -> str:
    """Stable ID from the absolute path. Same path across runs => same source_id."""
    return "src-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def chunk_id_for(source_id: str, seq: int) -> str:
    return f"{source_id}:{seq:06d}"


def sha256_of_file(path: Path, *, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(bufsize)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _l2_normalise(vec: np.ndarray) -> np.ndarray:
    if vec.ndim == 1:
        n = float(np.linalg.norm(vec))
        return vec if n == 0.0 else vec / n
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vec / norms


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`with transaction(conn) as c:` — commits on success, rolls back on raise."""
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# DAO
# ---------------------------------------------------------------------------


def get_source_by_path(conn: sqlite3.Connection, path: str) -> Optional[Source]:
    row = conn.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at "
        "FROM sources WHERE path=?",
        (path,),
    ).fetchone()
    return _row_to_source(row) if row else None


def get_source(conn: sqlite3.Connection, source_id: str) -> Optional[Source]:
    row = conn.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at "
        "FROM sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return _row_to_source(row) if row else None


def list_sources(
    conn: sqlite3.Connection,
    *,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = [
        "SELECT s.source_id, s.path, s.kind, s.sha256, s.mtime, s.bytes, s.parser, "
        "s.meta_json, s.updated_at, "
        "(SELECT COUNT(*) FROM chunks c WHERE c.source_id=s.source_id) AS chunk_count "
        "FROM sources s WHERE 1=1"
    ]
    params: List[Any] = []
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if since is not None:
        sql.append("AND s.mtime >= ?")
        params.append(float(since))
    sql.append("ORDER BY s.mtime DESC LIMIT ?")
    params.append(int(limit))

    rows = conn.execute(" ".join(sql), params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "source_id": r["source_id"],
                "path": r["path"],
                "kind": r["kind"],
                "sha256": r["sha256"],
                "mtime": r["mtime"],
                "bytes": r["bytes"],
                "parser": r["parser"],
                "meta": json.loads(r["meta_json"] or "{}"),
                "updated_at": r["updated_at"],
                "chunk_count": int(r["chunk_count"]),
            }
        )
    return out


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        source_id=row["source_id"],
        path=row["path"],
        kind=row["kind"],
        sha256=row["sha256"],
        mtime=row["mtime"],
        bytes=row["bytes"],
        parser=row["parser"],
        meta=json.loads(row["meta_json"] or "{}"),
        updated_at=row["updated_at"],
    )


def delete_source(conn: sqlite3.Connection, source_id: str) -> int:
    """Delete a source + cascade chunks + vec rows. Returns chunks removed."""
    with transaction(conn):
        rows = conn.execute(
            "SELECT rowid FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()
        rowids = [int(r["rowid"]) for r in rows]
        for rid in rowids:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (rid,))
        conn.execute("DELETE FROM sources WHERE source_id=?", (source_id,))
    return len(rowids)


def delete_source_by_path(conn: sqlite3.Connection, path: str) -> int:
    src = get_source_by_path(conn, path)
    if src is None:
        return 0
    return delete_source(conn, src.source_id)


def upsert_source(
    conn: sqlite3.Connection,
    *,
    path: str,
    kind: str,
    sha256: str,
    mtime: float,
    bytes_: int,
    parser: str,
    source_meta: Dict[str, Any],
    chunks: Sequence[Tuple[str, Optional[str], Dict[str, Any]]],
    embeddings: np.ndarray,
) -> str:
    """
    Replace a source and all its chunks atomically.

    chunks: sequence of (text, role, chunk_meta) in seq order.
    embeddings: (N, dim) float32, row-aligned with chunks. Will be L2-normalised.
    Returns source_id.
    """
    if len(chunks) != embeddings.shape[0]:
        raise ValueError(
            f"chunks/embeddings length mismatch: {len(chunks)} vs {embeddings.shape[0]}"
        )

    expected_dim = get_embed_dim(conn)
    if embeddings.size and embeddings.shape[1] != expected_dim:
        raise ValueError(
            f"embedding dim mismatch: got {embeddings.shape[1]}, db expects {expected_dim}"
        )

    embeddings = _l2_normalise(embeddings.astype(np.float32, copy=False))
    sid = source_id_for(path)
    now = time.time()

    with transaction(conn):
        # Wipe prior rows (cascade clears chunks, but we still need vec cleanup).
        prior = conn.execute(
            "SELECT rowid FROM chunks WHERE source_id=?", (sid,)
        ).fetchall()
        for r in prior:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (int(r["rowid"]),))
        conn.execute("DELETE FROM sources WHERE source_id=?", (sid,))

        conn.execute(
            "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                path,
                kind,
                sha256,
                float(mtime),
                int(bytes_),
                parser,
                json.dumps(source_meta, ensure_ascii=False),
                now,
            ),
        )

        for seq, ((text, role, cmeta), emb) in enumerate(zip(chunks, embeddings)):
            cid = chunk_id_for(sid, seq)
            cur = conn.execute(
                "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (cid, sid, seq, role, text, json.dumps(cmeta, ensure_ascii=False)),
            )
            rid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                (rid, _vec_blob(emb)),
            )
    return sid


def _vec_blob(vec: np.ndarray) -> bytes:
    # sqlite-vec accepts raw little-endian float32 bytes for float[N] columns.
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    *,
    top_k: int = 8,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    role: Optional[str] = None,
) -> List[Hit]:
    """KNN search with optional source/role filters.

    To keep the filtering cheap we over-fetch KNN candidates (top_k * 8) and
    then filter+trim in SQL. For <100k chunks this is well under a ms.
    """
    q = _l2_normalise(query_vec.astype(np.float32, copy=False))
    expected = get_embed_dim(conn)
    if q.shape[0] != expected:
        raise ValueError(f"query dim mismatch: got {q.shape[0]}, db expects {expected}")

    fetch = max(top_k * 8, top_k + 16)
    cand = conn.execute(
        "SELECT rowid, distance FROM vec_chunks "
        "WHERE embedding MATCH ? AND k=? "
        "ORDER BY distance",
        (_vec_blob(q), fetch),
    ).fetchall()
    if not cand:
        return []

    rowids = [int(r["rowid"]) for r in cand]
    dist_by_rowid = {int(r["rowid"]): float(r["distance"]) for r in cand}

    placeholders = ",".join("?" * len(rowids))
    sql = [
        f"SELECT c.rowid AS rid, c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        f"s.path, s.kind, s.mtime, s.meta_json AS source_meta_json "
        f"FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        f"WHERE c.rowid IN ({placeholders})"
    ]
    params: List[Any] = list(rowids)
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if since is not None:
        sql.append("AND s.mtime >= ?")
        params.append(float(since))

    rows = conn.execute(" ".join(sql), params).fetchall()
    # Preserve vec-order, then cap.
    ordered = sorted(rows, key=lambda r: dist_by_rowid[int(r["rid"])])
    hits: List[Hit] = []
    for r in ordered[:top_k]:
        dist = dist_by_rowid[int(r["rid"])]
        # vec0's default distance is L2. Because we store L2-normalised vectors,
        # cos_sim = 1 - dist^2 / 2, which maps to [-1, 1] and matches the
        # cosine-similarity convention callers expect.
        score = 1.0 - (dist * dist) / 2.0
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                score=score,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
            )
        )
    return hits


def get_chunk(conn: sqlite3.Connection, chunk_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json "
        "FROM chunks c JOIN sources s ON s.source_id=c.source_id "
        "WHERE c.chunk_id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "role": row["role"],
        "text": row["text"],
        "path": row["path"],
        "kind": row["kind"],
        "mtime": row["mtime"],
        "meta": json.loads(row["meta_json"] or "{}"),
        "source_meta": json.loads(row["source_meta_json"] or "{}"),
    }


def browse_chunks_chronological(
    conn: sqlite3.Connection,
    *,
    order: str = "oldest",
    role: Optional[str] = None,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    before: Optional[float] = None,
    after: Optional[float] = None,
    query_substring: Optional[str] = None,
    limit: int = 10,
) -> List[Hit]:
    """Pure-SQL chronological retrieval over chunks by meta.create_time.

    No embedding. Drops hits with missing create_time so results are always sorted.
    `query_substring` is an optional case-insensitive LIKE filter on chunk.text.
    """
    if order not in ("oldest", "newest"):
        raise ValueError(f"order must be 'oldest' or 'newest', got {order!r}")
    direction = "ASC" if order == "oldest" else "DESC"

    sql = [
        "SELECT c.rowid AS rid, c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json, "
        "json_extract(c.meta_json, '$.create_time') AS ctime "
        "FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE json_extract(c.meta_json, '$.create_time') IS NOT NULL"
    ]
    params: List[Any] = []
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if before is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') <= ?")
        params.append(float(before))
    if after is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') >= ?")
        params.append(float(after))
    if query_substring:
        sql.append("AND LOWER(c.text) LIKE ?")
        params.append(f"%{query_substring.lower()}%")
    sql.append(f"ORDER BY ctime {direction} LIMIT ?")
    params.append(int(max(1, limit)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    hits: List[Hit] = []
    for r in rows:
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                # Score convention: for chronological results we use a recency
                # proxy in (0, 1]. 1.0 = rank-1, decreasing monotonically.
                score=1.0,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
            )
        )
    return hits


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 8,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    before: Optional[float] = None,
    after: Optional[float] = None,
) -> List[Hit]:
    """FTS5 BM25-ranked keyword search over chunk text.

    `query` is passed through to FTS5 MATCH; callers should pre-quote phrases
    with internal whitespace to avoid spurious operator parsing.
    """
    if not fts_available(conn):
        raise RuntimeError(
            "FTS5 table not available; this SQLite build may lack FTS5 support. "
            "Rebuild the index or use mode='relevance'."
        )
    if not query or not query.strip():
        return []

    sql = [
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json, "
        "bm25(fts_chunks) AS rank "
        "FROM fts_chunks "
        "JOIN chunks c ON c.rowid = fts_chunks.rowid "
        "JOIN sources s ON s.source_id = c.source_id "
        "WHERE fts_chunks MATCH ?"
    ]
    params: List[Any] = [_fts5_sanitize(query)]
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if before is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') <= ?")
        params.append(float(before))
    if after is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') >= ?")
        params.append(float(after))
    sql.append("ORDER BY rank LIMIT ?")
    params.append(int(max(1, top_k)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    hits: List[Hit] = []
    for r in rows:
        # bm25 returns lower-is-better; invert sign and clamp to a friendly 0..1-ish score.
        raw = float(r["rank"])
        score = 1.0 / (1.0 + max(0.0, raw))
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                score=score,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
            )
        )
    return hits


def _fts5_sanitize(query: str) -> str:
    """Make a user-typed string safe for FTS5 MATCH.

    FTS5 treats unquoted punctuation (colons, parens, quotes, dashes) as
    operators. We wrap each whitespace-separated token in double quotes,
    escaping embedded double quotes, which forces phrase/token matching
    without surprising operator semantics.
    """
    tokens = [t for t in query.split() if t.strip()]
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def list_conversations(
    conn: sqlite3.Connection,
    *,
    title_like: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    order: str = "newest",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Aggregate chunks by meta.conversation_id to list chat threads.

    `since`/`until` bound by the conversation's latest create_time.
    `order`: 'newest' | 'oldest' | 'most_messages'.
    """
    if order not in ("newest", "oldest", "most_messages"):
        raise ValueError(f"unknown order: {order!r}")

    sql = [
        "SELECT json_extract(c.meta_json, '$.conversation_id') AS conv_id, "
        "MAX(json_extract(c.meta_json, '$.conversation_title')) AS title, "
        "MIN(json_extract(c.meta_json, '$.create_time')) AS first_ts, "
        "MAX(json_extract(c.meta_json, '$.create_time')) AS last_ts, "
        "COUNT(*) AS msg_count "
        "FROM chunks c WHERE json_extract(c.meta_json, '$.conversation_id') IS NOT NULL"
    ]
    params: List[Any] = []
    if title_like:
        sql.append(
            "AND LOWER(COALESCE(json_extract(c.meta_json, '$.conversation_title'), '')) LIKE ?"
        )
        params.append(f"%{title_like.lower()}%")
    sql.append("GROUP BY conv_id")
    having: List[str] = []
    if since is not None:
        having.append("last_ts >= ?")
        params.append(float(since))
    if until is not None:
        having.append("last_ts <= ?")
        params.append(float(until))
    if having:
        sql.append("HAVING " + " AND ".join(having))
    if order == "newest":
        sql.append("ORDER BY last_ts DESC")
    elif order == "oldest":
        sql.append("ORDER BY first_ts ASC")
    else:
        sql.append("ORDER BY msg_count DESC")
    sql.append("LIMIT ?")
    params.append(int(max(1, limit)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    return [
        {
            "conversation_id": r["conv_id"],
            "conversation_title": r["title"],
            "first_create_time": (float(r["first_ts"]) if r["first_ts"] is not None else None),
            "last_create_time": (float(r["last_ts"]) if r["last_ts"] is not None else None),
            "message_count": int(r["msg_count"]),
        }
        for r in rows
    ]


def get_conversation_chunks(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return chunks belonging to one conversation, ordered by create_time then seq."""
    rows = conn.execute(
        "SELECT c.chunk_id, c.source_id, c.seq, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime "
        "FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE json_extract(c.meta_json, '$.conversation_id') = ? "
        "ORDER BY json_extract(c.meta_json, '$.create_time') ASC, c.seq ASC "
        "LIMIT ?",
        (conversation_id, int(max(1, limit))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        out.append(
            {
                "chunk_id": r["chunk_id"],
                "source_id": r["source_id"],
                "seq": int(r["seq"]),
                "role": r["role"],
                "path": r["path"],
                "kind": r["kind"],
                "mtime": r["mtime"],
                "conversation_id": meta.get("conversation_id"),
                "conversation_title": meta.get("conversation_title"),
                "create_time": meta.get("create_time"),
                "text": r["text"],
            }
        )
    return out


def count_chunks(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    return int(row["n"])


def count_sources(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
    return int(row["n"])


def iter_source_ids(conn: sqlite3.Connection) -> Iterable[Tuple[str, str, str, float]]:
    """Yield (source_id, path, sha256, mtime) for all sources. Used by watcher reconciliation."""
    for row in conn.execute(
        "SELECT source_id, path, sha256, mtime FROM sources ORDER BY path"
    ):
        yield row["source_id"], row["path"], row["sha256"], float(row["mtime"])
