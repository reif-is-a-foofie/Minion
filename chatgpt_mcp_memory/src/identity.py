"""Digital identity graph: validation + orchestration over store tables."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sqlite3

from store import (
    get_chunk,
    identity_claim_get,
    identity_claim_insert,
    identity_claim_list,
    identity_claim_mirror_history,
    identity_claim_patch_fields,
    identity_claim_set_status,
    identity_edge_insert,
    identity_edges_for_claim,
    preference_clusters_list,
    transaction,
)

CLAIM_KINDS = frozenset(
    {"preference", "value", "relationship", "goal", "boundary", "fact"}
)
CLAIM_STATUSES = frozenset({"proposed", "active", "rejected", "superseded"})

_MAX_CLAIM_TEXT = 4000
_MIN_CLAIM_TEXT = 3
_MAX_RATIONALE = 1200
_MAX_EVIDENCE_CHUNKS = 12


def new_claim_id() -> str:
    return "icl-" + secrets.token_hex(8)


def new_edge_id() -> str:
    return "ied-" + secrets.token_hex(8)


def validate_kind(kind: str) -> Optional[str]:
    k = (kind or "").strip().lower()
    if k not in CLAIM_KINDS:
        return f"kind must be one of: {sorted(CLAIM_KINDS)}"
    return None


def validate_text(text: str) -> Optional[str]:
    t = (text or "").strip()
    if len(t) < _MIN_CLAIM_TEXT:
        return f"text too short (min {_MIN_CLAIM_TEXT} chars)"
    if len(t) > _MAX_CLAIM_TEXT:
        return f"text too long (max {_MAX_CLAIM_TEXT} chars)"
    return None


def propose_identity_update(
    conn: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    source_agent: Optional[str] = None,
    confidence: Optional[float] = None,
    evidence_chunk_ids: Optional[Sequence[str]] = None,
    evidence_rationales: Optional[Sequence[Optional[str]]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    err = validate_kind(kind)
    if err:
        return None, err
    err = validate_text(text)
    if err:
        return None, err
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        return None, "confidence must be between 0 and 1 when set"

    chunk_ids = list(evidence_chunk_ids or [])[:_MAX_EVIDENCE_CHUNKS]
    rationales = list(evidence_rationales or [])
    if len(rationales) > len(chunk_ids):
        rationales = rationales[: len(chunk_ids)]
    while len(rationales) < len(chunk_ids):
        rationales.append(None)

    agent = (source_agent or "").strip() or None
    claim_id = new_claim_id()
    now = time.time()

    try:
        with transaction(conn):
            identity_claim_insert(
                conn,
                claim_id=claim_id,
                kind=kind.strip().lower(),
                text=text.strip(),
                status="proposed",
                confidence=float(confidence) if confidence is not None else None,
                source_agent=agent,
                meta={**(meta or {}), "proposed_at": now},
            )
            for cid, rat in zip(chunk_ids, rationales):
                row = get_chunk(conn, cid)
                if row is None:
                    continue
                rtext = (rat or "").strip() if rat else None
                if rtext and len(rtext) > _MAX_RATIONALE:
                    rtext = rtext[: _MAX_RATIONALE - 1] + "…"
                identity_edge_insert(
                    conn,
                    edge_id=new_edge_id(),
                    claim_id=claim_id,
                    chunk_id=cid,
                    source_id=row.get("source_id"),
                    rationale=rtext,
                )
    except sqlite3.IntegrityError as e:
        return None, str(e)

    claim = identity_claim_get(conn, claim_id)
    edges = identity_edges_for_claim(conn, claim_id)
    return {"claim": claim, "edges": edges, "claim_id": claim_id}, None


def list_claims(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if status and status not in CLAIM_STATUSES:
        return None, f"status must be one of: {sorted(CLAIM_STATUSES)}"
    if kind:
        err = validate_kind(kind)
        if err:
            return None, err
    rows = identity_claim_list(
        conn, status=status, kind=kind, limit=min(limit, 500)
    )
    return rows, None


def set_claim_status(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    status: str,
    superseded_by: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    if status not in CLAIM_STATUSES:
        return False, f"status must be one of: {sorted(CLAIM_STATUSES)}"
    ok = identity_claim_set_status(
        conn, claim_id, status=status, superseded_by=superseded_by
    )
    if not ok:
        return False, "claim_id not found"
    return True, None


def patch_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    status: Optional[str] = None,
    superseded_by: Optional[str] = None,
    text: Optional[str] = None,
    meta_merge: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Partial update: pass only fields to change. At least one field required."""
    if status is not None and status not in CLAIM_STATUSES:
        return None, f"status must be one of: {sorted(CLAIM_STATUSES)}"
    if text is not None:
        err = validate_text(text)
        if err:
            return None, err
    if all(x is None for x in (status, superseded_by, text, meta_merge)):
        return None, "at least one of status, superseded_by, text, meta required"
    if identity_claim_get(conn, claim_id) is None:
        return None, "claim_id not found"
    ok = identity_claim_patch_fields(
        conn,
        claim_id,
        text=text,
        status=status,
        superseded_by=superseded_by,
        meta_merge=meta_merge,
    )
    if not ok:
        return None, "claim_id not found"
    return identity_claim_get(conn, claim_id), None


def build_identity_summary(
    conn: sqlite3.Connection,
    *,
    max_claims: int = 40,
    max_clusters: int = 8,
    history_tail: int = 0,
) -> str:
    active = identity_claim_list(conn, status="active", limit=max_claims)
    proposed = identity_claim_list(conn, status="proposed", limit=min(20, max_claims))
    clusters = preference_clusters_list(conn)[:max_clusters]

    lines: List[str] = ["## Identity snapshot (Minion)"]
    if active:
        lines.append("### Active claims")
        for c in active:
            meta = c.get("meta") or {}
            rel = meta.get("relation")
            labels = meta.get("labels")
            suffix = ""
            if rel:
                suffix += f" _(relation: {rel})_"
            if isinstance(labels, list) and labels:
                lab = ", ".join(str(x) for x in labels[:12])
                suffix += f" `{lab}`"
            lines.append(f"- **{c['kind']}**: {c['text']}{suffix}")
    else:
        lines.append("### Active claims\n- _(none yet)_")

    if proposed:
        lines.append("### Pending proposals (need user review)")
        for c in proposed:
            who = f" — _via {c['source_agent']}_" if c.get("source_agent") else ""
            meta = c.get("meta") or {}
            rel = meta.get("relation")
            labels = meta.get("labels")
            sfx = ""
            if rel:
                sfx += f" _(relation: {rel})_"
            if isinstance(labels, list) and labels:
                sfx += f" `{', '.join(str(x) for x in labels[:12])}`"
            lines.append(f"- **{c['kind']}** (`{c['claim_id']}`){who}: {c['text']}{sfx}")

    if clusters:
        lines.append("### Preference clusters (derived)")
        seen_run: Optional[float] = None
        for cl in clusters:
            if seen_run is None:
                seen_run = cl["run_at"]
            if cl["run_at"] != seen_run:
                break
            lines.append(f"- **{cl['label']}**: {cl['summary']}")

    ht = int(max(0, min(history_tail, 500)))
    if ht > 0:
        hist = identity_claim_mirror_history(conn, limit=ht)
        if hist:
            lines.append("### Recent revisions (superseded / rejected)")
            for c in hist:
                meta = c.get("meta") or {}
                rs = meta.get("revision_source") or "unknown"
                who = f" — _source: {rs}_"
                sup = f" → `{c['superseded_by']}`" if c.get("superseded_by") else ""
                excerpt = c["text"][:280] + ("…" if len(c["text"]) > 280 else "")
                lines.append(
                    f"- **{c['kind']}** ({c['status']}){who}{sup}: {excerpt}"
                )

    return "\n".join(lines) + "\n"


def auto_propose_from_clusters(conn: sqlite3.Connection, run_at: float) -> Dict[str, Any]:
    """Draft proposed claims from one clustering run (dedup via meta.cluster_auto_key)."""
    rows = [
        r
        for r in preference_clusters_list(conn)
        if abs(float(r["run_at"]) - float(run_at)) < 1e-6
    ]
    proposed_n = 0
    skipped_n = 0
    for cl in rows:
        cid = str(cl["cluster_id"])
        key = f"{run_at}:{cid}"
        hit = conn.execute(
            "SELECT 1 FROM identity_claims "
            "WHERE json_extract(meta_json, '$.cluster_auto_key') = ? LIMIT 1",
            (key,),
        ).fetchone()
        if hit:
            skipped_n += 1
            continue
        members = list(cl.get("member_chunk_ids") or [])[:_MAX_EVIDENCE_CHUNKS]
        text = (cl.get("summary") or cl.get("label") or "").strip()
        if len(text) < _MIN_CLAIM_TEXT:
            skipped_n += 1
            continue
        payload, err = propose_identity_update(
            conn,
            kind="preference",
            text=text,
            source_agent="cluster_auto",
            confidence=0.35,
            evidence_chunk_ids=members,
            meta={"cluster_auto_key": key, "cluster_id": cid, "run_at": run_at},
        )
        if err or not payload:
            skipped_n += 1
        else:
            proposed_n += 1
    return {"proposed": proposed_n, "skipped": skipped_n, "clusters": len(rows)}


def export_identity_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    claims = identity_claim_list(conn, limit=5000)
    edges_all: List[Dict[str, Any]] = []
    for c in claims:
        edges_all.extend(identity_edges_for_claim(conn, c["claim_id"]))
    clusters = preference_clusters_list(conn)
    return {
        "version": 1,
        "exported_at": time.time(),
        "claims": claims,
        "edges": edges_all,
        "preference_clusters": clusters,
    }


def snapshot_manifest_hash(snapshot: Dict[str, Any]) -> str:
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
