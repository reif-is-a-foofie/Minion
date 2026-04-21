import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ChatMessage:
    conversation_id: str
    conversation_title: str
    message_id: str
    role: str
    create_time: Optional[float]
    content_type: str
    text: str


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_text_from_parts(parts: Sequence[Any]) -> str:
    out: List[str] = []
    for part in parts:
        if isinstance(part, str):
            s = part.strip()
            if s:
                out.append(s)
    return "\n".join(out).strip()


def get_linear_path(mapping: Dict[str, Any], current_node: str) -> List[str]:
    path: List[str] = []
    node_id: Optional[str] = current_node
    while node_id:
        node = mapping.get(node_id)
        if not node:
            break
        path.append(node_id)
        node_id = node.get("parent")
    return list(reversed(path))


def iter_conversation_json_paths(export_dir: str) -> List[str]:
    # ChatGPT export shapes we accept:
    #   * classic single-file `conversations.json`
    #   * chunked OpenAI exports: `conversations-<n>.json` / `conversations-YYYY-MM-DD.json`
    #   * third-party per-conversation exporters: one file per chat in a
    #     `json/` subfolder, filenames `YYYY-MM-DD_<slug>_<hash>.json`
    # Dedup because some exports include multiple shapes.
    paths = set(glob.glob(os.path.join(export_dir, "conversations.json")))
    paths.update(glob.glob(os.path.join(export_dir, "conversations-*.json")))
    if not paths:
        # Per-conversation layout: `<root>/json/YYYY-MM-DD_*.json`
        per_conv = glob.glob(os.path.join(export_dir, "json", "[12][0-9][0-9][0-9]-*.json"))
        paths.update(per_conv)
    return sorted(paths)


def load_conversations_from_export(export_dir: str) -> List[Dict[str, Any]]:
    conversations: List[Dict[str, Any]] = []
    for path in iter_conversation_json_paths(export_dir):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            conversations.extend(data)
        else:
            conversations.append(data)
    return conversations


def iter_messages(
    export_dir: str,
    *,
    include_roles: Sequence[str] = ("user",),
) -> Iterator[ChatMessage]:
    include_roles_set = set(include_roles)
    conversations = load_conversations_from_export(export_dir)

    for conv in conversations:
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
            if role not in include_roles_set:
                continue

            content = msg.get("content") or {}
            content_type = content.get("content_type") or "unknown"
            text = ""
            if content_type == "text":
                text = extract_text_from_parts(content.get("parts") or [])
                text = normalize_text(text)

            yield ChatMessage(
                conversation_id=str(conv_id),
                conversation_title=str(title),
                message_id=str(msg.get("id") or node_id),
                role=str(role),
                create_time=msg.get("create_time"),
                content_type=str(content_type),
                text=text,
            )


def chunk_text(text: str, *, max_chars: int = 1200) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current and current_len + para_len + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len + (2 if current_len else 0)

    if current:
        chunks.append("\n\n".join(current))

    final_chunks: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final_chunks.append(chunk)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        cur: List[str] = []
        cur_len = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if cur and cur_len + len(sentence) + 1 > max_chars:
                final_chunks.append(" ".join(cur))
                cur = [sentence]
                cur_len = len(sentence)
            else:
                cur.append(sentence)
                cur_len += len(sentence) + (1 if cur_len else 0)
        if cur:
            final_chunks.append(" ".join(cur))

    return [c for c in final_chunks if c.strip()]

