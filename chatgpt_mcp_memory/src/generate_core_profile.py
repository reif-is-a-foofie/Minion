#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from llm import chat


DEFAULT_MODEL = "mistral:7b"


def _cap_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_code_fences(markdown: str) -> str:
    # Many models wrap markdown in ``` fences; we want a clean file.
    s = markdown.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
        s = re.sub(r"\n```$", "", s)
    return s.strip() + "\n"


@dataclass(frozen=True)
class Inputs:
    sourcebook_md: str
    quote_bank_md: str


def _load_inputs(derived_dir: Path, *, max_sourcebook_chars: int, max_quote_bank_chars: int) -> Inputs:
    sourcebook_path = derived_dir / "persona_sourcebook.md"
    quote_bank_path = derived_dir / "persona_quote_bank.md"
    if not sourcebook_path.exists() or not quote_bank_path.exists():
        missing = []
        if not sourcebook_path.exists():
            missing.append(str(sourcebook_path))
        if not quote_bank_path.exists():
            missing.append(str(quote_bank_path))
        raise FileNotFoundError(
            "Missing persona artifacts. Run:\n"
            f"  python src/persona_extract.py --export \"/path/to/export\" --derived-dir \"{derived_dir}\"\n"
            "Missing:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    return Inputs(
        sourcebook_md=_cap_text(_read_text(sourcebook_path), max_sourcebook_chars),
        quote_bank_md=_cap_text(_read_text(quote_bank_path), max_quote_bank_chars),
    )


def _build_prompt(inputs: Inputs) -> str:
    return (
        "You are generating a stable, always-on 'core profile' for an AI assistant.\n"
        "The profile MUST be derived ONLY from the provided evidence.\n"
        "\n"
        "Hard rules:\n"
        "- Do NOT guess. If a section is not supported by evidence, write 'Unknown'.\n"
        "- Do NOT invent names, employers, religions, family status, or personal facts.\n"
        "- Prefer generalizable phrasing over specific claims unless directly evidenced.\n"
        "- Output MUST be markdown with EXACT headings (keep them in this order):\n"
        "  1) # Core Profile\n"
        "  2) ## Identity\n"
        "  3) ## Values And Priorities\n"
        "  4) ## Communication Style\n"
        "  5) ## Working Style\n"
        "  6) ## Typical Deliverables\n"
        "  7) ## Constraints\n"
        "  8) ## Evidence\n"
        "\n"
        "Evidence section rules:\n"
        "- Include 8–20 short verbatim quotes.\n"
        "- Each quote MUST be copied from the evidence below.\n"
        "- If a quote has a 'Source conversation:' line, keep it.\n"
        "\n"
        "Tone/style rules for the profile:\n"
        "- Concise, concrete, pasteable.\n"
        "- Use bullet points.\n"
        "\n"
        "=== EVIDENCE: Persona Quote Bank (preferred) ===\n"
        f"{inputs.quote_bank_md}\n"
        "\n"
        "=== EVIDENCE: Persona Sourcebook (backup) ===\n"
        f"{inputs.sourcebook_md}\n"
    )


def _default_repo_root() -> Path:
    # .../minion/chatgpt_mcp_memory/src/generate_core_profile.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def main() -> None:
    repo_root = _default_repo_root()
    default_derived = Path(__file__).resolve().parents[1] / "data" / "derived"

    parser = argparse.ArgumentParser(
        description="Generate core_profile.md from ChatGPT export-derived persona evidence via local Ollama."
    )
    parser.add_argument(
        "--derived-dir",
        default=str(default_derived),
        help="Directory containing persona_sourcebook.md and persona_quote_bank.md",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name (default: mistral:7b)")
    parser.add_argument(
        "--max-sourcebook-chars",
        type=int,
        default=60_000,
        help="Max characters from persona_sourcebook.md to include",
    )
    parser.add_argument(
        "--max-quote-bank-chars",
        type=int,
        default=20_000,
        help="Max characters from persona_quote_bank.md to include",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Best-effort request timeout for Ollama client (optional)",
    )
    parser.add_argument(
        "--out-chatgpt-mcp",
        default=str(repo_root / "chatgpt_mcp_memory" / "core_profile.md"),
        help="Output path for chatgpt_mcp_memory/core_profile.md",
    )
    parser.add_argument(
        "--out-agent",
        default=str(repo_root / "agent" / "core_profile.md"),
        help="Output path for agent/core_profile.md",
    )
    args = parser.parse_args()

    derived_dir = Path(args.derived_dir).expanduser().resolve()
    inputs = _load_inputs(
        derived_dir,
        max_sourcebook_chars=int(args.max_sourcebook_chars),
        max_quote_bank_chars=int(args.max_quote_bank_chars),
    )
    prompt = _build_prompt(inputs)

    print(
        f"Calling Ollama ({args.model}) to write core_profile.md — one request, may take a few minutes…",
        flush=True,
    )

    llm_resp = chat(
        model=str(args.model),
        system="You follow instructions exactly and only use supplied evidence.",
        user=prompt,
        options={
            # Keep generation deterministic-ish and compact.
            "temperature": 0.2,
            "num_predict": 1200,
        },
        timeout_seconds=args.timeout_seconds,
    )
    markdown = llm_resp.content
    markdown = _strip_code_fences(markdown)

    out_chatgpt_mcp = Path(args.out_chatgpt_mcp).expanduser().resolve()
    out_agent = Path(args.out_agent).expanduser().resolve()
    out_chatgpt_mcp.parent.mkdir(parents=True, exist_ok=True)
    out_agent.parent.mkdir(parents=True, exist_ok=True)
    out_chatgpt_mcp.write_text(markdown, encoding="utf-8")
    out_agent.write_text(markdown, encoding="utf-8")

    # Build marker + manifest (lets other parts of the system know this exists and how it was produced)
    built_marker_path = derived_dir / "core_profile.built"
    manifest_path = derived_dir / "core_profile_manifest.json"
    built_marker_path.write_text("", encoding="utf-8")
    manifest = {
        "created_at_unix": time.time(),
        "model": str(args.model),
        "derived_dir": str(derived_dir),
        "inputs": {
            "max_sourcebook_chars": int(args.max_sourcebook_chars),
            "max_quote_bank_chars": int(args.max_quote_bank_chars),
        },
        "outputs": {
            "chatgpt_mcp_core_profile": str(out_chatgpt_mcp),
            "agent_core_profile": str(out_agent),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(str(out_chatgpt_mcp))
    print(str(out_agent))
    print(str(manifest_path))


if __name__ == "__main__":
    main()

