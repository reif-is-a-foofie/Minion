"""MCP tool result wire shape (token budget): single JSON in content, no duplicate structuredContent."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import mcp_server as ms


def test_tool_result_single_json_channel():
    r = ms._tool_result_structured({"results": [{"chunk_id": "a"}], "extra": 1}, is_error=False)
    assert "structuredContent" not in r
    assert r.get("isError") is False
    body = json.loads(r["content"][0]["text"])
    assert body["results"][0]["chunk_id"] == "a"
    assert body["extra"] == 1


def test_brief_injection_merges_into_single_json(monkeypatch):
    ms._SESSION_STATE["brief_sent"] = False

    def fake_brief():
        return "brief body for test"

    monkeypatch.setattr(ms, "_load_profile_brief", fake_brief)
    structured = ms._normalize_tool_payload([{"x": 1}])
    structured = ms._maybe_inject_brief_structured(structured)
    r = ms._tool_result_structured(structured, is_error=False)
    body = json.loads(r["content"][0]["text"])
    assert "profile_brief" in body
    assert body["profile_brief"] == "brief body for test"
    assert ms._SESSION_STATE.get("brief_sent") is True


def test_initialize_instructions_shorter_when_voice_unbuilt(monkeypatch, tmp_path):
    """Voice-bootstrap path should stay below prior ~9k+ char essay (regression guard)."""
    data = tmp_path / "derived"
    data.mkdir()
    monkeypatch.setenv("MINION_DATA_DIR", str(data))
    ms._SESSION_STATE.clear()
    ms._SESSION_STATE["brief_sent"] = False
    monkeypatch.setattr(ms, "_voice_is_built", lambda: False)
    monkeypatch.setattr(ms, "_load_voice", lambda: None)
    monkeypatch.setattr(ms, "_load_profile_brief", lambda: None)
    monkeypatch.setattr(ms, "_load_retrieval_instructions", lambda: "stub policy")

    out = ms._handle_initialize({"id": 1, "method": "initialize"})
    instr = (out.get("result") or {}).get("instructions") or ""
    assert "Voice bootstrap required" in instr
    assert len(instr) < 6500, f"initialize instructions unexpectedly large: {len(instr)} chars"


def test_get_identity_schema_returns_seven_layers():
    out = ms._tool_get_identity_schema({})
    assert out.get("status") == "ok"
    sch = out.get("schema") or {}
    assert sch.get("version") == 1
    assert len(sch.get("layers") or []) == 7
    assert sch["layers"][0]["layer"] == 1
    assert sch["layers"][-1]["access_tier"] == "locked"


def test_initialize_skips_bootstrap_when_voice_built(monkeypatch, tmp_path):
    data = tmp_path / "derived"
    data.mkdir()
    monkeypatch.setenv("MINION_DATA_DIR", str(data))
    ms._SESSION_STATE.clear()
    ms._SESSION_STATE["brief_sent"] = False
    monkeypatch.setattr(ms, "_voice_is_built", lambda: True)
    monkeypatch.setattr(ms, "_load_voice", lambda: "### Typography\n- x\n")
    monkeypatch.setattr(ms, "_load_profile_brief", lambda: None)
    monkeypatch.setattr(ms, "_load_retrieval_instructions", lambda: "stub")

    out = ms._handle_initialize({"id": 2, "method": "initialize"})
    instr = (out.get("result") or {}).get("instructions") or ""
    assert "Voice bootstrap required" not in instr
    assert "### Typography" in instr
