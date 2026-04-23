"""Phase-1 crypto: Argon2id MK, wrapped DEK/L7K, AES-256-GCM for Layer 0 blobs.

Runtime split: Python in the sidecar (matches ingest/MCP). Tauri may later
hold MK in OS secure storage and IPC unwrap requests — same wire format.
"""
from __future__ import annotations

import base64
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Literal, Tuple

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYRING_FILENAME = "keyring.json"
KEYRING_DIR = "crypto"

MAGIC = b"MNRAW"
BLOB_VERSION = 1
NONCE_LEN = 12
KEY_LEN = 32

KeyTier = Literal["dek", "l7k"]


def keyring_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEYRING_DIR / KEYRING_FILENAME


def derive_mk(passphrase: str, salt: bytes, *, time_cost: int, memory_kib: int, parallelism: int) -> bytes:
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_kib * 1024,
        parallelism=parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def _wrap_key(mk: bytes, key32: bytes) -> Tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_LEN)
    aes = AESGCM(mk)
    ct = aes.encrypt(nonce, key32, None)
    return nonce, ct


def _unwrap_key(mk: bytes, nonce: bytes, ct: bytes) -> bytes:
    aes = AESGCM(mk)
    return aes.decrypt(nonce, ct, None)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


@dataclass
class VaultSession:
    """Unwrapped DEK + L7K; keep in memory only."""

    dek: bytes
    l7k: bytes


def setup_keyring(
    data_dir: Path,
    passphrase: str,
    *,
    time_cost: int = 3,
    memory_kib: int = 65536,
    parallelism: int = 4,
) -> None:
    """Create keyring on disk (fails if present). Argon2id params are stored for unlock."""
    dest = keyring_path(data_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"keyring already exists: {dest}")
    salt = os.urandom(16)
    mk = derive_mk(passphrase, salt, time_cost=time_cost, memory_kib=memory_kib, parallelism=parallelism)
    dek = os.urandom(KEY_LEN)
    l7k = os.urandom(KEY_LEN)
    n_dek, c_dek = _wrap_key(mk, dek)
    n_l7, c_l7 = _wrap_key(mk, l7k)
    payload = {
        "v": 1,
        "kdf": "argon2id",
        "salt": _b64e(salt),
        "time_cost": time_cost,
        "memory_kib": memory_kib,
        "parallelism": parallelism,
        "dek": {"nonce": _b64e(n_dek), "ct": _b64e(c_dek)},
        "l7k": {"nonce": _b64e(n_l7), "ct": _b64e(c_l7)},
    }
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)


def unlock_keyring(data_dir: Path, passphrase: str) -> VaultSession:
    dest = keyring_path(data_dir)
    if not dest.exists():
        raise FileNotFoundError(f"no keyring at {dest}")
    doc = json.loads(dest.read_text(encoding="utf-8"))
    if int(doc.get("v") or 0) != 1:
        raise ValueError("unsupported keyring version")
    salt = _b64d(str(doc["salt"]))
    mk = derive_mk(
        passphrase,
        salt,
        time_cost=int(doc["time_cost"]),
        memory_kib=int(doc["memory_kib"]),
        parallelism=int(doc["parallelism"]),
    )
    try:
        dek = _unwrap_key(mk, _b64d(str(doc["dek"]["nonce"])), _b64d(str(doc["dek"]["ct"])))
        l7k = _unwrap_key(mk, _b64d(str(doc["l7k"]["nonce"])), _b64d(str(doc["l7k"]["ct"])))
    except Exception as e:
        raise ValueError("passphrase incorrect or keyring corrupt") from e
    if len(dek) != KEY_LEN or len(l7k) != KEY_LEN:
        raise ValueError("invalid key material")
    return VaultSession(dek=dek, l7k=l7k)


def seal_plaintext(
    session: VaultSession,
    *,
    plaintext: bytes,
    source_type: str,
    event_id: str,
    tier: KeyTier = "dek",
) -> bytes:
    """AES-GCM seal; returns on-disk blob (MAGIC + header + AAD + nonce + ciphertext+tag)."""
    key = session.dek if tier == "dek" else session.l7k
    nonce = os.urandom(NONCE_LEN)
    aad = json.dumps(
        {"v": BLOB_VERSION, "source_type": source_type, "event_id": event_id},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    if len(aad) > 65535:
        raise ValueError("AAD too large")
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, plaintext, aad)
    key_id = 0 if tier == "dek" else 1
    header = (
        MAGIC
        + struct.pack("<IBH", BLOB_VERSION, key_id, len(aad))
        + aad
        + struct.pack("B", NONCE_LEN)
        + nonce
    )
    return header + ct


def open_ciphertext(session: VaultSession, blob: bytes) -> bytes:
    """Decrypt a blob written by seal_plaintext."""
    if len(blob) < len(MAGIC) + struct.calcsize("<IBH") + NONCE_LEN + 16:
        raise ValueError("blob too short")
    if blob[: len(MAGIC)] != MAGIC:
        raise ValueError("bad magic")
    off = len(MAGIC)
    ver, key_id, aad_len = struct.unpack_from("<IBH", blob, off)
    off += struct.calcsize("<IBH")
    if ver != BLOB_VERSION:
        raise ValueError("unsupported blob version")
    if aad_len < 0 or aad_len > 4096:
        raise ValueError("invalid AAD length")
    aad = blob[off : off + aad_len]
    off += aad_len
    nlen = struct.unpack_from("B", blob, off)[0]
    off += 1
    if nlen != NONCE_LEN:
        raise ValueError("bad nonce length")
    nonce = blob[off : off + NONCE_LEN]
    off += NONCE_LEN
    ct = blob[off:]
    tier: KeyTier = "dek" if int(key_id) == 0 else "l7k"
    key = session.dek if tier == "dek" else session.l7k
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, aad)


def keyring_exists(data_dir: Path) -> bool:
    return keyring_path(data_dir).is_file()
