#!/usr/bin/env python3
"""Emit Tauri v2 static updater manifest (latest.json) for GitHub Releases.

Example (after `tauri build` with TAURI_SIGNING_PRIVATE_KEY_* set):

  python3 scripts/write_latest_json.py \\
    --version 1.0.1 \\
    --notes "Bug fixes" \\
    --darwin-aarch64-url "https://github.com/org/repo/releases/download/v1.0.1/Minion_1.0.1_aarch64.app.tar.gz" \\
    --darwin-aarch64-sig path/to/Minion_1.0.1_aarch64.app.tar.gz.sig \\
    --darwin-x86_64-url "https://github.com/org/repo/releases/download/v1.0.1/Minion_1.0.1_x64.app.tar.gz" \\
    --darwin-x86_64-sig path/to/Minion_1.0.1_x64.app.tar.gz.sig \\
    > latest.json

Attach `latest.json` plus each `.tar.gz` to the GitHub release. The app’s
`tauri.conf.json` `plugins.updater.endpoints` must point at this file
(typically `.../releases/latest/download/latest.json`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict


def _read_sig(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", required=True, help="Semver shown to the updater")
    p.add_argument("--notes", default="", help="Release notes / changelog snippet")
    p.add_argument("--darwin-aarch64-url", dest="daa_url")
    p.add_argument("--darwin-aarch64-sig", dest="daa_sig", type=Path)
    p.add_argument("--darwin-x86_64-url", dest="dx64_url")
    p.add_argument("--darwin-x86_64-sig", dest="dx64_sig", type=Path)
    args = p.parse_args()

    platforms: Dict[str, Dict[str, str]] = {}
    if args.daa_url and args.daa_sig:
        platforms["darwin-aarch64"] = {
            "url": args.daa_url,
            "signature": _read_sig(args.daa_sig),
        }
    if args.dx64_url and args.dx64_sig:
        platforms["darwin-x86_64"] = {
            "url": args.dx64_url,
            "signature": _read_sig(args.dx64_sig),
        }
    if not platforms:
        raise SystemExit("Provide at least one complete darwin-* url+sig pair")

    out: Dict[str, Any] = {
        "version": args.version,
        "notes": args.notes,
        "pub_date": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "platforms": platforms,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
