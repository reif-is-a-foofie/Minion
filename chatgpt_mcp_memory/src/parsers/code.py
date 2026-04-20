"""Source-code parser.

Preferred: tree-sitter-language-pack to split at function/class/method
boundaries. Fallback: fixed 40-line windows (preserves enough context for
semantic search without dragging entire files into one chunk).

The parser is robust to missing tree-sitter grammars - if tree-sitter
can't be imported or the language isn't available, we transparently
fall back to line-window chunking.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from . import ParsedChunk, ParseResult
from ._common import window_text


_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cxx": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".m": "objc", ".mm": "objc",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".lua": "lua",
    ".r": "r", ".R": "r",
    ".sql": "sql",
    ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".clj": "clojure", ".cljs": "clojure",
    ".vue": "vue",
    ".svelte": "svelte",
}

_BOUNDARY_NODES = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "class_definition",
    "class_declaration",
    "struct_item",
    "impl_item",
    "trait_item",
    "enum_item",
    "module",
    "interface_declaration",
}


def _load_parser(lang: str):  # pragma: no cover - depends on optional dep
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore
    except Exception:
        return None
    try:
        return get_parser(lang)
    except Exception:
        return None


def _tree_sitter_chunks(src: bytes, lang: str) -> Optional[List[Tuple[str, int]]]:
    parser = _load_parser(lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(src)
    except Exception:
        return None

    spans: List[Tuple[int, int]] = []

    def walk(node) -> None:
        if node.type in _BOUNDARY_NODES:
            spans.append((node.start_byte, node.end_byte))
            return
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    if not spans:
        return None

    spans.sort()
    text_chunks: List[Tuple[str, int]] = []
    for start, end in spans:
        snippet = src[start:end].decode("utf-8", errors="replace")
        start_line = src[:start].count(b"\n") + 1
        if snippet.strip():
            text_chunks.append((snippet, start_line))
    return text_chunks or None


def parse(path: Path) -> ParseResult:
    suffix = path.suffix
    lang = _EXT_LANG.get(suffix) or _EXT_LANG.get(suffix.lower())

    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return ParseResult(chunks=[], kind="code", parser="code-empty")

    if not raw_bytes:
        return ParseResult(chunks=[], kind="code", parser="code-empty")

    chunks: List[ParsedChunk] = []
    extractor = "line-window"

    if lang:
        ts_chunks = _tree_sitter_chunks(raw_bytes, lang)
        if ts_chunks:
            extractor = f"tree-sitter:{lang}"
            for i, (text, start_line) in enumerate(ts_chunks):
                chunks.append(
                    ParsedChunk(
                        text=text,
                        role=None,
                        meta={
                            "seq": i,
                            "language": lang,
                            "start_line": start_line,
                        },
                    )
                )

    if not chunks:
        text = raw_bytes.decode("utf-8", errors="replace")
        for i, c in enumerate(window_text(text, max_chars=1600)):
            chunks.append(
                ParsedChunk(
                    text=c,
                    role=None,
                    meta={"seq": i, "language": lang or suffix.lstrip(".")},
                )
            )

    return ParseResult(
        chunks=chunks,
        source_meta={"language": lang, "extractor": extractor},
        kind="code",
        parser=extractor,
    )
