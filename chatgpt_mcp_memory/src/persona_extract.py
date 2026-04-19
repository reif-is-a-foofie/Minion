#!/usr/bin/env python3
import argparse
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from chatgpt_export_reader import ChatMessage, iter_messages, normalize_text


def split_sentences(text: str) -> List[str]:
    text = text.replace("\n", " ")
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip(" -") for part in parts if part.strip(" -")]


def looks_like_noise(text: str) -> bool:
    lowered = text.lower()
    noisy_markers = [
        "traceback",
        "localhost:",
        "request payload",
        "response status",
        "response headers",
        "payload json",
        "cli launched successfully",
        "server url:",
        "total tokens:",
        "api called with tokens",
        "```",
        "~/",
        "╔",
        "════════",
        "process exited",
    ]
    if any(marker in lowered for marker in noisy_markers):
        return True
    if len(text) > 4000:
        return True
    if (text.count("\n") + 1) > 35:
        return True
    return False


def is_first_person_statement(text: str) -> bool:
    lowered = text.lower()
    first_person_markers = [
        " i ",
        " i'm",
        "i'm ",
        " i’m",
        "my ",
        "mine ",
        "me ",
        "myself",
    ]
    padded = f" {lowered} "
    return any(marker in padded for marker in first_person_markers)


def bucket_text(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["i am", "i'm", "my name", "about me", "who i am"]):
        return "Identity"
    if any(word in lowered for word in ["i want", "my goal", "i'm trying", "i need to", "i hope to"]):
        return "Goals"
    if any(word in lowered for word in ["i like", "i prefer", "i love", "i hate", "i don't like"]):
        return "Preferences"
    if any(word in lowered for word in ["believe", "value", "important to me", "principle", "conviction"]):
        return "Values"
    if any(word in lowered for word in ["project", "business", "company", "client", "dashboard", "build", "working on"]):
        return "Projects"
    if any(word in lowered for word in ["write", "tone", "voice", "style", "wording"]):
        return "Writing Style"
    return "Raw Statements"


def sentence_is_quote_candidate(sentence: str) -> bool:
    sentence = sentence.strip()
    if looks_like_noise(sentence):
        return False
    if len(sentence) < 24 or len(sentence) > 280:
        return False
    if "http" in sentence.lower():
        return False
    if sentence.count(",") > 6:
        return False
    if not is_first_person_statement(sentence):
        return False
    lowered = sentence.lower().strip()
    bad_starts = [
        "can you",
        "could you",
        "please",
        "what ",
        "why ",
        "how ",
        "pull ",
        "make ",
        "update ",
        "use ",
        "help ",
    ]
    if any(lowered.startswith(prefix) for prefix in bad_starts):
        return False
    return True


def render_sourcebook(messages: List[Dict], buckets: Dict[str, List[Dict]], counts: Counter) -> str:
    lines: List[str] = []
    lines.append("# Persona Sourcebook")
    lines.append("")
    lines.append("This file uses only user-authored text taken from the ChatGPT export.")
    lines.append("It is intended as raw first-person persona material, not a paraphrased summary.")
    lines.append("")
    lines.append(f"- Total user messages extracted: {len(messages)}")
    lines.append(f"- Unique user messages after exact dedupe: {len(counts)}")
    lines.append("")

    preferred_order = [
        "Identity",
        "Goals",
        "Preferences",
        "Values",
        "Projects",
        "Writing Style",
        "Raw Statements",
    ]

    for bucket in preferred_order:
        items = buckets.get(bucket) or []
        if not items:
            continue
        lines.append(f"## {bucket}")
        lines.append("")
        for item in items:
            text = item["text"]
            title = item["conversation_title"]
            count = counts[text]
            lines.append(f"> {text.replace(chr(10), chr(10) + '> ')}")
            lines.append("")
            lines.append(f"Source conversation: {title}")
            if count > 1:
                lines.append(f"Repeated exact message count: {count}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_quote_bank(messages: List[Dict]) -> str:
    seen = set()
    buckets: Dict[str, List[Dict]] = {}
    for msg in messages:
        for sentence in split_sentences(msg["text"]):
            sentence = normalize_text(sentence)
            if not sentence_is_quote_candidate(sentence):
                continue
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            bucket = bucket_text(sentence)
            buckets.setdefault(bucket, []).append({"text": sentence, "conversation_title": msg["conversation_title"]})

    lines: List[str] = []
    lines.append("# Persona Quote Bank")
    lines.append("")
    lines.append("Short first-person lines pulled verbatim from user messages.")
    lines.append("")
    for bucket in ["Identity", "Goals", "Preferences", "Values", "Projects", "Writing Style", "Raw Statements"]:
        items = buckets.get(bucket, [])
        if not items:
            continue
        lines.append(f"## {bucket}")
        lines.append("")
        for item in items[:120]:
            lines.append(f"- \"{item['text']}\"")
            lines.append(f"  Source conversation: {item['conversation_title']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract persona artifacts from a ChatGPT export directory.")
    parser.add_argument(
        "--export",
        help="Path to unzipped ChatGPT export root (contains conversations-*.json).",
    )
    parser.add_argument(
        "--chunks",
        help="Alternative input: path to chunks.jsonl (as produced by build_index.py). Useful if raw export is unavailable.",
    )
    parser.add_argument(
        "--derived-dir",
        default=str(Path(__file__).resolve().parents[1] / "data" / "derived"),
        help="Where to write persona_sourcebook.md and persona_quote_bank.md",
    )
    parser.add_argument("--max-per-bucket", type=int, default=40)
    args = parser.parse_args()

    derived_dir = Path(args.derived_dir).expanduser().resolve()
    derived_dir.mkdir(parents=True, exist_ok=True)

    raw_messages: List[Dict] = []
    if args.chunks:
        chunks_path = Path(args.chunks).expanduser().resolve()
        print(f"Reading user chunks from {chunks_path.name}…", flush=True)
        n_lines = 0
        with open(chunks_path, "r", encoding="utf-8") as fh:
            for line in fh:
                n_lines += 1
                if n_lines % 200_000 == 0:
                    print(f"  … read {n_lines} lines from chunks file", flush=True)
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if item.get("role") != "user":
                    continue
                text = normalize_text(str(item.get("text") or ""))
                if not text:
                    continue
                raw_messages.append(
                    {
                        "conversation_title": item.get("conversation_title") or "(unknown)",
                        "conversation_id": item.get("conversation_id") or "unknown",
                        "create_time": item.get("create_time"),
                        "text": text,
                    }
                )
        print(f"Finished reading chunks file: {n_lines} lines.", flush=True)
    elif args.export:
        export_dir = str(Path(args.export).expanduser().resolve())
        print("Scanning export for user messages (this can take a few minutes on large exports)…", flush=True)
        n = 0
        for msg in iter_messages(export_dir, include_roles=("user",)):
            n += 1
            if n % 100_000 == 0:
                print(f"  … scanned {n} user messages", flush=True)
            if msg.content_type != "text" or not msg.text:
                continue
            raw_messages.append(
                {
                    "conversation_title": msg.conversation_title,
                    "conversation_id": msg.conversation_id,
                    "create_time": msg.create_time,
                    "text": msg.text,
                }
            )
        print(f"Finished scan: {n} user messages read.", flush=True)
    else:
        raise SystemExit("Provide --export or --chunks")

    counts: Counter = Counter()
    dedup: Dict[str, Dict] = {}
    for m in raw_messages:
        counts[m["text"]] += 1
        if m["text"] not in dedup:
            dedup[m["text"]] = m

    # Choose persona-relevant (first-person, not noisy), bucket and sort
    buckets: Dict[str, List[Dict]] = {}
    for text, m in dedup.items():
        if looks_like_noise(text):
            continue
        if not is_first_person_statement(text):
            continue
        bucket = bucket_text(text)
        buckets.setdefault(bucket, []).append(m)

    for bucket, items in buckets.items():
        items.sort(key=lambda x: (-counts[x["text"]], -len(x["text"]), x["conversation_title"].lower()))
        buckets[bucket] = items[: args.max_per_bucket]

    sourcebook = render_sourcebook(raw_messages, buckets, counts)
    quote_bank = build_quote_bank(raw_messages)

    sourcebook_path = derived_dir / "persona_sourcebook.md"
    quote_bank_path = derived_dir / "persona_quote_bank.md"
    sourcebook_path.write_text(sourcebook, encoding="utf-8")
    quote_bank_path.write_text(quote_bank, encoding="utf-8")

    print(str(sourcebook_path))
    print(str(quote_bank_path))
    print(f"user_messages={len(raw_messages)} unique_messages={len(counts)}")


if __name__ == "__main__":
    main()

