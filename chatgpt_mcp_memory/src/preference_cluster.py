"""Cluster chunk embeddings → preference_clusters + telemetry."""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import identity
import telemetry
from store import (
    iter_chunk_embedding_rows,
    preference_clusters_clear,
    preference_clusters_insert,
    transaction,
)

log = logging.getLogger("minion.preference_cluster")

_DEFAULT_K = 8
_MAX_SAMPLE = 2000
_KMEANS_ITERS = 25


def _cosine_kmeans(X: np.ndarray, k: int, *, seed: int = 42) -> np.ndarray:
    n, d = X.shape
    k = int(max(2, min(k, n)))
    rng = np.random.default_rng(seed)
    cent_idx = rng.choice(n, size=k, replace=False)
    centroids = X[cent_idx].copy()
    labels = np.zeros(n, dtype=np.int32)
    for _ in range(_KMEANS_ITERS):
        sim = X @ centroids.T
        labels = np.argmax(sim, axis=1)
        new_c = np.zeros_like(centroids)
        for j in range(k):
            mask = labels == j
            if not np.any(mask):
                new_c[j] = centroids[j]
            else:
                v = X[mask].mean(axis=0)
                nv = float(np.linalg.norm(v))
                new_c[j] = v / nv if nv > 0 else centroids[j]
        if np.allclose(new_c, centroids, atol=1e-4):
            break
        centroids = new_c
    return labels


def _heuristic_label(samples: List[str]) -> Tuple[str, str]:
    joined = " ".join(s[:200] for s in samples[:5] if s)
    joined = " ".join(joined.split())[:400]
    if not joined:
        return "Unlabeled cluster", "No text in cluster members."
    words = [w.strip(".,;:!?()[]\"'") for w in joined.lower().split()]
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "is", "are",
        "was", "were", "be", "been", "i", "you", "it", "that", "this", "with",
        "as", "at", "by", "from", "have", "has", "had", "not", "but", "so", "if",
    }
    freq: Dict[str, int] = {}
    for w in words:
        if len(w) < 3 or w in stop:
            continue
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda x: -x[1])[:3]
    label = ", ".join(t[0] for t in top) if top else "Mixed topics"
    summary = joined[:280] + ("…" if len(joined) > 280 else "")
    return label.title()[:80], summary


def _llm_label(samples: List[str], model: Optional[str] = None) -> Optional[Tuple[str, str]]:
    try:
        from llm import chat
    except Exception:
        return None
    m = model or os.environ.get("MINION_CLUSTER_MODEL", "mistral:7b")
    body = "\n---\n".join(s[:500] for s in samples[:6] if s)
    if not body.strip():
        return None
    try:
        r = chat(
            model=m,
            system=(
                "You label a cluster of user memory snippets. Reply with exactly two lines: "
                "Line1: short title (max 8 words). Line2: one-sentence summary of the theme."
            ),
            user=body,
            options={"temperature": 0.2, "num_predict": 120},
            timeout_seconds=45.0,
        )
        lines = [ln.strip() for ln in r.content.strip().split("\n") if ln.strip()]
        if len(lines) >= 2:
            return lines[0][:80], lines[1][:400]
        if lines:
            return lines[0][:80], lines[0][:400]
    except Exception as exc:
        log.debug("cluster llm label skipped: %s", exc)
    return None


def run_preference_clustering(
    conn: sqlite3.Connection,
    *,
    sample_limit: int = _MAX_SAMPLE,
    k: int = _DEFAULT_K,
    use_llm: bool = True,
    cluster_auto_propose: bool = False,
) -> Dict[str, Any]:
    rows = iter_chunk_embedding_rows(conn, limit=min(sample_limit, _MAX_SAMPLE))
    if len(rows) < k * 3:
        return {
            "status": "skipped",
            "reason": f"not enough embedded chunks (have {len(rows)}, need ~{k * 3})",
            "clusters_written": 0,
        }

    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    X = np.stack([r[2] for r in rows], axis=0)
    X = X.astype(np.float32, copy=False)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    labels = _cosine_kmeans(X, k)
    run_at = time.time()
    clusters_written = 0

    with transaction(conn):
        preference_clusters_clear(conn)
        for j in range(int(labels.max()) + 1):
            mask = labels == j
            member_ids = [ids[i] for i in range(len(ids)) if mask[i]]
            member_texts = [texts[i] for i in range(len(texts)) if mask[i]]
            if use_llm:
                llm_out = _llm_label(member_texts)
                if llm_out:
                    label_s, summary_s = llm_out
                else:
                    label_s, summary_s = _heuristic_label(member_texts)
            else:
                label_s, summary_s = _heuristic_label(member_texts)
            cid = "pcl-" + secrets.token_hex(8)
            preference_clusters_insert(
                conn,
                cluster_id=cid,
                label=label_s,
                summary=summary_s,
                member_chunk_ids=member_ids[:200],
                run_at=run_at,
            )
            clusters_written += 1

    telemetry.log_event(
        "preference_cluster",
        clusters=clusters_written,
        chunks_sampled=len(rows),
        k=k,
    )
    env_flag = (os.environ.get("MINION_CLUSTER_AUTO_PROPOSE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if (env_flag or cluster_auto_propose) and clusters_written:
        try:
            ap = identity.auto_propose_from_clusters(conn, run_at)
            telemetry.log_event("cluster_auto_propose", **ap)
        except Exception:
            log.exception("cluster auto-propose failed")
    return {
        "status": "ok",
        "clusters_written": clusters_written,
        "chunks_sampled": len(rows),
        "run_at": run_at,
    }
