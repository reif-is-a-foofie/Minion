"""
Pytest wrapper around eval/run_eval.py.

Point the harness at a derived dir via one of:
    - MINION_DERIVED_DIR env var
    - pytest --derived-dir <path> CLI flag (registered in conftest.py)

Skipped gracefully when no derived dir is available, so CI passes on machines
without an indexed export.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_SRC = HERE.parent / "chatgpt_mcp_memory" / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

from run_eval import (  # noqa: E402
    DEFAULT_SERVER_PY,
    MCPStdioClient,
    run_case,
)


def _resolve_derived_dir(request) -> Path | None:
    raw = request.config.getoption("--derived-dir") or os.environ.get("MINION_DERIVED_DIR")
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.exists() else None


@pytest.fixture(scope="session")
def mcp_client(request):
    derived = _resolve_derived_dir(request)
    if derived is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR — skipping MCP golden tests.")

    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")

    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    try:
        client.initialize()
        yield client
    finally:
        client.stop()


def test_golden_query(mcp_client, case):
    result = run_case(mcp_client, case, top_k_override=None)
    assert result.error is None, f"MCP error on {case.get('query')!r}: {result.error}"
    assert result.passed, (
        f"Query {case.get('query')!r} missed all expectations. "
        f"Top hit: {result.hits[0] if result.hits else None}"
    )


def test_profile_brief_first_call_only(request):
    """First tools/call carries structuredContent.profile_brief; second does not."""
    derived = _resolve_derived_dir(request)
    if derived is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR — skipping brief-injection test.")
    if not (derived / "brief.md").exists() and not (derived / "core_profile.md").exists():
        pytest.skip("No brief.md / core_profile.md in derived dir — nothing to inject.")
    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")

    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    try:
        client.initialize()
        first = client.call_tool(
            "ask_minion", {"query": "sanity probe", "top_k": 1, "max_chars": 80}
        )
        second = client.call_tool(
            "ask_minion", {"query": "another probe", "top_k": 1, "max_chars": 80}
        )
    finally:
        client.stop()

    sc1 = first.get("structuredContent") or {}
    sc2 = second.get("structuredContent") or {}
    assert isinstance(sc1, dict), f"first call structuredContent not a dict: {sc1!r}"
    assert "profile_brief" in sc1, "first call missing profile_brief"
    assert len(sc1["profile_brief"]) > 50, "profile_brief suspiciously short"
    assert "profile_brief" not in sc2, "profile_brief leaked into second call"


def test_temporal_oldest_user_message(mcp_client):
    """mode='oldest' returns the chronologically earliest user chunk."""
    result = mcp_client.call_tool(
        "ask_minion", {"mode": "oldest", "role": "user", "top_k": 1, "max_chars": 200}
    )
    sc = result.get("structuredContent") or {}
    hits = sc.get("results") or []
    assert hits, f"mode=oldest returned no hits: {result!r}"
    top = hits[0]
    assert top.get("role") == "user"
    assert top.get("create_time") is not None
    title = (top.get("conversation_title") or "").lower()
    # Ground truth: earliest message in this export is in this conversation.
    assert "funding company acquisitions" in title, (
        f"expected 'Funding Company Acquisitions' in title, got {top.get('conversation_title')!r}"
    )


def test_temporal_newest_is_after_oldest(mcp_client):
    """Basic sanity: newest.create_time > oldest.create_time."""
    oldest = mcp_client.call_tool(
        "ask_minion", {"mode": "oldest", "top_k": 1, "max_chars": 50}
    )
    newest = mcp_client.call_tool(
        "ask_minion", {"mode": "newest", "top_k": 1, "max_chars": 50}
    )
    o = (oldest.get("structuredContent") or {}).get("results") or []
    n = (newest.get("structuredContent") or {}).get("results") or []
    assert o and n
    assert float(n[0]["create_time"]) > float(o[0]["create_time"])


def test_keyword_mode_recovers_rare_phrase(mcp_client):
    """mode='keyword' should surface exact-phrase hits semantic search misses."""
    result = mcp_client.call_tool(
        "ask_minion",
        {"mode": "keyword", "query": "Song for Maia", "top_k": 5, "max_chars": 200},
    )
    sc = result.get("structuredContent") or {}
    hits = sc.get("results") or []
    assert hits, f"keyword mode returned no hits for proper-noun query: {result!r}"
    joined = " ".join((h.get("text") or "").lower() for h in hits)
    joined_titles = " ".join((h.get("conversation_title") or "").lower() for h in hits)
    assert "song for maia" in joined or "song for maia" in joined_titles, (
        f"keyword mode missed 'Song for Maia'; titles={[h.get('conversation_title') for h in hits]}"
    )


def test_browse_conversations(mcp_client):
    result = mcp_client.call_tool(
        "browse_conversations", {"order": "newest", "limit": 25}
    )
    sc = result.get("structuredContent") or {}
    convs = sc.get("conversations") or []
    assert len(convs) >= 5, f"expected >= 5 conversations, got {len(convs)}"
    for c in convs:
        assert c.get("conversation_id"), f"conv missing id: {c!r}"
        assert isinstance(c.get("message_count"), int) and c["message_count"] >= 1
        assert c.get("last_create_time") is not None


def _start_client(request):
    derived = _resolve_derived_dir(request)
    if derived is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR.")
    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")
    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    return client, derived


def test_voice_initialize_reflects_built_state(request):
    """initialize.instructions must inject either the voice (if built) OR the bootstrap directive."""
    client, derived = _start_client(request)
    try:
        init = client.initialize()
    finally:
        client.stop()

    instructions = (init.get("result") or {}).get("instructions") or ""
    voice_md = derived / "voice.md"

    # Unbuilt path: bootstrap directive must be present.
    # Built path: the committed voice content must be injected.
    if voice_md.exists():
        from build_voice import is_voice_built as _is_built
        built = _is_built(voice_md.read_text(encoding="utf-8"))
    else:
        built = False

    if built:
        assert "# User voice" in instructions
        assert "Voice bootstrap required" not in instructions, (
            "bootstrap directive should NOT be injected when voice is built"
        )
    else:
        assert "Voice bootstrap required" in instructions, (
            "unbuilt voice should trigger the bootstrap directive"
        )
        # Directive must point at retrieval + write tools Claude already has.
        for tool in ("ask_minion", "browse_conversations", "commit_voice"):
            assert tool in instructions, f"expected {tool!r} in bootstrap directive"


def test_commit_voice_roundtrip(request, tmp_path, monkeypatch):
    """commit_voice writes AUTO_DRAFT block; subsequent initialize reads it back."""
    derived_src = _resolve_derived_dir(request)
    if derived_src is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR.")
    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")

    # Make a throwaway derived dir so we don't clobber the live voice.md.
    import shutil
    derived = tmp_path / "derived"
    shutil.copytree(derived_src, derived)
    vp = derived / "voice.md"
    if vp.exists():
        vp.unlink()

    synthesis = (
        "### Typography\n- No em dashes.\n- No emojis.\n"
        "### Formatting\n- Paragraphs over bullets for prose answers.\n"
        "### Length and density\n- No preamble.\n"
        "### Tone and register\n- Direct, diagnostic, time-pressed.\n"
        "### Style references\n- _(insufficient signal)_\n"
        "### Hard nos\n- Don't describe what you're about to do.\n"
        "### Voice sample\nThe user writes in short, clean paragraphs. No throat-clearing. "
        "Claims earn their place with evidence. Preference for systems-level framing over "
        "tactical detail, unless tactics are the point.\n"
    )

    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    try:
        client.initialize()
        commit = client.call_tool("commit_voice", {"voice_markdown": synthesis})
        csc = commit.get("structuredContent") or {}
        assert csc.get("status") == "ok", f"commit failed: {csc!r}"
        assert csc.get("built") is True
    finally:
        client.stop()

    # Second session reads the committed voice.
    client2 = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client2.start()
    try:
        init2 = client2.initialize()
    finally:
        client2.stop()

    instr = (init2.get("result") or {}).get("instructions") or ""
    assert "Voice bootstrap required" not in instr, "bootstrap directive still injected after commit"
    assert "# User voice" in instr
    assert "diagnostic, time-pressed" in instr, "committed voice body not present in new session"


def test_commit_voice_rejects_junk(mcp_client):
    """commit_voice validates size + heading requirement."""
    r1 = mcp_client.call_tool("commit_voice", {"voice_markdown": "too short"})
    sc1 = r1.get("structuredContent") or {}
    assert sc1.get("status") == "error"

    r2 = mcp_client.call_tool("commit_voice", {"voice_markdown": "x" * 300})
    sc2 = r2.get("structuredContent") or {}
    assert sc2.get("status") == "error", "no-heading markdown should be rejected"


def test_get_voice_tool_returns_voice(mcp_client):
    result = mcp_client.call_tool("get_voice", {})
    sc = result.get("structuredContent") or {}
    assert "# User voice" in (sc.get("voice") or "")
    assert "built" in sc
    assert sc.get("char_count", 0) > 100


def test_conversation_chunks_roundtrip(mcp_client):
    """browse_conversations -> conversation_chunks returns chunks for that thread."""
    browse = mcp_client.call_tool(
        "browse_conversations", {"order": "most_messages", "limit": 1}
    )
    convs = (browse.get("structuredContent") or {}).get("conversations") or []
    assert convs, "browse_conversations returned no rows"
    cid = convs[0]["conversation_id"]

    result = mcp_client.call_tool(
        "conversation_chunks",
        {"conversation_id": cid, "limit": 10, "max_chars": 120},
    )
    sc = result.get("structuredContent") or {}
    assert sc.get("conversation_id") == cid
    chunks = sc.get("chunks") or []
    assert len(chunks) >= 1
    # seq should be monotonically non-decreasing within a conversation's chronological order.
    seqs = [c["seq"] for c in chunks]
    assert seqs == sorted(seqs), f"chunks not in seq order: {seqs}"
    for c in chunks:
        assert c.get("conversation_id") == cid
