#!/usr/bin/env python3
from __future__ import annotations

import sys
import json
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

# Ensure no warnings are emitted to stdout (breaks JSON-RPC framing over stdio).
# Must run before importing libraries that may trigger warnings at import time.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*LibreSSL.*",
)

from sentence_transformers import SentenceTransformer


APP_NAME = "ChatGPT Memory (Local)"
TOP_K_CAP = 12
DEFAULT_TOP_K = 8
DEFAULT_MAX_CHARS = 900
DEFAULT_MAX_CHARS_FULL = 2000
PROTOCOL_VERSION = "2025-11-25"

# Shown to Claude when the MCP session starts (if no policy file is found nearby).
_INSTRUCTIONS_FALLBACK = (
    "Use tools/search_memory for small, per-hit retrieval; use get_chunk to expand a hit. "
    "Call search_memory early when prior context might matter. Avoid pulling huge context."
)


def _instructions_max_chars() -> int:
    raw = os.environ.get("CHATGPT_MCP_INSTRUCTIONS_MAX_CHARS", "20000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 20000


def _load_retrieval_instructions() -> str:
    """
    Prefer file-based policy so Claude receives full retrieval discipline via MCP initialize.instructions.
    Search order:
    1) CHATGPT_MCP_RETRIEVAL_POLICY path
    2) <CHATGPT_MCP_DATA_DIR>/retrieval_policy.md
    3) parent of data dir / retrieval_policy.md (run folder next to derived/)
    """
    explicit = os.environ.get("CHATGPT_MCP_RETRIEVAL_POLICY")
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())
    data_dir = _data_dir()
    candidates.append(data_dir / "retrieval_policy.md")
    candidates.append(data_dir.parent / "retrieval_policy.md")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            cap = _instructions_max_chars()
            if len(text) > cap:
                text = text[: cap - 30].rstrip() + "\n\n… [truncated for MCP instructions size cap]"
            return (
                "Minion memory MCP — follow this retrieval policy when using search_memory / get_chunk:\n\n"
                + text
            )
    return _INSTRUCTIONS_FALLBACK


@dataclass
class MemoryIndex:
    manifest: Dict[str, Any]
    chunks: List[Dict[str, Any]]
    embeddings: np.ndarray
    model: SentenceTransformer


_INDEX: Optional[MemoryIndex] = None


def _data_dir() -> Path:
    env = os.environ.get("CHATGPT_MCP_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()

    # Fallbacks:
    # - Source checkout: <repo>/src/mcp_server.py -> <repo>/data/derived
    # - PyInstaller onefile: executable in <repo>/dist/minion-mcp -> <repo>/data/derived
    here = Path(__file__).resolve()
    repo_guess = here.parents[1]
    candidate = repo_guess / "data" / "derived"
    if candidate.exists():
        return candidate

    exe = Path(sys.argv[0]).resolve()
    candidate2 = exe.parent.parent / "data" / "derived"
    return candidate2


def _load_index() -> MemoryIndex:
    global _INDEX
    if _INDEX is not None:
        return _INDEX

    data_dir = _data_dir()
    manifest_path = data_dir / "manifest.json"
    chunks_path = data_dir / "chunks.jsonl"
    embeddings_path = data_dir / "embeddings.npy"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    if not chunks_path.exists():
        raise FileNotFoundError(f"Missing chunks: {chunks_path}")
    if not embeddings_path.exists():
        raise FileNotFoundError(f"Missing embeddings: {embeddings_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks: List[Dict[str, Any]] = []
    with open(chunks_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            chunks.append(json.loads(line))

    embeddings = np.load(embeddings_path)
    if len(chunks) != embeddings.shape[0]:
        raise ValueError(f"Chunk/embedding count mismatch: chunks={len(chunks)} embeddings={embeddings.shape[0]}")

    model = SentenceTransformer(manifest["model_name"])
    _INDEX = MemoryIndex(manifest=manifest, chunks=chunks, embeddings=embeddings, model=model)
    return _INDEX


def _cap_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _tool_search_memory(arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
    query = str(arguments.get("query") or "")
    top_k = int(arguments.get("top_k") or DEFAULT_TOP_K)
    role = arguments.get("role")
    role = str(role) if role is not None else None
    max_chars = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS)
    dedupe_by_conversation = bool(arguments.get("dedupe_by_conversation", True))

    idx = _load_index()

    if top_k < 1:
        top_k = 1
    if top_k > TOP_K_CAP:
        top_k = TOP_K_CAP

    query_embedding = idx.model.encode([query], convert_to_numpy=True, normalize_embeddings=True)[0]
    scores = idx.embeddings @ query_embedding
    ranked = np.argsort(-scores)

    results: List[Dict[str, Any]] = []
    seen_conv: set[str] = set()
    for i in ranked:
        chunk = idx.chunks[int(i)]
        if role and chunk.get("role") != role:
            continue
        conv_id = str(chunk.get("conversation_id") or "")
        if dedupe_by_conversation and conv_id:
            if conv_id in seen_conv:
                continue
            seen_conv.add(conv_id)

        results.append(
            {
                "score": float(scores[int(i)]),
                "chunk_id": chunk.get("chunk_id"),
                "role": chunk.get("role"),
                "conversation_id": chunk.get("conversation_id"),
                "conversation_title": chunk.get("conversation_title"),
                "create_time": chunk.get("create_time"),
                "text": _cap_text(str(chunk.get("text") or ""), max_chars),
            }
        )
        if len(results) >= top_k:
            break

    return results


def _tool_get_chunk(arguments: Dict[str, Any]) -> Dict[str, Any]:
    chunk_id = str(arguments.get("chunk_id") or "")
    max_chars = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS_FULL)

    idx = _load_index()
    for chunk in idx.chunks:
        if chunk.get("chunk_id") == chunk_id:
            return {
                "chunk_id": chunk.get("chunk_id"),
                "role": chunk.get("role"),
                "conversation_id": chunk.get("conversation_id"),
                "conversation_title": chunk.get("conversation_title"),
                "create_time": chunk.get("create_time"),
                "text": _cap_text(str(chunk.get("text") or ""), max_chars),
            }
    raise ValueError(f"chunk_id not found: {chunk_id}")


def _tool_index_info(_: Dict[str, Any]) -> Dict[str, Any]:
    idx = _load_index()
    return {
        "data_dir": str(_data_dir()),
        "chunk_count": idx.manifest.get("chunk_count"),
        "roles_indexed": idx.manifest.get("roles_indexed"),
        "model_name": idx.manifest.get("model_name"),
        "created_at_unix": idx.manifest.get("created_at_unix"),
        "max_chars": idx.manifest.get("max_chars"),
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_memory",
        "title": "Search ChatGPT memory",
        "description": "Semantic search over the local ChatGPT export-derived index. Returns top-k short snippets + metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": TOP_K_CAP, "default": DEFAULT_TOP_K},
                "role": {"type": ["string", "null"], "description": "Filter by role: user or assistant"},
                "max_chars": {"type": "integer", "minimum": 50, "maximum": 4000, "default": DEFAULT_MAX_CHARS},
                "dedupe_by_conversation": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_chunk",
        "title": "Get a chunk by id",
        "description": "Fetch a single chunk by chunk_id (useful for expanding a promising search hit).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 50, "maximum": 10000, "default": DEFAULT_MAX_CHARS_FULL},
            },
            "required": ["chunk_id"],
        },
    },
    {
        "name": "index_info",
        "title": "Index metadata",
        "description": "Return metadata about the loaded local index.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
]


def _jsonrpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    out: Dict[str, Any] = {"jsonrpc": "2.0", "error": err}
    if req_id is not None:
        out["id"] = req_id
    return out


def _tool_result(payload: Any, *, is_error: bool = False) -> Dict[str, Any]:
    # Include both structuredContent (best for clients) and a text fallback for older tooling.
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


def _handle_initialize(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    params = req.get("params") or {}
    requested = params.get("protocolVersion") or PROTOCOL_VERSION
    # For now we just respond with our preferred version. Client can decide if it supports it.
    return _jsonrpc_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chatgpt-memory-local", "title": APP_NAME, "version": "0.1.0"},
            "instructions": _load_retrieval_instructions(),
        },
    )


def _handle_tools_list(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    return _jsonrpc_result(req_id, {"tools": TOOLS})


def _handle_tools_call(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    params = req.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "search_memory":
        try:
            payload = _tool_search_memory(arguments)
            return _jsonrpc_result(req_id, _tool_result(payload))
        except Exception as e:
            return _jsonrpc_result(req_id, _tool_result({"error": str(e)}, is_error=True))
    if name == "get_chunk":
        try:
            payload = _tool_get_chunk(arguments)
            return _jsonrpc_result(req_id, _tool_result(payload))
        except Exception as e:
            return _jsonrpc_result(req_id, _tool_result({"error": str(e)}, is_error=True))
    if name == "index_info":
        try:
            payload = _tool_index_info(arguments)
            return _jsonrpc_result(req_id, _tool_result(payload))
        except Exception as e:
            return _jsonrpc_result(req_id, _tool_result({"error": str(e)}, is_error=True))
    return _jsonrpc_error(req_id, -32602, f"Unknown tool: {name}")


def main() -> None:
    # Stdio transport: read JSON-RPC messages line-by-line, write responses to stdout.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            resp = _jsonrpc_error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        method = req.get("method")
        # Notifications have no id and require no response.
        if req.get("id") is None:
            continue

        if method == "initialize":
            resp = _handle_initialize(req)
        elif method == "tools/list":
            resp = _handle_tools_list(req)
        elif method == "tools/call":
            resp = _handle_tools_call(req)
        elif method == "ping":
            resp = _jsonrpc_result(req.get("id"), {})
        else:
            resp = _jsonrpc_error(req.get("id"), -32601, f"Method not found: {method}")

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

