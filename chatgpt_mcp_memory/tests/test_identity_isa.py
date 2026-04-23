"""ISA layer columns, access grants, and access log."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

pytest.importorskip("sqlite_vec")

from store import connect, identity_claim_list  # noqa: E402
import identity  # noqa: E402
import identity_layers  # noqa: E402


def test_layer7_requires_explicit_declaration(tmp_path) -> None:
    db = tmp_path / "m.db"
    conn = connect(db)
    payload, err = identity.propose_identity_update(
        conn,
        kind="sensitive",
        text="user declared health note for testing",
        layer=7,
        field="health_condition",
        meta={"explicit_declaration": False},
    )
    assert payload is None and err and "explicit_declaration" in err

    payload2, err2 = identity.propose_identity_update(
        conn,
        kind="sensitive",
        text="user declared health note for testing",
        layer=7,
        field="health_condition",
        meta={"explicit_declaration": True},
    )
    assert err2 is None and payload2 is not None
    row = identity_claim_list(conn, limit=5)[0]
    assert row["layer"] == 7
    assert row["field"] == "health_condition"


def test_grant_respects_selective_layers(tmp_path) -> None:
    db = tmp_path / "m.db"
    conn = connect(db)
    identity.propose_identity_update(
        conn,
        kind="pattern",
        text="deep work mornings preferred",
        layer=5,
        field="work_rhythm",
        source_agent="test",
    )
    c = identity_claim_list(conn, status="proposed", limit=1)[0]
    identity.set_claim_status(conn, c["claim_id"], status="active")

    out, err = identity.grant_identity_context(
        conn,
        agent_id="scheduling_agent",
        purpose="optimize calendar blocking for deep work",
        requested_layers=[5, 6],
        session_grants=set(),
    )
    assert err is None and out is not None
    assert out["denied_layers"] == [5]
    assert out["granted_layers"] == [6]
    assert out["claims"] == {}

    out2, err2 = identity.grant_identity_context(
        conn,
        agent_id="scheduling_agent",
        purpose="optimize calendar blocking for deep work",
        requested_layers=[5, 6],
        session_grants={5},
    )
    assert err2 is None and out2 is not None
    assert 5 in out2["granted_layers"]
    assert "5" in out2["claims"] and len(out2["claims"]["5"]) >= 1

    logs = identity.export_identity_snapshot(conn)["access_log"]
    assert len(logs) >= 1


def test_field_validation(tmp_path) -> None:
    db = tmp_path / "m.db"
    conn = connect(db)
    _, err = identity.propose_identity_update(
        conn,
        kind="fact",
        text="preferred name is Alex",
        layer=1,
        field="not_a_real_field",
    )
    assert err and "not valid" in err


def test_parse_session_grants_header() -> None:
    assert identity_layers.parse_session_grants("2, 7, 99") == {2, 7}
