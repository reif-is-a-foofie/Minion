from __future__ import annotations

import json
from pathlib import Path

import screen_context_store as scs


def test_stream_path_under_data_dir(tmp_path: Path) -> None:
    p = scs.stream_path(tmp_path)
    assert p == tmp_path / "screen_context" / "stream.jsonl"


def test_read_recent_empty(tmp_path: Path) -> None:
    assert scs.read_recent(tmp_path, limit=10) == []


def test_read_recent_parses_tail(tmp_path: Path) -> None:
    d = tmp_path / "screen_context"
    d.mkdir(parents=True)
    fp = d / "stream.jsonl"
    rows = [
        {"ts": 1.0, "window_title": "Alpha", "app_name": "App"},
        {"ts": 2.0, "window_title": "Beta", "app_name": "App"},
    ]
    fp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    got = scs.read_recent(tmp_path, limit=10)
    assert len(got) == 2
    assert got[-1]["window_title"] == "Beta"


def test_search_substring_matches_ax_sample(tmp_path: Path) -> None:
    d = tmp_path / "screen_context"
    d.mkdir(parents=True)
    fp = d / "stream.jsonl"
    rec = {
        "ts": 3.0,
        "app_name": "X",
        "window_title": "Y",
        "ax_text_sample": "Castle Hill budget memo",
    }
    fp.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    hits = scs.search_substring(tmp_path, "Castle Hill", limit=10)
    assert len(hits) == 1
    assert hits[0]["window_title"] == "Y"


def test_summarize_recent_nonempty(tmp_path: Path) -> None:
    d = tmp_path / "screen_context"
    d.mkdir(parents=True)
    fp = d / "stream.jsonl"
    fp.write_text(
        json.dumps({"ts": 1.0, "app_name": "Slack", "window_title": "#general"}) + "\n",
        encoding="utf-8",
    )
    out = scs.summarize_recent_heuristic(tmp_path, event_limit=10)
    assert out["status"] == "ok"
    assert out["events_used"] == 1
    assert "Slack" in out["summary"]

