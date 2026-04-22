import pytest


@pytest.fixture()
def home(monkeypatch, tmp_path):
    fake = str(tmp_path / "u")
    monkeypatch.setenv("HOME", fake)
    return fake


def test_redact_bearer_and_home(home):
    from diagnostics import redact_log_line

    line = f"GET {home}/secret Authorization: Bearer supersecret123"
    out = redact_log_line(line)
    assert "supersecret123" not in out
    assert "[redacted]" in out
    assert "~/" in out or "~" in out


def test_read_log_tail_empty_when_no_env(monkeypatch: pytest.MonkeyPatch):
    from diagnostics import read_log_tail

    monkeypatch.delenv("MINION_LOG_FILE", raising=False)
    path, lines = read_log_tail()
    assert path is None
    assert lines == []


def test_read_log_tail(tmp_path, monkeypatch):
    from diagnostics import read_log_tail

    logf = tmp_path / "sidecar.log"
    logf.write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setenv("MINION_LOG_FILE", str(logf))
    path, lines = read_log_tail(max_lines=10)
    assert path == logf.resolve()
    assert "alpha" in lines[0] and "beta" in lines[1]
