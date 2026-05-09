from pathlib import Path

from consent_policy import (
    DEFAULT_POLICY,
    hit_allowed_for_mcp,
    load_policy,
    policy_path,
    save_policy,
)
from store import Hit


def test_hit_blocked_for_ambient_kind(tmp_path: Path) -> None:
    pol = load_policy(tmp_path)
    h = Hit(
        chunk_id="c1",
        score=1.0,
        text="x",
        role=None,
        source_id="s1",
        path="/tmp/x.txt",
        kind="ambient",
        mtime=0.0,
        meta={},
        source_meta={},
    )
    assert hit_allowed_for_mcp(h, pol) is False


def test_hit_blocked_for_screen_memory_path(tmp_path: Path) -> None:
    pol = load_policy(tmp_path)
    h = Hit(
        chunk_id="c1",
        score=1.0,
        text="x",
        role=None,
        source_id="s1",
        path="/Users/me/inbox/screen-memory/foo.png",
        kind="image",
        mtime=0.0,
        meta={},
        source_meta={},
    )
    assert hit_allowed_for_mcp(h, pol) is False


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    save_policy(tmp_path, DEFAULT_POLICY)
    assert policy_path(tmp_path).is_file()
    pol = load_policy(tmp_path)
    assert pol["readers"]["mcp"]["deny_chunk_source_kinds"] == ["ambient"]
