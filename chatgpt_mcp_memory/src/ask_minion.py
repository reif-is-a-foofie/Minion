#!/usr/bin/env python3
"""
Chunk-native strategic profile: load chunks.jsonl, extract structured signals per
conversation (map), aggregate, synthesize one markdown doc (reduce) via local Ollama.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from llm import chat

DEFAULT_MODEL = "mistral:7b"
DEFAULT_MAX_WINDOW_CHARS = 14_000
WINDOW_OVERLAP = 2_000
TOP_ITEMS_PER_LIST_FOR_SYNTH = 120
EXTRACT_NUM_PREDICT = 2_048
SYNTH_NUM_PREDICT = 3_200


EXTRACTION_KEYS = ("decisions", "frameworks", "beliefs", "projects", "verbatim_snippets")


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def _parse_json_object(raw: str) -> Dict[str, Any]:
    """Best-effort parse of a single JSON object from model output."""
    s = _strip_code_fences(raw)
    try:
        out = json.loads(s)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            out = json.loads(s[start : end + 1])
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
    raise ValueError("Could not parse JSON object from model response.")


def _normalize_key(s: str) -> str:
    return " ".join(s.lower().split())


def _dedupe_lists(d: Dict[str, List[str]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k in EXTRACTION_KEYS:
        seen: set[str] = set()
        items: List[str] = []
        for x in d.get(k) or []:
            if not isinstance(x, str):
                continue
            t = x.strip()
            if not t:
                continue
            nk = _normalize_key(t)
            if nk in seen:
                continue
            seen.add(nk)
            items.append(t)
        out[k] = items
    return out


def _empty_extraction() -> Dict[str, List[str]]:
    return {k: [] for k in EXTRACTION_KEYS}


def _merge_extractions(parts: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    merged = _empty_extraction()
    for p in parts:
        if not isinstance(p, dict):
            continue
        for k in EXTRACTION_KEYS:
            v = p.get(k)
            if isinstance(v, list):
                merged[k].extend(str(x).strip() for x in v if str(x).strip())
            elif isinstance(v, str) and v.strip():
                merged[k].append(v.strip())
    return _dedupe_lists(merged)


def _cap_for_synthesis(agg: Dict[str, List[str]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k, items in agg.items():
        out[k] = items[:TOP_ITEMS_PER_LIST_FOR_SYNTH]
    return out


def load_chunks_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def group_by_conversation(chunks: List[Dict[str, Any]]) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """
    Returns list of (conversation_id, title, ordered_chunks) sorted by
    max(create_time) descending for stable prioritization.
    """
    by_cid: Dict[str, List[Dict[str, Any]]] = {}
    for c in chunks:
        cid = str(c.get("conversation_id") or "")
        by_cid.setdefault(cid, []).append(c)

    def sort_key(chs: List[Dict[str, Any]]) -> Tuple[float, str]:
        times = [float(x["create_time"]) for x in chs if x.get("create_time") is not None]
        tmax = max(times) if times else 0.0
        title = str(chs[0].get("conversation_title") or "") if chs else ""
        return (-tmax, title)

    out: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for cid, chs in by_cid.items():
        chs_sorted = sorted(
            chs,
            key=lambda x: (float(x["create_time"]) if x.get("create_time") is not None else 0.0, x.get("chunk_id") or ""),
        )
        title = str(chs_sorted[0].get("conversation_title") or "") if chs_sorted else ""
        out.append((cid, title, chs_sorted))

    out.sort(key=lambda item: sort_key(item[2]))
    return out


def conversation_body(chs: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for c in chs:
        role = str(c.get("role") or "unknown")
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"[{role}]\n{text}")
    return "\n\n".join(parts)


def iter_text_windows(text: str, max_chars: int, overlap: int) -> Iterable[str]:
    if max_chars <= 0:
        return
    if len(text) <= max_chars:
        yield text
        return
    i = 0
    step = max(1, max_chars - overlap)
    while i < len(text):
        yield text[i : i + max_chars]
        i += step
        if i >= len(text):
            break


def extract_from_text(
    *,
    text: str,
    title: str,
    model: str,
    timeout_seconds: Optional[float],
) -> Dict[str, List[str]]:
    system = (
        "You extract structured information from conversation text. "
        "Output ONLY a single JSON object, no markdown fences. "
        "If nothing is supported by the text, use empty arrays."
    )
    user = (
        f"Conversation title: {title}\n\n"
        "Text:\n"
        f"{text}\n\n"
        "Return JSON with exactly these keys (each value is an array of strings): "
        "decisions, frameworks, beliefs, projects, verbatim_snippets. "
        "Only include items explicitly supported by the text; do not invent."
    )
    resp = chat(
        model=model,
        system=system,
        user=user,
        options={"temperature": 0.1, "num_predict": EXTRACT_NUM_PREDICT},
        timeout_seconds=timeout_seconds,
    )
    parsed = _parse_json_object(resp.content)
    return _merge_extractions([parsed])


def synthesize_profile(
    *,
    aggregated: Dict[str, List[str]],
    model: str,
    timeout_seconds: Optional[float],
) -> str:
    capped = _cap_for_synthesis(aggregated)
    blob = json.dumps(capped, ensure_ascii=False, indent=2)
    system = (
        "You synthesize a stable strategic/identity profile from aggregated evidence JSON. "
        "Use ONLY the supplied JSON. Do not invent facts, names, or events. "
        "If a section lacks evidence, write 'Unknown' or skip."
    )
    user = (
        "Produce markdown between 800 and 1200 words with EXACT section headings in this order:\n\n"
        "# Strategic profile\n"
        "## Thesis and priorities\n"
        "## Active projects and commitments\n"
        "## Frameworks and mental models\n"
        "## Beliefs and decisions\n"
        "## Recurring themes\n"
        "## Evidence gaps\n\n"
        "Use bullets where helpful. Stay grounded in the JSON.\n\n"
        "Aggregated evidence (JSON):\n"
        f"{blob}"
    )
    resp = chat(
        model=model,
        system=system,
        user=user,
        options={"temperature": 0.2, "num_predict": SYNTH_NUM_PREDICT},
        timeout_seconds=timeout_seconds,
    )
    text = resp.content.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    return text if text.endswith("\n") else text + "\n"


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> None:
    default_derived = Path(__file__).resolve().parents[1] / "data" / "derived"
    parser = argparse.ArgumentParser(
        description="Synthesize identity_profile.md from chunks.jsonl via map→reduce and local Ollama (ask_minion)."
    )
    parser.add_argument(
        "--derived-dir",
        default=str(default_derived),
        help="Directory for outputs (default: chatgpt_mcp_memory/data/derived)",
    )
    parser.add_argument(
        "--chunks",
        default=None,
        help="Path to chunks.jsonl (default: <derived-dir>/chunks.jsonl)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    parser.add_argument(
        "--out",
        default=None,
        help="Output markdown path (default: <derived-dir>/identity_profile.md)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=None, help="Ollama client timeout")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and group chunks only; print stats, no LLM calls",
    )
    parser.add_argument(
        "--max-conversations",
        type=int,
        default=None,
        help="Process at most this many conversations (after filters, in processing order)",
    )
    parser.add_argument(
        "--sample-every-n",
        type=int,
        default=1,
        help="Keep every Nth conversation after filters (default: 1 = all)",
    )
    parser.add_argument(
        "--since-unix",
        type=float,
        default=None,
        help="Only conversations with max(create_time) >= this Unix timestamp",
    )
    parser.add_argument(
        "--max-window-chars",
        type=int,
        default=DEFAULT_MAX_WINDOW_CHARS,
        help="Max chars per extract window (large threads are split)",
    )
    parser.add_argument(
        "--save-extractions",
        default=None,
        help="Optional path to write JSONL of per-conversation merged extractions",
    )
    args = parser.parse_args()

    derived_dir = Path(args.derived_dir).expanduser().resolve()
    chunks_path = Path(args.chunks).expanduser().resolve() if args.chunks else derived_dir / "chunks.jsonl"
    out_path = Path(args.out).expanduser().resolve() if args.out else derived_dir / "identity_profile.md"
    manifest_path = derived_dir / "identity_profile_manifest.json"

    if not chunks_path.exists():
        print(f"Missing chunks file: {chunks_path}", file=sys.stderr)
        raise SystemExit(1)

    chunks = load_chunks_jsonl(chunks_path)
    grouped = group_by_conversation(chunks)

    def conv_max_time(chs: List[Dict[str, Any]]) -> Optional[float]:
        ts = [float(x["create_time"]) for x in chs if x.get("create_time") is not None]
        return max(ts) if ts else None

    filtered: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for cid, title, chs in grouped:
        if args.since_unix is not None:
            mt = conv_max_time(chs)
            if mt is None or mt < float(args.since_unix):
                continue
        filtered.append((cid, title, chs))

    if args.sample_every_n < 1:
        print("--sample-every-n must be >= 1", file=sys.stderr)
        raise SystemExit(1)

    sampled: List[Tuple[str, str, List[Dict[str, Any]]]] = [
        filtered[i] for i in range(len(filtered)) if i % int(args.sample_every_n) == 0
    ]

    if args.max_conversations is not None:
        sampled = sampled[: int(args.max_conversations)]

    print(f"chunks: {len(chunks)}  conversations (grouped): {len(grouped)}  after filters: {len(sampled)}")

    if args.dry_run:
        print("dry-run: no LLM calls")
        return

    extractions_path: Optional[Path] = None
    if args.save_extractions:
        extractions_path = Path(args.save_extractions).expanduser().resolve()
        extractions_path.parent.mkdir(parents=True, exist_ok=True)

    aggregate = _empty_extraction()
    errors = 0
    model = str(args.model)
    max_w = int(args.max_window_chars)

    for idx, (cid, title, chs) in enumerate(sampled):
        body = conversation_body(chs)
        if not body.strip():
            continue
        window_parts: List[Dict[str, List[str]]] = []
        for win in iter_text_windows(body, max_w, WINDOW_OVERLAP):
            try:
                part = extract_from_text(
                    text=win,
                    title=title or "(untitled)",
                    model=model,
                    timeout_seconds=args.timeout_seconds,
                )
                window_parts.append(part)
            except Exception as e:
                errors += 1
                print(f"extract error conv={cid!r} idx={idx}: {e}", file=sys.stderr)

        merged = _merge_extractions(window_parts)
        if extractions_path is not None:
            line = json.dumps(
                {"conversation_id": cid, "title": title, "extraction": merged},
                ensure_ascii=False,
            )
            with open(extractions_path, "a", encoding="utf-8") as ef:
                ef.write(line + "\n")

        for k in EXTRACTION_KEYS:
            aggregate[k].extend(merged.get(k) or [])

        if (idx + 1) % 25 == 0 or (idx + 1) == len(sampled):
            print(f"  processed {idx + 1}/{len(sampled)} conversations…", flush=True)

    aggregate = _dedupe_lists(aggregate)

    try:
        markdown = synthesize_profile(
            aggregated=aggregate,
            model=model,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as e:
        print(f"synthesize error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    derived_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    manifest = {
        "created_at_unix": time.time(),
        "model": model,
        "chunks_path": str(chunks_path),
        "derived_dir": str(derived_dir),
        "out": str(out_path),
        "conversation_count_processed": len(sampled),
        "chunk_rows": len(chunks),
        "filters": {
            "since_unix": args.since_unix,
            "sample_every_n": int(args.sample_every_n),
            "max_conversations": args.max_conversations,
            "max_window_chars": max_w,
        },
        "extraction_errors": errors,
        "save_extractions": str(extractions_path) if extractions_path else None,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    repo = _default_repo_root()
    agent_copy = repo / "agent" / "identity_profile.md"
    try:
        agent_copy.parent.mkdir(parents=True, exist_ok=True)
        agent_copy.write_text(markdown, encoding="utf-8")
    except OSError:
        pass

    print(str(out_path))
    print(str(manifest_path))
    if agent_copy.exists():
        print(str(agent_copy))


if __name__ == "__main__":
    main()
