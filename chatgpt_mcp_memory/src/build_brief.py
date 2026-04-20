#!/usr/bin/env python3
"""
Build a condensed user brief from chunks.jsonl.

Output: <derived>/brief.md — capped at MINION_BRIEF_MAX_CHARS (default 4000).

The MCP server auto-attaches this to the first tool-call of each Claude session.
No Ollama / no network calls: pure stdlib regex + counting. When richer
Ollama-synthesized profiles exist (core_profile.md / identity_profile.md), the
server prefers those — this file is the zero-dependency fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# --- Preference extraction ---------------------------------------------------

_PREF_VERBS = (
    r"prefer|always|never|hate|love|want|need|usually|tend to|try to|"
    r"do not|don't|won't|cannot|can't|must|should"
)
_PREF_RE = re.compile(rf"\bI\s+(?:{_PREF_VERBS})\b[^.?!\n]{{3,220}}[.?!]?", re.IGNORECASE)

# Drop lines that are mostly code/markup or very terse.
_NOISE_RE = re.compile(r"[`{}<>|\[\]]{2,}|https?://|\.py\b|\.md\b")

# --- Name extraction ---------------------------------------------------------

# 1-2 word Title-cased tokens (e.g. "Maia", "Bob Gay").
_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,1})\b")

# Filter out sentence-starters, common English words that happen to Title-case,
# and survey/template boilerplate that dominates raw frequency counts.
_STOPWORDS = frozenset(
    """
    The A An And Or But So If Then When Where Why How What Who Which That This These Those
    Yes No Not Very Only Even Just More Less Most Least Some Any All Every Each Other
    Can Could Will Would Should May Might Must Shall Do Does Did Done Have Has Had
    Is Are Was Were Be Been Being Am
    I Me My We Our You Your He She They Them His Her Their It Its
    Monday Tuesday Wednesday Thursday Friday Saturday Sunday
    January February March April May June July August September October November December
    English Spanish French German Chinese Japanese Russian American European Asian
    Google Apple Microsoft Amazon OpenAI Claude ChatGPT GPT
    Please Thanks Thank Sure Okay Hello Hi Hey
    Yes No Maybe Sometimes Often Rarely Always Never
    Now Today Tomorrow Yesterday Later Soon Next Last First Second Third
    Good Great Best Better Bad Worse Worst New Old
    One Two Three Four Five Six Seven Eight Nine Ten
    Yeah Yep Nope Okay Alright Well Also Still Yet Too
    Here There Above Below Inside Outside Everywhere Somewhere Nowhere
    Question Questions Answer Answers Reply Replies Context
    Poll Polls Card Cards Form Forms Rate Rates Rating
    Lot Lots Buy Sale Sales Cost Costs Price Prices Free Paid
    Available Loading Users User Pros Cons Text Image Link
    Year Month Week Day Hour Minute Second
    Brand Type Basic Standard Description Title Name
    Hi Hello Hey Dear Thanks Regards Sincerely
    Okay OK Step Steps Note Notes Example Examples
    Yes No Maybe Please Need Needs Want Wants
    Rate How Form What Year Sale Poll Do Poll Which
    For Get Use Contact Life Project About Home Help Services News Info
    Welcome Overview Summary Search Find Create Build Make Open Close
    See Read Know Think Feel Look Take Give Put Set Let Try Call Show
    Here There Back Front Down Up Over Under Around Through
    Hours About Us Pre Owned Certified Shop Terms Privacy Careers
    Go Come Keep Run Walk Sit Stand Turn
    Like Type Kind Way Side Part Piece Thing Stuff Item
    """.split()
)

# --- Framework / phrase extraction -------------------------------------------

# Repeated Title-cased 2-4 word phrases (e.g. "ENFP Life Framework", "Octopus Day").
_PHRASE_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9]{1,}\s+){1,3}[A-Z][A-Za-z0-9]{1,}\b")


def _iter_chunks(chunks_path: Path) -> Iterable[Dict]:
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _normalize_preference(s: str) -> str:
    # Case- and whitespace-fold for dedup key; keep the first raw variant for display.
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".?!")


# Per-conversation dedupe is the single most important anti-noise lever: a massive
# survey/card-template conversation can't inflate a phrase's count beyond 1, so
# signal-bearing items that appear in *many* distinct conversations surface.


def _extract_preferences(user_chunks: List[Dict], limit: int = 15) -> List[Tuple[str, int]]:
    # Count distinct conversations per preference.
    seen_per_conv: Dict[str, set] = {}
    first_seen: Dict[str, str] = {}
    for ch in user_chunks:
        t = ch.get("text") or ""
        if not t or _NOISE_RE.search(t):
            continue
        conv_id = ch.get("conversation_id") or ""
        for m in _PREF_RE.finditer(t):
            raw = m.group(0).strip()
            if len(raw) < 8 or len(raw) > 200:
                continue
            # Tighter quality: require the match to end cleanly (avoid dangling parens).
            if raw.count("(") != raw.count(")"):
                continue
            key = _normalize_preference(raw)
            seen_per_conv.setdefault(key, set()).add(conv_id)
            first_seen.setdefault(key, raw)
    ranked = sorted(seen_per_conv.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [(first_seen[k], len(cs)) for k, cs in ranked if len(cs) >= 2][:limit]


def _extract_names(
    user_chunks: List[Dict], limit: int = 15
) -> List[Tuple[str, int, str]]:
    seen_per_conv: Dict[str, set] = {}
    example_title: Dict[str, str] = {}
    for ch in user_chunks:
        text = ch.get("text") or ""
        if not text:
            continue
        conv_id = ch.get("conversation_id") or ""
        title = ch.get("conversation_title") or ""
        sentences = re.split(r"(?<=[.!?\n])\s+", text)
        for sent in sentences:
            tokens = sent.strip()
            if not tokens:
                continue
            # Strip first token to eliminate sentence-start Title-case false positives.
            rest = re.sub(r"^\S+\s*", "", tokens)
            for m in _NAME_RE.finditer(rest):
                name = m.group(1)
                parts = name.split()
                if any(p in _STOPWORDS for p in parts):
                    continue
                seen_per_conv.setdefault(name, set()).add(conv_id)
                example_title.setdefault(name, title)
    ranked = sorted(seen_per_conv.items(), key=lambda kv: len(kv[1]), reverse=True)
    # Require presence in >=3 distinct conversations — real names/projects do; template noise usually doesn't.
    out = [(n, len(cs), example_title.get(n, "")) for n, cs in ranked if len(cs) >= 3]
    return out[:limit]


def _extract_frameworks(
    chunks_with_conv: List[Tuple[str, str]], limit: int = 10
) -> List[Tuple[str, int]]:
    seen_per_conv: Dict[str, set] = {}
    for conv_id, text in chunks_with_conv:
        if not text:
            continue
        for m in _PHRASE_RE.finditer(text):
            phrase = m.group(0).strip()
            # Reject fragments scraped across line breaks (dealer pages etc.).
            if "\n" in phrase or "\t" in phrase:
                continue
            words = phrase.split()
            if len(words) < 2 or len(words) > 4:
                continue
            if any(w in _STOPWORDS for w in words):
                continue
            seen_per_conv.setdefault(phrase, set()).add(conv_id)
    ranked = sorted(seen_per_conv.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [(p, len(cs)) for p, cs in ranked if len(cs) >= 3][:limit]


def _render(
    n_chunks: int,
    prefs: List[Tuple[str, int]],
    names: List[Tuple[str, int, str]],
    frameworks: List[Tuple[str, int]],
) -> str:
    lines: List[str] = []
    lines.append("# Minion profile brief")
    lines.append("")
    lines.append(
        "_Auto-generated grounding for Claude. Reflects observed patterns in the user's "
        "past conversations; treat as priors, not assertions._"
    )
    lines.append("")

    lines.append("## Preferences (stated)")
    if prefs:
        for raw, count in prefs:
            tag = f" _(x{count})_" if count > 1 else ""
            lines.append(f"- {raw}{tag}")
    else:
        lines.append("- _(none confidently extracted)_")
    lines.append("")

    lines.append("## People & projects (most referenced)")
    if names:
        for name, count, title in names:
            ctx = f" — e.g. *{title}*" if title else ""
            lines.append(f"- **{name}** (x{count}){ctx}")
    else:
        lines.append("- _(none met threshold)_")
    lines.append("")

    lines.append("## Recurring frameworks / phrases")
    if frameworks:
        for phrase, count in frameworks:
            lines.append(f"- {phrase} (x{count})")
    else:
        lines.append("- _(none met threshold)_")
    lines.append("")

    lines.append(
        f"_Generated from {n_chunks} chunks on {datetime.utcnow().strftime('%Y-%m-%d')}._"
    )
    return "\n".join(lines) + "\n"


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Trim lines until under cap, keep header + footer.
    lines = text.splitlines()
    while len("\n".join(lines)) > max_chars and len(lines) > 5:
        # Drop from the middle (keep header & footer intact).
        lines.pop(len(lines) // 2)
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 30].rstrip() + "\n\n_…truncated…_\n"
    return out


def build_brief(derived_dir: Path, max_chars: int) -> Path:
    chunks_path = derived_dir / "chunks.jsonl"
    if not chunks_path.is_file():
        raise FileNotFoundError(f"Missing chunks.jsonl: {chunks_path}")

    user_chunks: List[Dict] = []
    all_pairs: List[Tuple[str, str]] = []  # (conversation_id, text)
    n_total = 0
    for ch in _iter_chunks(chunks_path):
        n_total += 1
        text = ch.get("text") or ""
        conv_id = ch.get("conversation_id") or ""
        all_pairs.append((conv_id, text))
        if (ch.get("role") or "").lower() == "user":
            user_chunks.append(ch)

    prefs = _extract_preferences(user_chunks)
    names = _extract_names(user_chunks)
    frameworks = _extract_frameworks(all_pairs)

    out = _render(n_total, prefs, names, frameworks)
    out = _cap(out, max_chars)

    brief_path = derived_dir / "brief.md"
    brief_path.write_text(out, encoding="utf-8")
    return brief_path


def _max_chars_from_env(default: int = 4000) -> int:
    raw = os.environ.get("MINION_BRIEF_MAX_CHARS", str(default))
    try:
        return max(500, int(raw))
    except ValueError:
        return default


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a condensed user brief from chunks.jsonl")
    ap.add_argument("--derived-dir", required=True, help="Directory containing chunks.jsonl")
    ap.add_argument(
        "--max-chars",
        type=int,
        default=_max_chars_from_env(),
        help="Hard cap on brief size (default 4000, env MINION_BRIEF_MAX_CHARS)",
    )
    args = ap.parse_args(argv)

    derived = Path(args.derived_dir).expanduser().resolve()
    brief_path = build_brief(derived, args.max_chars)
    print(f"Wrote {brief_path} ({brief_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
