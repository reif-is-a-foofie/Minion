"""MCP tool result wire shape (token budget): single JSON in content, no duplicate structuredContent."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import identity
import mcp_server as ms
from settings import load_settings, merge_identity_defaults, save_settings


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


def test_merge_tool_and_persisted_grants():
    assert ms._merge_tool_and_persisted_grants([2], {5, 6}) == {2, 5, 6}
    assert ms._merge_tool_and_persisted_grants(None, {7}) == {7}


def test_set_identity_claim_status_rejects(monkeypatch, tmp_path):
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    ms._CONN = None
    try:
        conn = ms._get_conn()
        payload, err = identity.propose_identity_update(
            conn,
            kind="fact",
            text="mcp status tool test claim",
            source_agent="pytest",
        )
        assert err is None and payload
        cid = str(payload["claim_id"])
        out = ms._tool_set_identity_claim_status({"claim_id": cid, "status": "rejected"})
        assert out.get("status") == "ok"
        assert (out.get("claim") or {}).get("status") == "rejected"
    finally:
        c = ms._CONN
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            ms._CONN = None


def test_get_identity_context_uses_persisted_settings_grants(monkeypatch, tmp_path):
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    ms._CONN = None
    try:
        cur = load_settings(tmp_path)
        cur["identity"] = merge_identity_defaults({"session_layer_grants": [5]})
        save_settings(tmp_path, cur)

        conn = ms._get_conn()
        payload, err = identity.propose_identity_update(
            conn,
            kind="pattern",
            text="pytest deep work mornings",
            layer=5,
            field="work_rhythm",
            source_agent="pytest",
        )
        assert err is None and payload
        cid = payload["claim_id"]
        identity.set_claim_status(conn, cid, status="active")
        conn.commit()

        out = ms._tool_get_identity_context(
            {
                "purpose": "test persisted session grants",
                "requested_layers": [5, 6],
                "session_layer_grants": [],
            }
        )
        assert out.get("status") == "ok"
        assert 5 in (out.get("granted_layers") or [])
        assert "5" in (out.get("claims") or {})
        assert len((out.get("claims") or {}).get("5") or []) >= 1
    finally:
        c = ms._CONN
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            ms._CONN = None


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
