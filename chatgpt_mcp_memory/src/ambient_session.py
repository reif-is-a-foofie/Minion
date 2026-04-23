"""Process-wide crypto session (DEK/L7K in RAM only). Set from API unlock."""
from __future__ import annotations

import threading
from typing import Optional

from ambient_vault import VaultSession

_lock = threading.RLock()
_sess: Optional[VaultSession] = None


def attach_session(sess: VaultSession) -> None:
    with _lock:
        global _sess
        _sess = sess


def detach_session() -> None:
    with _lock:
        global _sess
        _sess = None


def get_session() -> Optional[VaultSession]:
    with _lock:
        return _sess
