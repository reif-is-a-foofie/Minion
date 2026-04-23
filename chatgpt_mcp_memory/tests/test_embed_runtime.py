"""Tests for embed_runtime env helpers and coordinated idle unload."""
from __future__ import annotations

import sys
import threading
import time
import types

import embed_runtime as er


def test_embed_batch_size_default(monkeypatch):
    monkeypatch.delenv("MINION_EMBED_BATCH_SIZE", raising=False)
    assert er.embed_batch_size(64) == 64


def test_embed_batch_size_from_env(monkeypatch):
    monkeypatch.setenv("MINION_EMBED_BATCH_SIZE", "32")
    assert er.embed_batch_size(64) == 32


def test_embed_batch_size_clamped(monkeypatch):
    monkeypatch.setenv("MINION_EMBED_BATCH_SIZE", "9999")
    assert er.embed_batch_size(64) == 256


def test_unload_shared_clears_ingest_and_api(monkeypatch):
    monkeypatch.setenv("MINION_EMBED_IDLE_SEC", "0.01")

    fake_ingest = types.ModuleType("ingest")
    fake_ingest._MODEL_LOCK = threading.Lock()
    fake_ingest._MODEL = object()
    fake_ingest._MODEL_NAME = "m"

    fake_api = types.ModuleType("api")
    fake_api._query_model_lock = threading.Lock()
    fake_api._query_model = object()

    monkeypatch.setitem(sys.modules, "ingest", fake_ingest)
    monkeypatch.setitem(sys.modules, "api", fake_api)

    er.touch_embedding_use()
    time.sleep(0.05)
    er.unload_shared_embedding_caches_if_idle()

    assert fake_ingest._MODEL is None
    assert fake_ingest._MODEL_NAME is None
    assert fake_api._query_model is None


def test_unload_shared_noop_when_idle_disabled(monkeypatch):
    monkeypatch.delenv("MINION_EMBED_IDLE_SEC", raising=False)

    fake_ingest = types.ModuleType("ingest")
    fake_ingest._MODEL_LOCK = threading.Lock()
    fake_ingest._MODEL = object()
    fake_ingest._MODEL_NAME = "m"

    monkeypatch.setitem(sys.modules, "ingest", fake_ingest)

    er.unload_shared_embedding_caches_if_idle()
    assert fake_ingest._MODEL is not None
