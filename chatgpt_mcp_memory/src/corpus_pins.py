"""
Harvest explicit taste anchors from the indexed corpus: URLs, author bylines,
long substantive user prose, and — in ChatGPT exports — conversational praise
("I love X's writing"), affection around links, and quoted snippets the user liked.

Pins with meta_json.origin == "bootstrap" are replaced on each bootstrap run;
user-added pins should use another origin or omit the field.

Optional: ``corpus_pins_llm`` batches chat user chunks through the same local Ollama path as
``ingest_delight`` (see MINION_TASTE_MODEL / MINION_DELIGHT_MODEL).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from store import DB_FILENAME, connect, transaction

log = logging.getLogger("minion.corpus_pins")

URL_RE = re.compile(
    r"https?://[^\s<>()\[\]\"']{4,}[^\s<>()\[\]\"']*",
    re.I,
)
# Essay/blog byline
BYLINE_RE = re.compile(
    r"(?m)^By\s+([A-Z][a-z]+(?:\s+(?:de|von|van|Da|La|Le)\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\s*$"
)
# Signature dash before name at end of a block (short line)
SIGNATURE_RE = re.compile(
    r"(?:—|–)\s*([A-Z][a-z]+\s+[A-Z][a-z]+)\s*$",
    re.M,
)
HAPPY_RE = re.compile(
    r"\b(?:final(?:ly)?|published|essay|chapter|piece|draft|happy with|pleased with|"
    r"polished|submit(?:ted)?|my best work|I'm proud|we shipped)\b",
    re.I,
)

# Conversational praise in ChatGPT threads (user said they liked someone's writing).
CHAT_AUTHOR_PRAISE_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:^|[.!?\n])\s*(?:I\s+)?(?:really\s+|particularly\s+)?(?:love|loved|adore|admire)\s+"
        r"(?:reading\s+)?(?:the\s+)?(?:writing|prose|style|voice|work|essays)\s+(?:of|from)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){0,4})\b",
        re.I | re.M,
    ),
    re.compile(
        r"\b(?:love|loved|adore)\s+(?:reading\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){0,4})\b"
        r"(?:'s|\s+)(?:writing|prose|essays|fiction|novels)\b",
        re.I,
    ),
    re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){0,4})'s\s+(?:writing|prose|style|voice|essays)\s+"
        r"(?:is|are|was|were)\s+(?:just\s+)?(?:incredible|beautiful|stunning|perfect|gorgeous|chef)",
        re.I,
    ),
    re.compile(
        r"\b(?:favorite|favourite)\s+(?:living\s+)?(?:writer|author|essayist)\s*[:(]?\s*"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){0,4})\b",
        re.I,
    ),
    re.compile(
        r"\bwriters?\s+(?:I\s+)?(?:return to|come back to|always recommend)\s+(?:include\s+)?"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){0,4})\b",
        re.I,
    ),
)

# Chunk has a URL and language that signals the user valued that link.
CHAT_LINK_AFFECTION_RE = re.compile(
    r"\b(?:"
    r"I\s+(?:really\s+)?(?:love|loved)\s+this\s+(?:link|article|post|piece|essay)|"
    r"favorite\s+(?:link|article|post|read)|favourite\s+(?:link|article)|"
    r"(?:really\s+)?(?:love|adore)\s+this\s+(?:link|article|piece)|"
    r"saved\s+this\s+link|bookmark(?:ed)?\s+this|"
    r"(?:can't|cannot)\s+recommend\s+(?:this\s+)?(?:enough|highly)|"
    r"(?:chef'?s\s+kiss|gem of (?:an )?article)|"
    r"(?:such|so)\s+(?:a\s+)?(?:good|great)\s+(?:read|piece|find)|"
    r"beautifully\s+written"
    r")\b",
    re.I,
)

# Short user blurbs with a link + warmth (emoji or exclamation cluster).
CHAT_SHORT_WARMTH_RE = re.compile(
    r"(?:❤|💯|🔥|🙌|!!{1,}|so good|incredible read|must-read)",
    re.I,
)

_BAD_AUTHOR_TOKENS = frozenset(
    {
        "The",
        "This",
        "That",
        "Reading",
        "Writing",
        "Being",
        "Having",
        "When",
        "What",
        "Your",
        "Their",
        "Some",
        "Any",
        "Just",
        "Also",
        "Here",
        "How",
        "Why",
        "Chatgpt",
        "Openai",
        "Claude",
        "Assistant",
    }
)


def _clean_author_name(s: str) -> Optional[str]:
    s = s.strip().strip(" '\"“”")
    if len(s) < 4 or len(s) > 72:
        return None
    parts = s.split()
    if parts[0] in _BAD_AUTHOR_TOKENS:
        return None
    if not parts[0][0].isupper():
        return None
    return s


def extract_chat_author_praises(text: str) -> List[str]:
    """Names the user (or thread) explicitly praised as writers."""
    out: List[str] = []
    for rx in CHAT_AUTHOR_PRAISE_RES:
        for m in rx.finditer(text):
            raw = m.group(1) if m.lastindex else None
            if not raw:
                continue
            name = _clean_author_name(raw)
            if name:
                out.append(name)
    return out


def urls_signaled_as_loved(text: str) -> List[str]:
    """URLs in a chunk where the text also praises or recommends that link/read."""
    urls = [normalize_url(u) for u in URL_RE.findall(text)]
    urls = [u for u in urls if len(u) > 12]
    if not urls:
        return []
    if CHAT_LINK_AFFECTION_RE.search(text):
        return urls
    if len(text) < 1100 and len(urls) <= 4 and CHAT_SHORT_WARMTH_RE.search(text):
        return urls
    return []


def _chat_snippet_boost(text: str, role: Optional[str], source_kind: str) -> float:
    """User chunks with pasted quotes/blockquotes plus enthusiasm read as valued snippets."""
    if source_kind != "chatgpt-export" or role != "user":
        return 0.0
    if len(text) < 220:
        return 0.0
    if text.count("\n") < 3:
        return 0.0
    quote_density = text.count("\n>") + text.count("\n\n>") + text.count('"""')
    if quote_density < 2:
        return 0.0
    if not re.search(
        r"\b(?:beautiful|perfect|gorgeous|this\s+paragraph|saved\s+this|yes\s+yes|chef)\b",
        text,
        re.I,
    ):
        return 0.0
    return 0.15

MIN_USER_PROSE_CHARS = 420
MIN_DOC_PROSE_CHARS = 480
MAX_CHUNK_PINS = 350
MAX_LINK_PINS = 4000
MAX_AUTHOR_PINS = 120
MAX_CHUNK_PINS_PER_SHARED = 160
SHARED_LINK_BASE_W = 1.22
SHARED_WEIGHT_BOOST = 1.28


def _pin_id(kind: str, ref: str) -> str:
    h = hashlib.sha256(f"{kind}\n{ref}".encode("utf-8")).hexdigest()[:22]
    return f"pin-{h}"


def normalize_url(raw: str) -> str:
    u = raw.strip()
    while u and u[-1] in "),.;:\"'»]}]":
        u = u[:-1]
    return u[:2048]


def _prose_quality_document(text: str) -> float:
    """Long passages from dropped files (md/pdf/text), any role."""
    n = len(text)
    if n < MIN_DOC_PROSE_CHARS:
        return 0.0
    urls = URL_RE.findall(text)
    if urls and sum(len(x) for x in urls) > n * 0.24:
        return 0.0
    fence = text.count("```")
    if fence >= 8 and n < 4000:
        return 0.12
    score = min(n / 8500.0, 1.0) * 0.5
    if n >= 1600:
        score += 0.2
    if fence >= 2:
        score -= 0.1
    return max(0.0, min(score, 1.2))


def _prose_quality(text: str) -> float:
    """Higher = more likely durable user writing (not link dumps or stubs)."""
    n = len(text)
    if n < MIN_USER_PROSE_CHARS:
        return 0.0
    urls = URL_RE.findall(text)
    if urls:
        est = sum(len(x) for x in urls)
        if est > n * 0.22:
            return 0.0
    fence = text.count("```")
    if fence >= 6 and n < 3000:
        return 0.15
    score = min(n / 7500.0, 1.0) * 0.55
    if HAPPY_RE.search(text):
        score += 0.28
    if n >= 1400:
        score += 0.18
    if fence >= 2:
        score -= 0.12
    return max(0.0, min(score, 1.35))


def _clear_bootstrap_pins(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM corpus_pins WHERE json_extract(meta_json, '$.origin') = ?",
        ("bootstrap",),
    )
    return int(cur.rowcount)


def _insert_pin(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    weight: float,
    reason: str,
    meta: Dict[str, Any],
) -> None:
    pid = _pin_id(kind, ref)
    meta.setdefault("origin", "bootstrap")
    conn.execute(
        "INSERT INTO corpus_pins(pin_id, kind, ref, weight, reason, meta_json, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            pid,
            kind,
            ref,
            float(weight),
            reason,
            json.dumps(meta, ensure_ascii=False),
            time.time(),
        ),
    )


def _upsert_pin_max_weight(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    weight: float,
    reason: str,
    meta: Dict[str, Any],
) -> None:
    """Pin or raise weight if an existing pin is weaker (shared vs bootstrap)."""
    pid = _pin_id(kind, ref)
    conn.execute(
        "INSERT INTO corpus_pins(pin_id, kind, ref, weight, reason, meta_json, created_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(kind, ref) DO UPDATE SET "
        "weight = MAX(weight, excluded.weight), "
        "reason = CASE WHEN excluded.weight > weight THEN excluded.reason ELSE reason END, "
        "meta_json = CASE WHEN excluded.weight > weight THEN excluded.meta_json ELSE meta_json END",
        (
            pid,
            kind,
            ref,
            float(weight),
            reason,
            json.dumps(meta, ensure_ascii=False),
            time.time(),
        ),
    )


def _prose_score_for_row(
    role: Optional[str],
    source_kind: str,
    text: str,
    *,
    shared_source: bool,
) -> float:
    if shared_source:
        if role == "user":
            return _prose_quality(text)
        if source_kind == "chatgpt-export":
            return _prose_quality(text)
        return _prose_quality_document(text)
    if role == "user":
        return _prose_quality(text)
    return 0.0


def iter_chunks_with_sources(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind FROM chunks c "
        "JOIN sources s ON s.source_id = c.source_id"
    ).fetchall()


def extract_author_guesses(text: str) -> List[str]:
    out: List[str] = []
    for m in BYLINE_RE.finditer(text):
        out.append(m.group(1).strip())
    for m in SIGNATURE_RE.finditer(text):
        line = m.group(1).strip()
        if len(line.split()) <= 5:
            out.append(line)
    return out


def bootstrap_corpus_pins(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Scan all chunks; replace prior bootstrap pins. Idempotent for user pins.
    Returns summary counts.
    """
    links: Counter[str] = Counter()
    author_hits: Counter[str] = Counter()
    chunk_candidates: List[Tuple[float, str, Dict[str, Any]]] = []
    llm_chat_user_chunks: List[Tuple[str, str]] = []

    rows = iter_chunks_with_sources(conn)
    for r in rows:
        text = r["text"] or ""
        cid = str(r["chunk_id"])
        path = str(r["path"])
        kind = str(r["kind"])
        role = r["role"]
        meta = json.loads(r["meta_json"] or "{}")

        for raw in URL_RE.findall(text):
            u = normalize_url(raw)
            if len(u) > 12:
                links[u] += 1

        for name in extract_author_guesses(text):
            if 6 <= len(name) <= 80:
                author_hits[name] += 1

        if kind == "chatgpt-export":
            for name in extract_chat_author_praises(text):
                author_hits[name] += 6
            for u in urls_signaled_as_loved(text):
                links[u] += 5

        if kind == "chatgpt-export" and role == "user" and len(text.strip()) > 55:
            llm_chat_user_chunks.append((cid, text))

        q = _prose_score_for_row(role, kind, text, shared_source=False)
        if kind == "chatgpt-export":
            q += _chat_snippet_boost(text, role, kind)
        if q > 0.08:
            preview = text[:420].replace("\n", " ") + ("…" if len(text) > 420 else "")
            chunk_candidates.append(
                (
                    q,
                    cid,
                    {
                        "origin": "bootstrap",
                        "chunk_id": cid,
                        "source_kind": kind,
                        "path": path,
                        "preview": preview,
                        "chars": len(text),
                        "conversation_title": meta.get("conversation_title"),
                    },
                )
            )

    chunk_candidates.sort(key=lambda x: -x[0])
    top_chunks = chunk_candidates[:MAX_CHUNK_PINS]

    llm_taste_stats: Dict[str, Any] = {}
    try:
        from corpus_pins_llm import merge_llm_taste_into_counters

        llm_taste_stats = merge_llm_taste_into_counters(
            llm_chat_user_chunks,
            links=links,
            author_hits=author_hits,
        )
    except Exception:
        llm_taste_stats = {"enabled": False, "error": True}

    top_links = [u for u, _ in links.most_common(MAX_LINK_PINS)]
    top_authors = [n for n, _ in author_hits.most_common(MAX_AUTHOR_PINS)]

    with transaction(conn):
        removed = _clear_bootstrap_pins(conn)
        inserted = {"link": 0, "chunk": 0, "author_guess": 0}

        for url in top_links:
            w = min(1.0 + 0.08 * min(links[url], 12), 2.2)
            try:
                _insert_pin(
                    conn,
                    kind="link",
                    ref=url,
                    weight=w,
                    reason="bootstrap:url_frequency",
                    meta={
                        "origin": "bootstrap",
                        "occurrences": links[url],
                    },
                )
                inserted["link"] += 1
            except sqlite3.IntegrityError:
                pass

        for score, cid, meta in top_chunks:
            try:
                _insert_pin(
                    conn,
                    kind="chunk",
                    ref=cid,
                    weight=round(0.85 + float(score) * 0.35, 4),
                    reason="bootstrap:user_prose",
                    meta=meta,
                )
                inserted["chunk"] += 1
            except sqlite3.IntegrityError:
                pass

        for name in top_authors:
            try:
                _insert_pin(
                    conn,
                    kind="author_guess",
                    ref=name,
                    weight=min(1.0 + 0.05 * author_hits[name], 2.0),
                    reason="bootstrap:byline_or_signature",
                    meta={
                        "origin": "bootstrap",
                        "occurrences": author_hits[name],
                    },
                )
                inserted["author_guess"] += 1
            except sqlite3.IntegrityError:
                pass

    out: Dict[str, Any] = {
        "removed_bootstrap_rows": removed,
        "chunks_scanned": len(rows),
        "pins_inserted": inserted,
        "unique_urls_seen": len(links),
        "author_names_seen": len(author_hits),
    }
    if llm_taste_stats:
        out["llm_taste"] = llm_taste_stats
    return out


def iter_chunks_for_source(conn: sqlite3.Connection, source_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE c.source_id=?",
        (source_id,),
    ).fetchall()


def harvest_pins_for_shared_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    path_hint: str = "",
) -> Dict[str, Any]:
    """Same extraction as corpus bootstrap, scoped to one source + stronger weights."""
    rows = iter_chunks_for_source(conn, source_id)
    if not rows:
        return {"source_id": source_id, "pins_upserted": {}, "chunks_scanned": 0}
    path0 = path_hint or str(rows[0]["path"])
    links: Counter[str] = Counter()
    author_hits: Counter[str] = Counter()
    chunk_scored: List[Tuple[float, str, Dict[str, Any]]] = []
    llm_user_chunks: List[Tuple[str, str]] = []

    for r in rows:
        text = r["text"] or ""
        cid = str(r["chunk_id"])
        sk = str(r["kind"])
        path = str(r["path"])
        role = r["role"]
        meta = json.loads(r["meta_json"] or "{}")

        if sk == "chatgpt-export" and role == "user" and len(text.strip()) > 55:
            llm_user_chunks.append((cid, text))

        for raw in URL_RE.findall(text):
            u = normalize_url(raw)
            if len(u) > 12:
                links[u] += 1
        for name in extract_author_guesses(text):
            if 6 <= len(name) <= 80:
                author_hits[name] += 1
        if sk == "chatgpt-export":
            for name in extract_chat_author_praises(text):
                author_hits[name] += 6
            for u in urls_signaled_as_loved(text):
                links[u] += 5
        q = _prose_score_for_row(role, sk, text, shared_source=True)
        if sk == "chatgpt-export":
            q += _chat_snippet_boost(text, role, sk)
        if q > 0.07:
            preview = text[:420].replace("\n", " ") + ("…" if len(text) > 420 else "")
            chunk_scored.append(
                (
                    q,
                    cid,
                    {
                        "origin": "shared",
                        "chunk_id": cid,
                        "source_id": source_id,
                        "source_kind": sk,
                        "path": path,
                        "preview": preview,
                        "chars": len(text),
                        "conversation_title": meta.get("conversation_title"),
                        "ingest_provenance": "shared",
                    },
                )
            )

    llm_taste_stats: Dict[str, Any] = {}
    try:
        from corpus_pins_llm import merge_llm_taste_into_counters

        llm_taste_stats = merge_llm_taste_into_counters(
            llm_user_chunks,
            links=links,
            author_hits=author_hits,
        )
    except Exception:
        llm_taste_stats = {"enabled": False, "error": True}

    chunk_scored.sort(key=lambda x: -x[0])
    top_chunks = chunk_scored[:MAX_CHUNK_PINS_PER_SHARED]
    ins = {"link": 0, "chunk": 0, "author_guess": 0}

    with transaction(conn):
        for url, cnt in links.most_common(220):
            w = min(SHARED_LINK_BASE_W + 0.065 * min(cnt, 10) * SHARED_WEIGHT_BOOST, 2.65)
            _upsert_pin_max_weight(
                conn,
                kind="link",
                ref=url,
                weight=w,
                reason="shared_file:url",
                meta={
                    "origin": "shared",
                    "occurrences": cnt,
                    "source_id": source_id,
                    "path": path0,
                },
            )
            ins["link"] += 1
        for score, cid, meta in top_chunks:
            w = round((0.92 + float(score) * 0.42) * SHARED_WEIGHT_BOOST, 4)
            _upsert_pin_max_weight(
                conn,
                kind="chunk",
                ref=cid,
                weight=w,
                reason="shared_file:prose",
                meta=meta,
            )
            ins["chunk"] += 1
        for name, cnt in author_hits.most_common(55):
            w = min(1.08 + 0.085 * min(cnt, 8) * SHARED_WEIGHT_BOOST, 2.15)
            _upsert_pin_max_weight(
                conn,
                kind="author_guess",
                ref=name,
                weight=w,
                reason="shared_file:byline",
                meta={
                    "origin": "shared",
                    "occurrences": cnt,
                    "source_id": source_id,
                    "path": path0,
                },
            )
            ins["author_guess"] += 1

    out: Dict[str, Any] = {
        "source_id": source_id,
        "path": path0,
        "pins_upserted": ins,
        "chunks_scanned": len(rows),
    }
    if llm_taste_stats:
        out["llm_taste"] = llm_taste_stats
    return out


def list_corpus_pins(conn: sqlite3.Connection, *, limit: int = 500) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT pin_id, kind, ref, weight, reason, meta_json, created_at "
        "FROM corpus_pins ORDER BY weight DESC, created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "pin_id": r["pin_id"],
                "kind": r["kind"],
                "ref": r["ref"],
                "weight": float(r["weight"]),
                "reason": r["reason"],
                "meta": json.loads(r["meta_json"] or "{}"),
                "created_at": float(r["created_at"]),
            }
        )
    return out


def _default_db_path() -> Path:
    env = os.environ.get("MINION_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve() / DB_FILENAME
    here = Path(__file__).resolve()
    return here.parents[1] / "data" / "derived" / DB_FILENAME


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Harvest corpus_pins from memory.db")
    ap.add_argument("--db", type=Path, help="Path to memory.db")
    ap.add_argument("--list", action="store_true", help="List pins and exit")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve() if args.db else _default_db_path()
    if not db_path.is_file():
        log.error("Database not found: %s", db_path)
        return 1
    conn = connect(db_path)
    if args.list:
        for p in list_corpus_pins(conn, limit=args.limit):
            print(json.dumps(p, ensure_ascii=False))
        return 0
    summary = bootstrap_corpus_pins(conn)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
