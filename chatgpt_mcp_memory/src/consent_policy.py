"""Reader-scoped consent defaults + persistence (`consent_policy.json` in data dir).

MCP tools (`ask_minion`) filter retrieved chunks here before returning hits.
Desktop HTTP search stays unfiltered so the human sees their full vault locally.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from store import Hit

log = logging.getLogger(__name__)

DEFAULT_POLICY: Dict[str, Any] = {
    "schema_version": 1,
    "readers": {
        "mcp": {
            # Indexed chunks whose source kind matches are withheld from MCP retrieval.
            "deny_chunk_source_kinds": ["ambient"],
            # Additional path-based withholding (substring match on normalized paths).
            "deny_path_substrings": ["/screen-memory/"],
            # Screen-context MCP tools read jsonl separately — allow disabling explicitly.
            "allow_screen_context_tools": True,
        }
    },
}


def policy_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "consent_policy.json"


def load_policy(data_dir: Path | str) -> Dict[str, Any]:
    pol = copy.deepcopy(DEFAULT_POLICY)
    p = policy_path(Path(data_dir))
    if not p.is_file():
        return pol
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning("consent_policy.json unreadable; using defaults")
        return pol
    try:
        readers = raw.get("readers")
        if isinstance(readers, dict):
            mcp = readers.get("mcp")
            if isinstance(mcp, dict):
                pol["readers"]["mcp"].update(mcp)
    except Exception:
        log.warning("consent_policy.json partial parse failure; merging cautiously")
    return pol


def save_policy(data_dir: Path | str, policy: Dict[str, Any]) -> None:
    p = policy_path(Path(data_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def hit_allowed_for_mcp(hit: Hit, policy: Dict[str, Any]) -> bool:
    r = (policy.get("readers") or {}).get("mcp") or {}
    kinds = [
        str(x).strip().lower()
        for x in (r.get("deny_chunk_source_kinds") or [])
        if str(x).strip()
    ]
    if hit.kind and hit.kind.lower() in kinds:
        return False
    subs = [str(x) for x in (r.get("deny_path_substrings") or []) if str(x).strip()]
    path_l = (hit.path or "").replace("\\", "/")
    for s in subs:
        if s in path_l:
            return False
    return True


def filter_hits_for_mcp(hits: List[Hit], data_dir: Path | str) -> List[Hit]:
    pol = load_policy(Path(data_dir))
    return [h for h in hits if hit_allowed_for_mcp(h, pol)]


def screen_tools_allowed_for_mcp(data_dir: Path | str) -> bool:
    pol = load_policy(Path(data_dir))
    r = (pol.get("readers") or {}).get("mcp") or {}
    return bool(r.get("allow_screen_context_tools", True))
