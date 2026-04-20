"""ChatGPT export parser.

Accepts either a .zip (OpenAI export) or a directory that already
contains `conversations-*.json`. Unzips to a sibling temp dir when
given a zip, then reuses the same message iterator as the legacy
`build_index.py` pipeline.
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


def _find_export_root(root: Path) -> Path:
    if list(root.glob("conversations-*.json")):
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("conversations-*.json")):
            return child
    for p in root.rglob("conversations-*.json"):
        return p.parent
    raise FileNotFoundError(f"No conversations-*.json under {root}")


def parse(path: Path) -> ParseResult:
    from chatgpt_export_reader import iter_messages  # type: ignore

    work_dir: Path
    tmp: tempfile.TemporaryDirectory | None = None

    if path.is_dir():
        work_dir = _find_export_root(path)
    elif path.suffix.lower() == ".zip":
        tmp = tempfile.TemporaryDirectory(prefix="minion_export_")
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(tmp.name)
        work_dir = _find_export_root(Path(tmp.name))
    else:
        raise ValueError(f"Unsupported ChatGPT export path: {path}")

    chunks: List[ParsedChunk] = []
    seq = 0
    try:
        for msg in iter_messages(str(work_dir), include_roles=("user",)):
            if not msg.text:
                continue
            for t in chunk_text(msg.text):
                chunks.append(
                    ParsedChunk(
                        text=t,
                        role=msg.role,
                        meta={
                            "seq": seq,
                            "conversation_id": msg.conversation_id,
                            "conversation_title": msg.conversation_title,
                            "create_time": msg.create_time,
                            "message_id": msg.message_id,
                        },
                    )
                )
                seq += 1
    finally:
        if tmp is not None:
            tmp.cleanup()

    return ParseResult(
        chunks=chunks,
        source_meta={"export_root": str(work_dir), "roles_indexed": ["user"]},
        kind="chatgpt-export",
        parser="chatgpt-export",
    )
