#!/usr/bin/env python3
import argparse
import hashlib
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


def _short_dest_dir_name(zip_path: Path, stamp: str) -> str:
    """Avoid very long paths (macOS ENAMETOOLONG) from OpenAI’s huge zip basename."""
    h = hashlib.sha256(zip_path.name.encode("utf-8")).hexdigest()[:12]
    return f"export_{h}_{stamp}"


_MAX_COMPONENT = 120


def _shorten_component(name: str) -> str:
    if len(name) <= _MAX_COMPONENT:
        return name
    stem, ext = os.path.splitext(name)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    keep = max(0, _MAX_COMPONENT - len(ext) - 1 - len(digest))
    prefix = stem[:keep] if keep else ""
    return f"{prefix}~{digest}{ext}"


def unzip_to(zip_path: Path, dest_dir: Path) -> None:
    """
    Extract zip member-by-member. Shorten path components that would exceed typical
    filesystem limits (ChatGPT exports can include extremely long image filenames).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        total = len(members)
        print(f"Unzipping {total} files (large exports take several minutes)…", flush=True)
        for idx, info in enumerate(members, start=1):
            if idx % 2500 == 0 or idx == total:
                print(f"  … {idx}/{total} files extracted", flush=True)
            parts = info.filename.replace("\\", "/").split("/")
            safe_parts = [_shorten_component(p) for p in parts if p]
            rel = "/".join(safe_parts)
            dest_root = dest_dir.resolve()
            target = (dest_dir / rel).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                target = dest_dir / (
                    hashlib.sha256(info.filename.encode("utf-8")).hexdigest()[:24] + Path(info.filename).suffix
                )
                target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info, "r") as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except OSError:
                flat = dest_dir / (
                    hashlib.sha256(info.filename.encode("utf-8")).hexdigest()[:24] + Path(info.filename).suffix
                )
                flat.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(flat, "wb") as out:
                    shutil.copyfileobj(src, out)


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
    dest = out_base / _short_dest_dir_name(zip_path, stamp)
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

    # Sentinel-delimited result block. bin/minion parses between these markers
    # so we never mistake a late-flushed progress line for a path.
    print("===INGEST_RESULT_BEGIN===", flush=True)
    print(str(export_root), flush=True)
    print(str(manifest_path), flush=True)
    print("===INGEST_RESULT_END===", flush=True)


if __name__ == "__main__":
    main()

