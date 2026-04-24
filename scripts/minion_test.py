#!/usr/bin/env python3
"""
Unified test entrypoint for Minion.

Usage (from repo root, with dev deps installed — see AGENTS.md):

  python scripts/minion_test.py core          # default: Python tests + live sidecar smoke
  python scripts/minion_test.py desktop       # SvelteKit typecheck
  python scripts/minion_test.py all           # core, then desktop
  python scripts/minion_test.py mcp-eval --derived-dir ~/path/to/derived
  python scripts/minion_test.py doctor        # quick import / binary sanity checks

Pytest forwards: anything after ``--`` goes to pytest, e.g.
  python scripts/minion_test.py core -- -k status_ready
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_RUN = REPO_ROOT / "eval" / "run_eval.py"
DESKTOP_DIR = REPO_ROOT / "desktop"


def _python() -> str:
    return os.environ.get("MINION_PYTHON") or sys.executable


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd, cwd=cwd or REPO_ROOT, env=env)


def cmd_doctor() -> int:
    py = _python()
    code = (
        "import importlib.util as u\n"
        "mods = ('pytest','httpx','websockets','fastapi','uvicorn')\n"
        "bad = [m for m in mods if u.find_spec(m) is None]\n"
        "print('python:', __import__('sys').executable)\n"
        "print('missing:', bad or 'none')\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    r = subprocess.run([py, "-c", code], cwd=REPO_ROOT)
    if r.returncode != 0:
        print(
            "Install chatgpt_mcp_memory deps, e.g.\n"
            "  cd chatgpt_mcp_memory && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt",
            file=sys.stderr,
        )
    return r.returncode


def cmd_core(forward: list[str]) -> int:
    cmd = [_python(), "-m", "pytest", "chatgpt_mcp_memory/tests"]
    if not any(f in ("-q", "-qq", "-v", "-vv", "-vvv") for f in forward):
        cmd.append("-q")
    cmd.extend(forward)
    return _run(cmd)


def cmd_desktop() -> int:
    npm = shutil.which("npm")
    if not npm:
        print("npm not on PATH", file=sys.stderr)
        return 127
    if not (DESKTOP_DIR / "package.json").exists():
        print(f"missing {DESKTOP_DIR / 'package.json'}", file=sys.stderr)
        return 2
    r = _run([npm, "ci"], cwd=DESKTOP_DIR)
    if r != 0:
        return r
    return _run([npm, "run", "check"], cwd=DESKTOP_DIR)


def cmd_desktop_quick() -> int:
    """Assume node_modules present (local dev)."""
    npm = shutil.which("npm")
    if not npm:
        print("npm not on PATH", file=sys.stderr)
        return 127
    return _run([npm, "run", "check"], cwd=DESKTOP_DIR)


def cmd_mcp_eval(derived: Path | None, golden: Path | None, json_out: bool, forward: list[str]) -> int:
    raw = os.environ.get("MINION_DERIVED_DIR") if derived is None else str(derived)
    if not raw:
        print("mcp-eval needs --derived-dir or MINION_DERIVED_DIR", file=sys.stderr)
        return 2
    d = Path(raw).expanduser().resolve()
    cmd = [_python(), str(EVAL_RUN), "--derived-dir", str(d)]
    if golden:
        cmd.extend(["--golden", str(golden)])
    if json_out:
        cmd.append("--json")
    cmd.extend(forward)
    return _run(cmd)


def cmd_all(desktop_mode: str, pytest_forward: list[str]) -> int:
    r = cmd_core(pytest_forward)
    if r != 0:
        return r
    if desktop_mode == "ci":
        return cmd_desktop()
    return cmd_desktop_quick()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Minion unified test runner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="verify Python test dependencies")
    p_doc.set_defaults(func=lambda a: cmd_doctor())

    p_core = sub.add_parser("core", help="pytest chatgpt_mcp_memory/tests (sidecar smoke + units)")
    p_core.set_defaults(func=lambda a: cmd_core(a.pytest_forward))

    p_desk = sub.add_parser("desktop", help="npm ci + npm run check in desktop/")
    p_desk.set_defaults(func=lambda a: cmd_desktop())

    p_desk_q = sub.add_parser("desktop-quick", help="npm run check only (node_modules must exist)")
    p_desk_q.set_defaults(func=lambda a: cmd_desktop_quick())

    p_eval = sub.add_parser("mcp-eval", help="run eval/run_eval.py golden harness (needs derived dir)")
    p_eval.add_argument("--derived-dir", default=None, help="override MINION_DERIVED_DIR")
    p_eval.add_argument("--golden", default=None, help="golden_queries.yaml path")
    p_eval.add_argument("--json", action="store_true", help="JSON report on stdout")
    p_eval.set_defaults(func=lambda a: cmd_mcp_eval(a.derived_dir, a.golden, a.json, a.forward))

    p_all = sub.add_parser("all", help="core then desktop-quick (use --ci for npm ci)")
    p_all.add_argument("--ci", action="store_true", help="run full npm ci + check like CI")
    p_all.set_defaults(func=lambda a: cmd_all("ci" if a.ci else "quick", a.pytest_forward))

    # Parse: allow `core -- -k foo` by pre-splitting on `--`
    if "--" in argv:
        idx = argv.index("--")
        main_args, pytest_forward = argv[:idx], argv[idx + 1 :]
    else:
        main_args, pytest_forward = argv, []

    args, unknown = ap.parse_known_args(main_args)
    if args.cmd == "mcp-eval":
        args.forward = unknown
        args.pytest_forward = []
    elif args.cmd in ("core", "all"):
        args.pytest_forward = pytest_forward + unknown
    else:
        if unknown:
            ap.error(f"unexpected arguments: {unknown}")
        if pytest_forward:
            ap.error("extra pytest args only apply to core / all (put them after --)")
        args.pytest_forward = []

    if args.cmd == "mcp-eval":
        args.golden = Path(args.golden).resolve() if args.golden else None
        args.derived_dir = Path(args.derived_dir).resolve() if args.derived_dir else None

    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
