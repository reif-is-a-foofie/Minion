"""
Optional small-model pass (local Ollama via ``llm.chat``, same stack as ingest_delight)
to recover praised writers and explicitly valued links from chat excerpts when regex misses nuance.

Environment (mirrors delight defaults):
  MINION_TASTE_MODEL      Model name; unset → MINION_DELIGHT_MODEL → ``qwen2.5:0.5b``.
                          ``off`` / ``none`` / ``0`` disables this pass entirely.
  MINION_TASTE_MAX_CHUNKS Cap on user chat chunks per bootstrap run (default 260).
  MINION_TASTE_BATCH_SIZE Chunks bundled per HTTP call to Ollama (default 8).
  MINION_TASTE_CHUNK_CHARS Truncate each chunk body (default 1500).
  MINION_TASTE_TIMEOUT    Seconds per batch (default 48).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("minion.corpus_pins_llm")


def _default_model() -> Optional[str]:
    raw = os.environ.get("MINION_TASTE_MODEL")
    if raw is not None:
        r = raw.strip()
        if not r or r.lower() in ("0", "off", "false", "none"):
            return None
        return r
    fallback = os.environ.get("MINION_DELIGHT_MODEL", "qwen2.5:0.5b").strip()
    if not fallback or fallback.lower() in ("0", "off", "false", "none"):
        return None
    return fallback


def taste_model() -> Optional[str]:
    """Active model slug, or None when disabled."""
    return _default_model()


def _batch_size() -> int:
    try:
        return max(2, min(16, int(os.environ.get("MINION_TASTE_BATCH_SIZE", "8"))))
    except ValueError:
        return 8


def _max_chunks() -> int:
    try:
        return max(40, min(8000, int(os.environ.get("MINION_TASTE_MAX_CHUNKS", "260"))))
    except ValueError:
        return 260


def _chunk_chars() -> int:
    try:
        return max(400, min(6000, int(os.environ.get("MINION_TASTE_CHUNK_CHARS", "1500"))))
    except ValueError:
        return 1500


def _timeout_s() -> float:
    try:
        return float(os.environ.get("MINION_TASTE_TIMEOUT", "48"))
    except ValueError:
        return 48.0


_CLEAN_URL = re.compile(r"https?://[^\s<>()\[\]\'\"]+", re.I)


def _extract_json_array(text: str) -> Optional[List[Any]]:
    t = text.strip()
    if not t:
        return None
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
        t = t.strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def _normalize_author(raw: Any) -> Optional[str]:
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip().strip("\"'”“")
    if len(s) < 3 or len(s) > 72:
        return None
    if not s[0].isupper():
        return None
    return s


SYSTEM_PROMPT = """You extract reading taste from chat excerpts. Output ONLY valid JSON — a JSON array with one object per input chunk id you could judge.

Each object MUST have exactly these keys:
  "chunk_id": string — copy the id exactly from the input bracket label.
  "praised_authors": string[] — proper names of writers/authors/poets or journalists the human clearly praises, prefers, cites as favorites, admires their writing-style, says they love/adore/admire — empty [] if none. Never invent names that are not clearly supported by that chunk.
  "endorsed_urls": string[] — http(s) URLs from that chunk text that the human clearly recommends, loves, bookmarks, calls a favorite read/article — MUST be literals copied from that chunk — empty [] if none.

Rules:
- If the chunk only paste-links without warmth, leave endorsed_urls empty unless they say they liked it.
- Names must appear as writers / authors — not celebrities mentioned in passing unless tied to reading/writing.
"""


def merge_llm_taste_into_counters(
    chat_user_chunks: List[Tuple[str, str]],
    *,
    links: Counter[str],
    author_hits: Counter[str],
    weight_author: int = 8,
    weight_url: int = 8,
) -> Dict[str, Any]:
    """Add LLM-judged authors and URLs into existing regex-driven counters."""
    model = taste_model()
    stats: Dict[str, Any] = {
        "enabled": bool(model),
        "model": model or "",
        "batches_ok": 0,
        "batches_failed": 0,
        "chunks_sent": 0,
        "authors_added": 0,
        "urls_added": 0,
    }
    if not model or not chat_user_chunks:
        return stats

    bs = _batch_size()
    cap = min(len(chat_user_chunks), _max_chunks())
    capped = sorted(chat_user_chunks, key=lambda x: -len(x[1]))[:cap]
    stats["chunks_sent"] = len(capped)

    mx = _chunk_chars()
    from llm import chat

    def _normalize_url(raw: str) -> str:
        from corpus_pins import normalize_url as nu

        return nu(raw)

    def _chunks(b: List[Tuple[str, str]]) -> str:
        parts: List[str] = []
        for cid, txt in b:
            body = (txt or "").strip().replace("\r\n", "\n")[:mx]
            parts.append(f"[{cid}]\n{body}")
        return "\n\n---CHUNK_BOUNDARY---\n\n".join(parts)

    for i in range(0, len(capped), bs):
        batch = capped[i : i + bs]
        user = (
            "Each block starts with [chunk_id] then the verbatim excerpt. "
            "Respond with one JSON array only.\n\n"
            + _chunks(batch)
        )
        temp = 0.05 + random.random() * 0.06
        try:
            resp = chat(
                model=model,
                system=SYSTEM_PROMPT,
                user=user,
                options={"temperature": temp},
                timeout_seconds=_timeout_s(),
            )
        except Exception as e:
            log.debug("taste LLM batch failed: %s", e)
            stats["batches_failed"] += 1
            continue

        arr = _extract_json_array(resp.content)
        if not arr:
            stats["batches_failed"] += 1
            continue
        stats["batches_ok"] += 1

        valid_ids = {cid for cid, _ in batch}
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            cid = obj.get("chunk_id")
            if not isinstance(cid, str) or cid not in valid_ids:
                continue
            for a in obj.get("praised_authors") or []:
                name = _normalize_author(a)
                if name:
                    author_hits[name] += weight_author
                    stats["authors_added"] += 1
            for u in obj.get("endorsed_urls") or []:
                if not isinstance(u, str):
                    continue
                for raw in _CLEAN_URL.findall(u):
                    nu = _normalize_url(raw)
                    if len(nu) > 12:
                        links[nu] += weight_url
                        stats["urls_added"] += 1

    return stats
