"""Optional `ps` titles for Minion Python processes (setproctitle if installed)."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("minion.process_title")


def _dir_tag(path: Optional[str]) -> str:
    if not path or not str(path).strip():
        return "nodata"
    return hashlib.sha256(str(Path(path).expanduser().resolve()).encode()).hexdigest()[:8]


def data_dir_sha8(data_dir: Path) -> str:
    """Short stable fingerprint for logs (not cryptographic identity)."""
    return _dir_tag(str(data_dir))


def apply_sidecar_title(*, port: int, data_dir: Path) -> None:
    title = f"Minion-sidecar:{int(port)}:{_dir_tag(str(data_dir))}"
    _try_set(title)


def apply_sidecar_title_from_env() -> None:
    port = int(os.environ.get("MINION_API_PORT", "8765"))
    raw = os.environ.get("MINION_DATA_DIR", "").strip()
    tag = _dir_tag(raw) if raw else "nodata"
    _try_set(f"Minion-sidecar:{port}:{tag}")


def apply_mcp_title() -> None:
    tag = _dir_tag(os.environ.get("MINION_DATA_DIR", ""))
    _try_set(f"Minion-mcp:{tag}")


def _try_set(title: str) -> None:
    try:
        import setproctitle  # type: ignore

        setproctitle.setproctitle(title[:120])
    except ImportError:
        log.debug("setproctitle not installed; process title unchanged")
    except Exception:
        log.debug("setproctitle failed", exc_info=True)
