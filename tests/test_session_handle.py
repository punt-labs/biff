"""SessionHandleStore writes the per-session handle file the standard specifies."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionIdError, SessionRouting


def test_write_creates_private_handle_file(tmp_path: Path) -> None:
    store = SessionHandleStore(tmp_path / "sessions")
    routing = SessionRouting("sess-1")
    path = store.write(routing, "rmh:abc")
    assert path == tmp_path / "sessions" / "sess-1" / "handle"
    assert path.read_text() == "rmh:abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "sessions").stat().st_mode) == 0o700


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    store = SessionHandleStore(tmp_path / "sessions")
    assert store.read(SessionRouting("nope")) is None


def test_write_then_read_round_trips_and_overwrites(tmp_path: Path) -> None:
    store = SessionHandleStore(tmp_path / "sessions")
    routing = SessionRouting("sess-1")
    store.write(routing, "first")
    store.write(routing, "second")
    assert store.read(routing) == "second"


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    store = SessionHandleStore(tmp_path / "sessions")
    store.write(SessionRouting("sess-1"), "k")
    assert [p.name for p in (tmp_path / "sessions" / "sess-1").iterdir()] == ["handle"]


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_malformed_session_id_never_reaches_a_path(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SessionIdError):
        SessionHandleStore(tmp_path / "sessions").write(SessionRouting(bad), "k")
    assert not (tmp_path / "sessions").exists()
