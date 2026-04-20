"""
Pytest hooks for the Minion MCP golden-query harness.

`pytest_addoption` and `pytest_generate_tests` must live at plugin (conftest)
scope — putting them in test modules silently skips them on newer pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_eval import DEFAULT_GOLDEN, _load_yaml  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--derived-dir",
        action="store",
        default=None,
        help="Directory containing chunks/embeddings/manifest for the MCP to serve.",
    )
    parser.addoption(
        "--golden",
        action="store",
        default=str(DEFAULT_GOLDEN),
        help="Golden queries YAML.",
    )


def pytest_generate_tests(metafunc):
    if "case" in metafunc.fixturenames:
        golden = Path(metafunc.config.getoption("--golden")).expanduser().resolve()
        data = _load_yaml(golden) if golden.exists() else {}
        cases = [c for c in (data.get("cases") or []) if isinstance(c, dict)]
        ids = [str(c.get("query") or f"case-{i}") for i, c in enumerate(cases)]
        metafunc.parametrize("case", cases, ids=ids)
