import os

import pytest

import ollama_limits as ol


def test_default_pct_maps_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINION_OLLAMA_MAX_CPU_PCT", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    ol._sem = None  # type: ignore[attr-defined]
    assert ol.ollama_num_thread_cap() == 3  # 30% of 10 → round 3


def test_zero_disables_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINION_OLLAMA_MAX_CPU_PCT", "0")
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    ol._sem = None  # type: ignore[attr-defined]
    assert ol.ollama_num_thread_cap() is None


def test_merged_options_caps_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINION_OLLAMA_MAX_CPU_PCT", "30")
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    ol._sem = None  # type: ignore[attr-defined]
    assert ol.merged_ollama_options({"num_thread": 99}) == {"num_thread": 3}
    assert ol.merged_ollama_options(None) == {"num_thread": 3}
