import pytest


def test_settings_analytics_default(tmp_path):
    from settings import load_settings, save_settings

    s = load_settings(tmp_path)
    assert s.get("analytics_opt_in") is False
    merged = dict(s)
    merged["analytics_opt_in"] = True
    save_settings(tmp_path, merged)
    assert load_settings(tmp_path).get("analytics_opt_in") is True


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
