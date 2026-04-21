#!/usr/bin/env python3
"""
Golden-query harness for the Minion MCP server.

Spawns chatgpt_mcp_memory/src/mcp_server.py as a subprocess over stdio,
sends JSON-RPC initialize + tools/call search_memory for each case in
golden_queries.yaml, and reports pass/fail + MRR.

Usage:
    python eval/run_eval.py --derived-dir <path> [--golden eval/golden_queries.yaml]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER_PY = REPO_ROOT / "chatgpt_mcp_memory" / "src" / "mcp_server.py"
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden_queries.yaml"
PROTOCOL_VERSION = "2025-11-25"


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        # Minimal fallback so eval works without PyYAML installed.
        return _parse_minimal_yaml(path.read_text(encoding="utf-8"))
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _parse_minimal_yaml(text: str) -> Dict[str, Any]:
    """
    Tiny YAML subset parser that handles our golden_queries.yaml shape:
      cases:
        - query: "..."
          expect_any_of:
            - substring: "..."
            - title: "..."
          top_k: 8
          role: user
    Strings may be quoted with " or '. Comments start with '#'.
    """
    cases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_list_key: Optional[str] = None
    expect_list: Optional[List[Dict[str, Any]]] = None

    def _parse_scalar(raw: str) -> Any:
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1]
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        if raw.lower() in ("null", "~", ""):
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)

        if indent == 0 and stripped.rstrip(":") == "cases":
            continue

        if stripped.startswith("- "):
            inner = stripped[2:]
            # Top-level case start: "- query: ..."
            if indent <= 2:
                current = {}
                cases.append(current)
                expect_list = None
                current_list_key = None
                if ":" in inner:
                    key, _, val = inner.partition(":")
                    current[key.strip()] = _parse_scalar(val)
            else:
                if expect_list is None:
                    # Shouldn't happen for well-formed input.
                    continue
                item: Dict[str, Any] = {}
                if ":" in inner:
                    key, _, val = inner.partition(":")
                    item[key.strip()] = _parse_scalar(val)
                expect_list.append(item)
            continue

        if current is None:
            continue

        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "expect_any_of" and not val:
                expect_list = []
                current[key] = expect_list
                current_list_key = key
            else:
                current[key] = _parse_scalar(val)
                current_list_key = None

    return {"cases": cases}


@dataclass
class MatcherResult:
    matcher: Dict[str, Any]
    matched_rank: Optional[int] = None  # 1-based rank of first matching hit
    matched_chunk_id: Optional[str] = None


@dataclass
class CaseResult:
    query: str
    passed: bool
    matchers: List[MatcherResult] = field(default_factory=list)
    hits: List[Dict[str, Any]] = field(default_factory=list)
    first_match_rank: Optional[int] = None
    error: Optional[str] = None


def _matches(matcher: Dict[str, Any], hit: Dict[str, Any]) -> bool:
    if "substring" in matcher:
        needle = str(matcher["substring"] or "").lower()
        return bool(needle) and needle in str(hit.get("text") or "").lower()
    if "conversation_id" in matcher:
        return str(matcher["conversation_id"]) == str(hit.get("conversation_id") or "")
    if "chunk_id" in matcher:
        return str(matcher["chunk_id"]) == str(hit.get("chunk_id") or "")
    if "title" in matcher:
        needle = str(matcher["title"] or "").lower()
        return bool(needle) and needle in str(hit.get("conversation_title") or "").lower()
    return False


class MCPStdioClient:
    """Minimal JSON-RPC-over-stdio client for the Minion MCP server."""

    def __init__(self, server_py: Path, derived_dir: Path, python_exe: Optional[str] = None):
        self.server_py = server_py
        self.derived_dir = derived_dir
        self.python_exe = python_exe or sys.executable
        self._proc: Optional[subprocess.Popen] = None
        self._id = 0

    def start(self) -> None:
        env = dict(os.environ)
        env["MINION_DATA_DIR"] = str(self.derived_dir)
        # Retain legacy var for older server builds.
        env["CHATGPT_MCP_DATA_DIR"] = str(self.derived_dir)
        self._proc = subprocess.Popen(
            [self.python_exe, "-u", str(self.server_py)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

        line = self._proc.stdout.readline()
        if not line:
            stderr_tail = ""
            if self._proc.stderr is not None:
                try:
                    stderr_tail = self._proc.stderr.read() or ""
                except Exception:
                    stderr_tail = ""
            raise RuntimeError(
                f"MCP server closed stdout before responding to {method}. "
                f"stderr:\n{stderr_tail}"
            )
        return json.loads(line)

    def initialize(self) -> Dict[str, Any]:
        return self._send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": {"name": "minion-eval", "version": "0.1.0"},
                "capabilities": {},
            },
        )

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Return the raw `result` dict (structuredContent + content + isError)."""
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        return resp.get("result") or {}

    def search_memory(
        self,
        query: str,
        *,
        top_k: int = 8,
        role: Optional[str] = None,
        max_chars: int = 900,
        dedupe_by_conversation: bool = True,
        mode: Optional[str] = None,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        args: Dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "max_chars": max_chars,
            "dedupe_by_conversation": dedupe_by_conversation,
        }
        if role:
            args["role"] = role
        if mode:
            args["mode"] = mode
        if before is not None:
            args["before"] = before
        if after is not None:
            args["after"] = after
        result = self.call_tool("ask_minion", args)
        structured = result.get("structuredContent")
        # New shape: {"results": [...], optional "profile_brief": "..."}.
        if isinstance(structured, dict) and isinstance(structured.get("results"), list):
            return structured["results"]
        # Legacy shape: bare list.
        if isinstance(structured, list):
            return structured
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text") or "[]")
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
                    return parsed["results"]
        return []


def run_case(
    client: MCPStdioClient,
    case: Dict[str, Any],
    *,
    top_k_override: Optional[int],
) -> CaseResult:
    query = str(case.get("query") or "")
    matchers_raw = case.get("expect_any_of") or []
    matchers = [m for m in matchers_raw if isinstance(m, dict)]
    top_k = int(case.get("top_k") or 8)
    if top_k_override is not None:
        top_k = top_k_override
    role = case.get("role")
    role = str(role) if role else None

    result = CaseResult(query=query, passed=False, matchers=[MatcherResult(matcher=m) for m in matchers])
    try:
        hits = client.search_memory(query, top_k=top_k, role=role)
    except Exception as e:
        result.error = str(e)
        return result

    result.hits = hits
    if not matchers:
        # No expectation => just mark as passed if we got any hit.
        result.passed = bool(hits)
        return result

    for mr in result.matchers:
        for rank, hit in enumerate(hits, start=1):
            if _matches(mr.matcher, hit):
                mr.matched_rank = rank
                mr.matched_chunk_id = str(hit.get("chunk_id") or "")
                break

    matched_ranks = [mr.matched_rank for mr in result.matchers if mr.matched_rank]
    if matched_ranks:
        result.passed = True
        result.first_match_rank = min(matched_ranks)
    return result


def _human_report(cases: List[CaseResult]) -> str:
    out: List[str] = []
    passed = sum(1 for c in cases if c.passed)
    total = len(cases)
    mrr_terms = [1.0 / c.first_match_rank for c in cases if c.first_match_rank]
    mrr = (sum(mrr_terms) / total) if total else 0.0

    out.append("")
    out.append(f"Minion MCP eval — {passed}/{total} passed, MRR={mrr:.3f}")
    out.append("=" * 64)
    for c in cases:
        tag = "PASS" if c.passed else "FAIL"
        rank = f" rank={c.first_match_rank}" if c.first_match_rank else ""
        out.append(f"[{tag}] {c.query!r}{rank}")
        if c.error:
            out.append(f"       ERROR: {c.error}")
            continue
        for mr in c.matchers:
            status = f"hit@{mr.matched_rank}" if mr.matched_rank else "miss"
            out.append(f"       - {mr.matcher}  =>  {status}")
        if not c.passed and c.hits:
            top = c.hits[0]
            snippet = str(top.get("text") or "").replace("\n", " ")[:120]
            out.append(f"       top hit: {top.get('chunk_id')}  {snippet!r}")
    out.append("")
    return "\n".join(out)


def _json_report(cases: List[CaseResult]) -> str:
    passed = sum(1 for c in cases if c.passed)
    total = len(cases)
    mrr_terms = [1.0 / c.first_match_rank for c in cases if c.first_match_rank]
    mrr = (sum(mrr_terms) / total) if total else 0.0
    payload = {
        "summary": {"passed": passed, "total": total, "mrr": mrr},
        "cases": [
            {
                "query": c.query,
                "passed": c.passed,
                "first_match_rank": c.first_match_rank,
                "error": c.error,
                "matchers": [
                    {
                        "matcher": mr.matcher,
                        "matched_rank": mr.matched_rank,
                        "matched_chunk_id": mr.matched_chunk_id,
                    }
                    for mr in c.matchers
                ],
                "hits": [
                    {
                        "rank": i + 1,
                        "chunk_id": h.get("chunk_id"),
                        "score": h.get("score"),
                        "conversation_id": h.get("conversation_id"),
                        "conversation_title": h.get("conversation_title"),
                        "role": h.get("role"),
                        "text": h.get("text"),
                    }
                    for i, h in enumerate(c.hits)
                ],
            }
            for c in cases
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden-query harness for the Minion MCP.")
    ap.add_argument("--derived-dir", required=True, help="Directory with chunks/embeddings/manifest.")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="Path to golden_queries.yaml.")
    ap.add_argument("--server-py", default=str(DEFAULT_SERVER_PY), help="Path to mcp_server.py.")
    ap.add_argument("--python-exe", default=None, help="Python executable for the MCP subprocess.")
    ap.add_argument("--top-k", type=int, default=None, help="Override top_k for every case.")
    ap.add_argument("--json", action="store_true", help="Emit JSON report to stdout instead of text.")
    args = ap.parse_args()

    derived_dir = Path(args.derived_dir).expanduser().resolve()
    golden_path = Path(args.golden).expanduser().resolve()
    server_py = Path(args.server_py).expanduser().resolve()

    if not derived_dir.exists():
        print(f"Missing derived dir: {derived_dir}", file=sys.stderr)
        return 2
    if not golden_path.exists():
        print(f"Missing golden file: {golden_path}", file=sys.stderr)
        return 2
    if not server_py.exists():
        print(f"Missing MCP server script: {server_py}", file=sys.stderr)
        return 2

    data = _load_yaml(golden_path)
    cases_in = data.get("cases") or []
    if not isinstance(cases_in, list) or not cases_in:
        print(f"No cases in {golden_path}", file=sys.stderr)
        return 2

    client = MCPStdioClient(server_py=server_py, derived_dir=derived_dir, python_exe=args.python_exe)
    client.start()
    started = time.time()
    try:
        client.initialize()
        results = [run_case(client, c, top_k_override=args.top_k) for c in cases_in if isinstance(c, dict)]
    finally:
        client.stop()

    elapsed = time.time() - started
    if args.json:
        print(_json_report(results))
    else:
        print(_human_report(results))
        print(f"({elapsed:.1f}s total)")

    return 0 if all(c.passed for c in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
