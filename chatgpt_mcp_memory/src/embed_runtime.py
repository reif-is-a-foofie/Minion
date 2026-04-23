"""Shared embedding cache policy: idle unload + batch size from env."""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

_touch_lock = threading.Lock()
_last_touch: float = time.monotonic()
_unload_lock = threading.Lock()


def embed_idle_seconds() -> Optional[float]:
    raw = (os.environ.get("MINION_EMBED_IDLE_SEC") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def embed_batch_size(default: int = 64) -> int:
    raw = (os.environ.get("MINION_EMBED_BATCH_SIZE") or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        return max(1, min(n, 256))
    except ValueError:
        return default


def touch_embedding_use() -> None:
    global _last_touch
    with _touch_lock:
        _last_touch = time.monotonic()


def should_unload_embedding() -> bool:
    lim = embed_idle_seconds()
    if lim is None:
        return False
    with _touch_lock:
        return (time.monotonic() - _last_touch) > lim


def unload_shared_embedding_caches_if_idle() -> None:
    """In the HTTP sidecar process, ingest + api each cache TextEmbedding; drop both together."""
    if not should_unload_embedding():
        return
    with _unload_lock:
        ing = sys.modules.get("ingest")
        if ing is not None:
            lock = getattr(ing, "_MODEL_LOCK", None)
            if lock is not None:
                with lock:
                    ing._MODEL = None  # type: ignore[attr-defined]
                    ing._MODEL_NAME = None  # type: ignore[attr-defined]
        api_mod = sys.modules.get("api")
        if api_mod is not None:
            qlock = getattr(api_mod, "_query_model_lock", None)
            if qlock is not None:
                with qlock:
                    api_mod._query_model = None  # type: ignore[attr-defined]
