"""
Parser registry. Each parser turns one on-disk file into a list of
ParsedChunk(text, role, meta). Heavy deps (whisper, tree-sitter, ocr models)
are imported lazily inside each parser so core installs stay tiny.

The dispatcher picks a parser by extension first, mimetype second. Override
by passing `parser=...` when calling `parse_file`.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ParsedChunk:
    text: str
    role: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """Return value from a parser: the chunks plus source-level metadata."""

    chunks: List[ParsedChunk]
    source_meta: Dict[str, Any] = field(default_factory=dict)
    kind: str = "unknown"
    parser: str = "unknown"


ParserFn = Callable[[Path], ParseResult]


# Extension -> (kind, module_path, function_name). Lazy-loaded via importlib
# so e.g. missing faster-whisper only errors when someone drops audio in.
_EXT_REGISTRY: Dict[str, Tuple[str, str, str]] = {
    # Plaintext / markup
    ".txt":  ("text", "parsers.text", "parse"),
    ".md":   ("text", "parsers.text", "parse"),
    ".markdown": ("text", "parsers.text", "parse"),
    ".rst":  ("text", "parsers.text", "parse"),
    ".org":  ("text", "parsers.text", "parse"),
    ".log":  ("text", "parsers.text", "parse"),
    ".csv":  ("text", "parsers.text", "parse"),
    ".tsv":  ("text", "parsers.text", "parse"),
    ".json": ("text", "parsers.text", "parse"),
    ".yaml": ("text", "parsers.text", "parse"),
    ".yml":  ("text", "parsers.text", "parse"),
    ".toml": ("text", "parsers.text", "parse"),
    ".ini":  ("text", "parsers.text", "parse"),
    # HTML
    ".html": ("html", "parsers.html", "parse"),
    ".htm":  ("html", "parsers.html", "parse"),
    # PDF
    ".pdf":  ("pdf",  "parsers.pdf",  "parse"),
    # Office docs
    ".docx": ("docx", "parsers.docx", "parse"),
    # Images
    ".png":  ("image", "parsers.image", "parse"),
    ".jpg":  ("image", "parsers.image", "parse"),
    ".jpeg": ("image", "parsers.image", "parse"),
    ".webp": ("image", "parsers.image", "parse"),
    ".bmp":  ("image", "parsers.image", "parse"),
    ".tif":  ("image", "parsers.image", "parse"),
    ".tiff": ("image", "parsers.image", "parse"),
    # Audio / video (audio track)
    ".mp3":  ("audio", "parsers.audio", "parse"),
    ".wav":  ("audio", "parsers.audio", "parse"),
    ".m4a":  ("audio", "parsers.audio", "parse"),
    ".flac": ("audio", "parsers.audio", "parse"),
    ".ogg":  ("audio", "parsers.audio", "parse"),
    ".opus": ("audio", "parsers.audio", "parse"),
    ".mp4":  ("audio", "parsers.audio", "parse"),
    ".mov":  ("audio", "parsers.audio", "parse"),
    ".webm": ("audio", "parsers.audio", "parse"),
    # ChatGPT export archives
    ".zip":  ("chatgpt-export", "parsers.chatgpt_export", "parse"),
}


# Common code extensions. tree-sitter-language-pack handles dozens; we map
# the popular ones and let the code parser fall back to line-window chunking
# for anything with an unknown grammar.
_CODE_EXT = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go",
    ".rs",
    ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".m", ".mm",
    ".sh", ".bash", ".zsh",
    ".lua",
    ".r", ".R",
    ".sql",
    ".dart",
    ".ex", ".exs",
    ".erl",
    ".hs",
    ".clj", ".cljs",
    ".vue", ".svelte",
}
for _ext in _CODE_EXT:
    _EXT_REGISTRY[_ext] = ("code", "parsers.code", "parse")


def supported_extensions() -> List[str]:
    return sorted(_EXT_REGISTRY.keys())


def choose_parser(path: Path) -> Optional[Tuple[str, str, str]]:
    """Return (kind, module, fn) for `path`, or None if unsupported."""
    suffix = path.suffix.lower()
    if suffix in _EXT_REGISTRY:
        return _EXT_REGISTRY[suffix]

    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        if mime.startswith("text/html"):
            return _EXT_REGISTRY[".html"]
        if mime.startswith("application/pdf"):
            return _EXT_REGISTRY[".pdf"]
        if mime.startswith("text/"):
            return _EXT_REGISTRY[".txt"]
        if mime.startswith("image/"):
            return _EXT_REGISTRY[".png"]
        if mime.startswith("audio/") or mime.startswith("video/"):
            return _EXT_REGISTRY[".mp3"]
    return None


def parse_file(path: Path, *, parser: Optional[str] = None) -> ParseResult:
    """Dispatch to the right parser. Raises UnsupportedFile if nothing matches."""
    path = Path(path)
    if parser:
        module_path, fn_name = parser, "parse"
        kind = "override"
    else:
        chosen = choose_parser(path)
        if not chosen:
            raise UnsupportedFile(f"No parser for {path.suffix or path.name}")
        kind, module_path, fn_name = chosen

    import importlib

    mod = importlib.import_module(module_path)
    fn: ParserFn = getattr(mod, fn_name)
    result = fn(path)
    if not result.kind or result.kind == "unknown":
        result.kind = kind
    return result


class UnsupportedFile(Exception):
    """Raised when no parser matches the file."""
