import pytest


def test_settings_telemetry_default_opt_in(tmp_path):
    from settings import load_settings, save_settings

    s = load_settings(tmp_path)
    assert s.get("telemetry_opt_out") is False
    merged = dict(s)
    merged["telemetry_opt_out"] = True
    save_settings(tmp_path, merged)
    assert load_settings(tmp_path).get("telemetry_opt_out") is True


def test_settings_legacy_analytics_opt_in_dropped(tmp_path):
    from pathlib import Path

    from settings import load_settings

    p = Path(tmp_path) / "settings.json"
    p.write_text('{"disabled_kinds": [], "analytics_opt_in": true}\n', encoding="utf-8")
    s = load_settings(tmp_path)
    assert "analytics_opt_in" not in s
    assert s.get("telemetry_opt_out") is False


def test_remote_disabled_when_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MINION_ANALYTICS_URL", "https://example.invalid/collect")
    from pathlib import Path

    from settings import save_settings
    import telemetry

    telemetry.configure(tmp_path)
    save_settings(tmp_path, {"disabled_kinds": [], "telemetry_opt_out": True})
    from analytics_remote import _remote_enabled

    ok, _ = _remote_enabled(Path(tmp_path))
    assert ok is False


def test_sanitize_search_strips_query():
    from analytics_remote import _sanitize

    p = _sanitize(
        "search",
        {
            "query": "secret patient name",
            "returned": 3,
            "top_k": 8,
            "mode": "relevance",
            "rerank": "rrf",
            "hit_kinds": ["text", "pdf"],
            "kind_filter": "text",
        },
    )
    assert p is not None
    assert "secret" not in str(p)
    assert p.get("has_query") is True
    assert p.get("returned") == 3


def test_sanitize_ingest_reason_class():
    from analytics_remote import _sanitize

    p = _sanitize(
        "ingest",
        {
            "file_kind": "image",
            "parser": "ollama",
            "chunks": 0,
            "skipped": True,
            "result": "parse-error",
            "reason": "parse-error: No module named 'foo'",
        },
    )
    assert p is not None
    assert p.get("reason_class") == "parse-error"


def test_sanitize_ingest_pathy_reason_redacted():
    from analytics_remote import _sanitize

    p = _sanitize(
        "ingest",
        {
            "file_kind": "html",
            "parser": "x",
            "skipped": True,
            "result": "skipped",
            "reason": "failed /Users/x/file.html",
        },
    )
    assert p is not None
    assert p.get("reason_class") == "redacted_path_token"
