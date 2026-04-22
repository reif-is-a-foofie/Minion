"""
Process-wide limits for local Ollama calls (CPU budget + concurrency).

Default: keep inference near a fraction of logical CPU capacity and avoid
stacking multiple Ollama runs (each of which can spawn its own threads).
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

_lock = threading.Lock()
_sem: Optional[threading.Semaphore] = None


def _raw_max_cpu_pct() -> str:
    v = os.environ.get("MINION_OLLAMA_MAX_CPU_PCT")
    if v is None:
        return "30"
    return str(v).strip()


def ollama_num_thread_cap() -> Optional[int]:
    """
    Maps MINION_OLLAMA_MAX_CPU_PCT to an Ollama ``num_thread`` ceiling.

    - Unset → treat as 30 (percent of ``os.cpu_count()`` logical slots).
    - 0, off, none, or 100+ → no cap (Ollama picks threads).
    """
    raw = _raw_max_cpu_pct().lower()
    if raw in ("", "0", "off", "none"):
        return None
    try:
        pct = float(raw)
    except ValueError:
        return None
    if pct <= 0 or pct >= 100:
        return None
    logical = os.cpu_count() or 4
    threads = max(1, int(logical * (pct / 100.0) + 0.5))
    return min(threads, logical)


def _default_max_inflight() -> int:
    """Bounded parallelism for Ollama HTTP calls in this process."""
    raw = os.environ.get("MINION_OLLAMA_MAX_INFLIGHT", "").strip()
    if raw:
        try:
            return max(1, min(16, int(raw)))
        except ValueError:
            pass
    # With a CPU cap, default to single-flight so two requests do not multiply load.
    return 1 if ollama_num_thread_cap() is not None else 2


def _semaphore() -> threading.Semaphore:
    global _sem
    with _lock:
        if _sem is None:
            _sem = threading.Semaphore(_default_max_inflight())
        return _sem


@contextmanager
def acquire_ollama_inference():
    """Serialize or bound concurrent ``ollama.chat`` calls in this process."""
    sem = _semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def merged_ollama_options(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge caller options with the process ``num_thread`` ceiling (if any)."""
    out: Dict[str, Any] = dict(user or {})
    cap = ollama_num_thread_cap()
    if cap is None:
        return out
    ut = out.get("num_thread")
    if ut is None:
        out["num_thread"] = cap
        return out
    try:
        out["num_thread"] = min(int(ut), cap)
    except (TypeError, ValueError):
        out["num_thread"] = cap
    return out
