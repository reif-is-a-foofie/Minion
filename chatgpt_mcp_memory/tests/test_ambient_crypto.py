"""Ambient Phase 1 crypto + Layer 0 round-trip."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

pytest.importorskip("cryptography")
pytest.importorskip("argon2")

from ambient_vault import (  # noqa: E402
    keyring_exists,
    open_ciphertext,
    seal_plaintext,
    setup_keyring,
    unlock_keyring,
)
from layer0 import build_raw_event, new_event_id, open_sealed_file, seal_raw_event_json  # noqa: E402


def _cheap_setup(d: Path, passphrase: str) -> None:
    setup_keyring(
        d,
        passphrase,
        time_cost=1,
        memory_kib=4096,
        parallelism=1,
    )


def test_ambient_crypto_and_layer0(tmp_path: Path) -> None:
    """Single Argon2 setup: keyring + AEAD + sealed JSON event."""
    d = tmp_path / "data"
    d.mkdir()
    _cheap_setup(d, "correct horse battery staple")
    assert keyring_exists(d)
    sess = unlock_keyring(d, "correct horse battery staple")
    pt = b'{"hello": "world"}'
    blob = seal_plaintext(
        sess,
        plaintext=pt,
        source_type="test",
        event_id="evt-test1",
        tier="dek",
    )
    assert open_ciphertext(sess, blob) == pt
    with pytest.raises(ValueError):
        unlock_keyring(d, "wrong-passphrase")

    sess2 = unlock_keyring(d, "correct horse battery staple")
    eid = new_event_id()
    ev = build_raw_event(
        event_id=eid,
        source_type="chatgpt_export",
        consent_source_key="chatgpt_export",
        ingest_path="/tmp/export",
        payload={"format": "unit", "n": 1},
        source_id="src-test",
    )
    path = seal_raw_event_json(d, sess2, ev, tier="dek")
    assert path.is_file()
    doc = open_sealed_file(d, sess2, path)
    assert doc["event_id"] == eid
    assert doc["payload"]["format"] == "unit"
