"""SessionOpener registers the durable record a SessionStart hook owns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from biff.session_open import SessionOpener
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    import pytest


def _opener(tmp_path: Path, relay: LocalRelay) -> SessionOpener:
    return SessionOpener(
        relay=relay,
        config=BiffConfig(
            user="rmh", display_name="Raymond", kind="agent", repo_name="r"
        ),
        repo_common_root=tmp_path / "repo",
        unread_dir=tmp_path / "unread",
        handles=SessionHandleStore(tmp_path / "sessions"),
        hostname="h",
        pwd="/w",
    )


async def test_open_registers_row_marker_unread_and_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    relay = LocalRelay(tmp_path / "relay")
    routing = SessionRouting("sess-1")
    key = routing.session_key("rmh")

    await _opener(tmp_path, relay).open(routing)

    row = await relay.get_session(key)
    assert row is not None and row.user == "rmh" and row.tty == routing.routing_id
    assert row.tty_name
    unread = json.loads(routing.unread_path(tmp_path / "unread").read_text())
    assert unread["user"] == "rmh" and unread["repo"] == "r"
    assert unread["tty_name"] == row.tty_name
    marker = tmp_path / ".punt-labs" / "biff" / "active" / key.replace(":", "-")
    assert marker.read_text().splitlines()[:2] == [key, "r"]
    assert SessionHandleStore(tmp_path / "sessions").read(routing) == key


async def test_open_is_idempotent_for_the_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    relay = LocalRelay(tmp_path / "relay")
    routing = SessionRouting("sess-1")
    opener = _opener(tmp_path, relay)

    first = await opener.open(routing)
    second = await opener.open(routing)

    assert first == second
    assert len(await relay.get_sessions()) == 1
