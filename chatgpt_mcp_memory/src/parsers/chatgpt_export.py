"""ChatGPT export parser.

Accepts either a .zip (OpenAI export) or a directory that already
contains `conversations-*.json`. When given a zip we selectively
extract only the JSON manifests -- ChatGPT exports ship DALL-E images
with 400+ character filenames that blow past macOS's 255-byte limit
(errno 63). We don't index those anyway.
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List

from . import ParsedChunk, ParseResult
from ._common import chunk_text


# The existing reader lives one dir up; use a path-based import so we don't
# need to package-ify the whole src tree.
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# We extract every JSON/HTML manifest in the export and skip everything
# else (DALL-E pngs, audio, dalle-generations/, user-*/). Modern exports
# chunk conversations across numbered files (`conversations-1.json`,
# `conversations-YYYY-MM-DD.json`, ...), so matching by extension rather
# than an exact allowlist keeps us forward-compatible.
_EXTRACT_EXTS = (".json", ".html")

# macOS HFS+/APFS caps basenames at 255 bytes; the DALL-E filenames that
# originally broke us are ~400 chars. A conservative guard catches them
# without false positives on any real manifest name.
_MAX_BASENAME_BYTES = 200


def _find_export_root(root: Path) -> Path:
    # Native OpenAI export: conversations*.json at root (or one level down).
    if list(root.glob("conversations*.json")):
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("conversations*.json")):
            return child
    for p in root.rglob("conversations*.json"):
        return p.parent

    # Per-conversation layout (third-party exporter): `<root>/json/YYYY-MM-DD_*.json`.
    # We return <root> itself; iter_conversation_json_paths knows to look in
    # the `json/` subfolder.
    per_conv = list(root.glob("json/[12][0-9][0-9][0-9]-*.json"))
    if per_conv:
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("json/[12][0-9][0-9][0-9]-*.json")):
            return child

    raise FileNotFoundError(
        f"No ChatGPT export manifests under {root} "
        "(expected conversations*.json or json/YYYY-MM-DD_*.json)"
    )


def _selective_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract every JSON/HTML manifest. Skip media + overlong names.

    Returns the number of files extracted.
    """
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        basename = Path(info.filename).name
        if not basename.lower().endswith(_EXTRACT_EXTS):
            continue
        if len(basename.encode("utf-8", "replace")) > _MAX_BASENAME_BYTES:
            continue
        target = dest / info.filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            count += 1
        except OSError:
            # Defensive: skip any lingering filesystem edge case without
            # aborting the whole extraction.
            continue
    return count


def _noop(_stage: str, _info: dict) -> None:
    pass


def parse(path: Path, *, on_progress=None) -> ParseResult:
    from chatgpt_export_reader import (  # type: ignore
        extract_text_from_parts,
        get_linear_path,
        load_conversations_from_export,
        normalize_text,
    )

    emit = on_progress or _noop
    work_dir: Path
    tmp: tempfile.TemporaryDirectory | None = None

    if path.is_dir():
        work_dir = _find_export_root(path)
    elif path.suffix.lower() == ".zip":
        emit("extract_start", {"path": str(path)})
        tmp = tempfile.TemporaryDirectory(prefix="minion_export_")
        with zipfile.ZipFile(path, "r") as zf:
            n = _selective_extract(zf, Path(tmp.name))
        emit("extract_done", {"files": n})
        if n == 0:
            raise ValueError(
                f"zip contains no ChatGPT export manifests ({_EXTRACT_ALLOW[0]} etc.)"
            )
        work_dir = _find_export_root(Path(tmp.name))
    else:
        raise ValueError(f"Unsupported ChatGPT export path: {path}")

    chunks: List[ParsedChunk] = []
    seq = 0
    try:
        emit("load_start", {"dir": str(work_dir)})
        conversations = load_conversations_from_export(str(work_dir))
        total_convs = len(conversations)
        emit("load_done", {"conversations": total_convs})

        messages_seen = 0
        for ci, conv in enumerate(conversations):
            mapping = conv.get("mapping", {}) or {}
            current_node = conv.get("current_node")
            if not mapping or not current_node:
                continue
            title = conv.get("title") or "(untitled)"
            conv_id = conv.get("id") or conv.get("conversation_id") or "unknown"

            for node_id in get_linear_path(mapping, current_node):
                node = mapping.get(node_id) or {}
                msg = node.get("message") or {}
                author = msg.get("author", {}) or {}
                role = author.get("role")
                if role != "user":
                    continue
                content = msg.get("content") or {}
                if content.get("content_type") != "text":
                    continue
                text = normalize_text(extract_text_from_parts(content.get("parts") or []))
                if not text:
                    continue
                messages_seen += 1
                for t in chunk_text(text):
                    chunks.append(
                        ParsedChunk(
                            text=t,
                            role=role,
                            meta={
                                "seq": seq,
                                "conversation_id": str(conv_id),
                                "conversation_title": str(title),
                                "create_time": msg.get("create_time"),
                                "message_id": str(msg.get("id") or node_id),
                            },
                        )
                    )
                    seq += 1

            if (ci + 1) % 25 == 0 or ci + 1 == total_convs:
                emit(
                    "parse_progress",
                    {
                        "conversations_done": ci + 1,
                        "conversations_total": total_convs,
                        "messages": messages_seen,
                        "chunks": len(chunks),
                    },
                )
    finally:
        if tmp is not None:
            tmp.cleanup()

    return ParseResult(
        chunks=chunks,
        source_meta={"export_root": str(work_dir), "roles_indexed": ["user"]},
        kind="chatgpt-export",
        parser="chatgpt-export",
    )
