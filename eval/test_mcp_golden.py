"""
Pytest wrapper around eval/run_eval.py.

Point the harness at a derived dir via one of:
    - MINION_DERIVED_DIR env var
    - pytest --derived-dir <path> CLI flag (registered in conftest.py)

Skipped gracefully when no derived dir is available, so CI passes on machines
without an indexed export.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_eval import (  # noqa: E402
    DEFAULT_SERVER_PY,
    MCPStdioClient,
    run_case,
)


def _resolve_derived_dir(request) -> Path | None:
    raw = request.config.getoption("--derived-dir") or os.environ.get("MINION_DERIVED_DIR")
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.exists() else None


@pytest.fixture(scope="session")
def mcp_client(request):
    derived = _resolve_derived_dir(request)
    if derived is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR — skipping MCP golden tests.")

    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")

    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    try:
        client.initialize()
        yield client
    finally:
        client.stop()


def test_golden_query(mcp_client, case):
    result = run_case(mcp_client, case, top_k_override=None)
    assert result.error is None, f"MCP error on {case.get('query')!r}: {result.error}"
    assert result.passed, (
        f"Query {case.get('query')!r} missed all expectations. "
        f"Top hit: {result.hits[0] if result.hits else None}"
    )


def test_profile_brief_first_call_only(request):
    """First tools/call carries structuredContent.profile_brief; second does not."""
    derived = _resolve_derived_dir(request)
    if derived is None:
        pytest.skip("No --derived-dir / MINION_DERIVED_DIR — skipping brief-injection test.")
    if not (derived / "brief.md").exists() and not (derived / "core_profile.md").exists():
        pytest.skip("No brief.md / core_profile.md in derived dir — nothing to inject.")
    if not DEFAULT_SERVER_PY.exists():
        pytest.skip(f"MCP server script missing: {DEFAULT_SERVER_PY}")

    client = MCPStdioClient(server_py=DEFAULT_SERVER_PY, derived_dir=derived)
    client.start()
    try:
        client.initialize()
        first = client.call_tool(
            "ask_minion", {"query": "sanity probe", "top_k": 1, "max_chars": 80}
        )
        second = client.call_tool(
            "ask_minion", {"query": "another probe", "top_k": 1, "max_chars": 80}
        )
    finally:
        client.stop()

    sc1 = first.get("structuredContent") or {}
    sc2 = second.get("structuredContent") or {}
    assert isinstance(sc1, dict), f"first call structuredContent not a dict: {sc1!r}"
    assert "profile_brief" in sc1, "first call missing profile_brief"
    assert len(sc1["profile_brief"]) > 50, "profile_brief suspiciously short"
    assert "profile_brief" not in sc2, "profile_brief leaked into second call"
