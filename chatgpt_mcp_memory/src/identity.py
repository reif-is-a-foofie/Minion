"""Digital identity graph: validation + orchestration over store tables."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import sqlite3

import identity_layers
from store import (
    get_chunk,
    identity_access_log_insert,
    identity_access_log_list,
    identity_claim_get,
    identity_claim_insert,
    identity_claim_list,
    identity_claim_set_status,
    identity_edge_insert,
    identity_edges_for_claim,
    preference_clusters_list,
    transaction,
)

CLAIM_KINDS = frozenset(
    {
        "preference",
        "value",
        "relationship",
        "goal",
        "boundary",
        "fact",
        "pattern",
        "sensitive",
    }
)
CLAIM_STATUSES = frozenset(
    {"proposed", "active", "rejected", "superseded", "stale", "archived"}
)

_MAX_CLAIM_TEXT = 4000
_MIN_CLAIM_TEXT = 3
_MAX_RATIONALE = 1200
_MAX_EVIDENCE_CHUNKS = 12


def new_claim_id() -> str:
    return "icl-" + secrets.token_hex(8)


def new_edge_id() -> str:
    return "ied-" + secrets.token_hex(8)


def new_access_log_id() -> str:
    return "ial-" + secrets.token_hex(8)


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
    layer: Optional[int] = None,
    field: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    err = validate_kind(kind)
    if err:
        return None, err
    err = validate_text(text)
    if err:
        return None, err
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        return None, "confidence must be between 0 and 1 when set"

    k = kind.strip().lower()
    inferred = identity_layers.KIND_DEFAULT_LAYER.get(k, 3)
    resolved_layer = int(layer) if layer is not None else inferred
    if resolved_layer < identity_layers.MIN_LAYER or resolved_layer > identity_layers.MAX_LAYER:
        return None, f"layer must be {identity_layers.MIN_LAYER}..{identity_layers.MAX_LAYER}"
    if resolved_layer == 7:
        m = meta or {}
        if not bool(m.get("explicit_declaration")):
            return None, "layer 7 requires meta.explicit_declaration true (no inference)"
    ferr = identity_layers.validate_field_for_layer(resolved_layer, field)
    if ferr:
        return None, ferr

    chunk_ids = list(evidence_chunk_ids or [])[:_MAX_EVIDENCE_CHUNKS]
    rationales = list(evidence_rationales or [])
    if len(rationales) > len(chunk_ids):
        rationales = rationales[: len(chunk_ids)]
    while len(rationales) < len(chunk_ids):
        rationales.append(None)

    agent = (source_agent or "").strip() or None
    claim_id = new_claim_id()
    now = time.time()
    field_norm = (field or "").strip().lower() or None

    try:
        with transaction(conn):
            identity_claim_insert(
                conn,
                claim_id=claim_id,
                kind=k,
                text=text.strip(),
                status="proposed",
                confidence=float(confidence) if confidence is not None else None,
                source_agent=agent,
                meta={**(meta or {}), "proposed_at": now},
                layer=resolved_layer,
                field=field_norm,
                last_reinforced_at=None,
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
    layer: Optional[int] = None,
    limit: int = 100,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if status and status not in CLAIM_STATUSES:
        return None, f"status must be one of: {sorted(CLAIM_STATUSES)}"
    if kind:
        err = validate_kind(kind)
        if err:
            return None, err
    if layer is not None and (
        layer < identity_layers.MIN_LAYER or layer > identity_layers.MAX_LAYER
    ):
        return None, f"layer must be {identity_layers.MIN_LAYER}..{identity_layers.MAX_LAYER}"
    rows = identity_claim_list(
        conn, status=status, kind=kind, layer=layer, limit=min(limit, 500)
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


def build_identity_summary(
    conn: sqlite3.Connection,
    *,
    max_claims: int = 40,
    max_clusters: int = 8,
) -> str:
    active = identity_claim_list(conn, status="active", limit=max_claims)
    proposed = identity_claim_list(conn, status="proposed", limit=min(20, max_claims))
    clusters = preference_clusters_list(conn)[:max_clusters]

    lines: List[str] = ["## Identity snapshot (Minion)"]
    if active:
        lines.append("### Active claims (by ISA layer)")
        by_layer: Dict[int, List[Dict[str, Any]]] = {}
        for c in active:
            L = identity_layers.effective_layer(c)
            by_layer.setdefault(L, []).append(c)
        for L in sorted(by_layer.keys()):
            title = identity_layers.LAYER_TITLES.get(L, f"Layer {L}")
            lines.append(f"#### L{L} — {title}")
            for c in by_layer[L]:
                fld = f" `{c['field']}`" if c.get("field") else ""
                ev = (
                    " _(evidence withdrawn — review)_"
                    if (c.get("meta") or {}).get("evidence_withdrawn")
                    else ""
                )
                lines.append(f"- **{c['kind']}**{fld}: {c['text']}{ev}")
    else:
        lines.append("### Active claims\n- _(none yet)_")

    if proposed:
        lines.append("### Pending proposals (need user review)")
        for c in proposed:
            who = f" — _via {c['source_agent']}_" if c.get("source_agent") else ""
            L = identity_layers.effective_layer(c)
            lines.append(
                f"- L{L} **{c['kind']}** (`{c['claim_id']}`){who}: {c['text']}"
            )

    if clusters:
        lines.append("### Preference clusters (derived)")
        seen_run: Optional[float] = None
        for cl in clusters:
            if seen_run is None:
                seen_run = cl["run_at"]
            if cl["run_at"] != seen_run:
                break
            lines.append(f"- **{cl['label']}**: {cl['summary']}")

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
            layer=6,
            field=None,
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
        "version": 2,
        "exported_at": time.time(),
        "claims": claims,
        "edges": edges_all,
        "preference_clusters": clusters,
        "access_log": identity_access_log_list(conn, limit=2000),
    }


def grant_identity_context(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    purpose: str,
    requested_layers: Sequence[int],
    session_grants: Set[int],
    limit_per_layer: int = 24,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    msg, req = identity_layers.normalize_layers(requested_layers)
    if msg:
        return None, msg
    if not req:
        return None, "requested_layers must be non-empty"
    purpose_s = (purpose or "").strip()
    if len(purpose_s) < 3:
        return None, "purpose too short"
    agent_s = (agent_id or "").strip()
    if len(agent_s) < 1:
        return None, "agent_id required"

    granted, denied = identity_layers.resolve_granted_layers(req, session_grants)
    rows = identity_claim_list(conn, status="active", limit=800)
    per_layer: Dict[int, int] = {}
    picked: List[Dict[str, Any]] = []
    for c in rows:
        el = identity_layers.effective_layer(c)
        if el not in granted:
            continue
        n = per_layer.get(el, 0)
        if n >= max(1, min(limit_per_layer, 200)):
            continue
        per_layer[el] = n + 1
        picked.append(c)

    claims: Dict[str, List[Dict[str, Any]]] = {}
    for c in picked:
        L = identity_layers.effective_layer(c)
        claims.setdefault(str(L), []).append(
            {
                "claim_id": c["claim_id"],
                "layer": L,
                "field": c.get("field"),
                "kind": c.get("kind"),
                "text": c.get("text"),
                "confidence": c.get("confidence"),
            }
        )

    log_id = new_access_log_id()
    # SAVEPOINT: callers may hold an open transaction (e.g. status patch); avoid nested BEGIN.
    conn.execute("SAVEPOINT ial_access_log")
    try:
        identity_access_log_insert(
            conn,
            log_id=log_id,
            agent_id=agent_s[:200],
            purpose=purpose_s[:2000],
            requested_layers=sorted(req),
            granted_layers=sorted(granted),
            denied_layers=sorted(denied),
            claim_ids=[str(c["claim_id"]) for c in picked],
            meta={},
        )
        conn.execute("RELEASE SAVEPOINT ial_access_log")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ial_access_log")
        raise
    return (
        {
            "granted_layers": sorted(granted),
            "denied_layers": sorted(denied),
            "claims": claims,
            "access_log_id": log_id,
        },
        None,
    )


def flag_claims_evidence_withdrawn_for_source(conn: sqlite3.Connection, source_id: str) -> int:
    """Mark claims that cite this source's chunks as evidence_withdrawn (consent revoked)."""
    rows = conn.execute(
        "SELECT DISTINCT ie.claim_id FROM identity_edges ie "
        "INNER JOIN chunks c ON c.chunk_id = ie.chunk_id "
        "WHERE c.source_id = ? AND ie.chunk_id IS NOT NULL",
        (source_id,),
    ).fetchall()
    n = 0
    now = time.time()
    for (cid,) in rows:
        claim = identity_claim_get(conn, cid)
        if not claim:
            continue
        meta = dict(claim["meta"])
        if meta.get("evidence_withdrawn"):
            continue
        meta["evidence_withdrawn"] = True
        meta["evidence_withdrawn_at"] = now
        meta["evidence_withdrawn_reason"] = f"source_revoked:{source_id}"
        conn.execute(
            "UPDATE identity_claims SET meta_json = ?, updated_at = ? WHERE claim_id = ?",
            (json.dumps(meta, ensure_ascii=False), now, cid),
        )
        n += 1
    return n


def identity_schema_public() -> Dict[str, Any]:
    """Stable JSON for UI / agents: layers, tiers, allowed fields."""
    layers = []
    for n in range(identity_layers.MIN_LAYER, identity_layers.MAX_LAYER + 1):
        layers.append(
            {
                "layer": n,
                "title": identity_layers.LAYER_TITLES.get(n, ""),
                "access_tier": identity_layers.LAYER_ACCESS_TIER.get(n, "open"),
                "fields": sorted(identity_layers.LAYER_FIELDS.get(n, frozenset())),
            }
        )
    return {
        "version": 1,
        "layers": layers,
        "claim_kinds": sorted(CLAIM_KINDS),
        "claim_statuses": sorted(CLAIM_STATUSES),
    }


def snapshot_manifest_hash(snapshot: Dict[str, Any]) -> str:
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
