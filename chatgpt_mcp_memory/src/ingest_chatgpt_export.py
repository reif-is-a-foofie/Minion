#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ExportFile:
    path: str
    size_bytes: int


def _safe_name(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum() or ch in ("-", "_", ".")).strip("._-") or "export"


def unzip_to(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def find_export_root(unzipped_dir: Path) -> Path:
    """
    ChatGPT exports sometimes unzip into a single top-level folder, sometimes into many files.
    We choose the directory that contains at least one conversations-*.json.
    """
    # First, check the unzipped dir itself
    if list(unzipped_dir.glob("conversations-*.json")):
        return unzipped_dir

    # Then, check one-level children
    for child in sorted(unzipped_dir.iterdir()):
        if child.is_dir() and list(child.glob("conversations-*.json")):
            return child

    # Finally, do a shallow recursive search up to depth 3
    for path in unzipped_dir.rglob("conversations-*.json"):
        return path.parent

    raise FileNotFoundError("Could not find conversations-*.json in the unzipped export.")


def build_export_manifest(export_root: Path) -> Dict:
    files: List[ExportFile] = []
    for p in sorted(export_root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(export_root))
            files.append(ExportFile(path=rel, size_bytes=p.stat().st_size))

    convo_files = sorted([str(p.relative_to(export_root)) for p in export_root.glob("conversations-*.json")])
    manifest = {
        "export_root": str(export_root),
        "created_at_unix": time.time(),
        "conversation_files": convo_files,
        "has_chat_html": (export_root / "chat.html").exists(),
        "export_files": [asdict(f) for f in files],
    }
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a ChatGPT export (ZIP or unzipped folder) into data/raw/"
    )
    parser.add_argument(
        "zip_path",
        help="ChatGPT export .zip from OpenAI, or a folder that contains it (including nested)",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "data" / "raw"),
        help="Output directory for unzipped export",
    )
    args = parser.parse_args()

    zip_path = Path(args.zip_path).expanduser().resolve()
    if not zip_path.exists():
        raise FileNotFoundError(str(zip_path))

    out_base = Path(args.out).expanduser().resolve()
    out_base.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    dest = out_base / f"{_safe_name(zip_path.stem)}_{stamp}"
    if dest.exists():
        raise FileExistsError(str(dest))

    if zip_path.is_dir():
        src_root = find_export_root(zip_path)
        shutil.copytree(src_root, dest)
    elif zip_path.is_file() and zip_path.suffix.lower() == ".zip":
        unzip_to(zip_path, dest)
    else:
        raise ValueError(
            "Expected a .zip file or a directory containing conversations-*.json (ChatGPT export)."
        )

    export_root = find_export_root(dest)

    manifest = build_export_manifest(export_root)
    manifest_path = export_root / "local_export_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(str(export_root))
    print(str(manifest_path))


if __name__ == "__main__":
    main()

