"""SessionOpener registers the durable record a SessionStart hook owns."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from biff.session_open import SessionOpener
from biff.session_routing import SessionRouting


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


def _git_repo(root: Path) -> Path:
    import subprocess

    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
    return root


async def test_open_from_payload_registers_under_the_payload_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biff.session_open import open_session_from_payload

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _git_repo(tmp_path / "repo")

    key = await open_session_from_payload(
        {"session_id": "sess-9", "cwd": str(repo)},
        user_override="rmh",
        relay_url_override="",
    )

    routing = SessionRouting("sess-9")
    assert key == routing.session_key("rmh")
    unread = routing.unread_path(tmp_path / ".punt-labs" / "biff" / "unread")
    assert json.loads(unread.read_text())["user"] == "rmh"
    handle = SessionHandleStore(tmp_path / ".punt-labs" / "biff" / "sessions")
    assert handle.read(routing) == key


@pytest.mark.parametrize("payload", [{}, {"session_id": ""}, {"session_id": "../x"}])
async def test_open_from_payload_without_a_valid_session_id_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    from biff.session_open import open_session_from_payload

    monkeypatch.setenv("HOME", str(tmp_path))
    assert await open_session_from_payload(payload, user_override="rmh") is None
    assert not (tmp_path / ".punt-labs").exists()


async def test_open_records_the_manager_role_on_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    relay = LocalRelay(tmp_path / "relay")
    routing = SessionRouting("sess-1")
    opener = _opener(tmp_path, relay)
    labelled = SessionOpener(
        relay=relay,
        config=opener.config.model_copy(update={"reports_to": "coo"}),
        repo_common_root=opener.repo_common_root,
        unread_dir=opener.unread_dir,
        handles=opener.handles,
        hostname="h",
        pwd="/w",
    )

    key = await labelled.open(routing)

    row = await relay.get_session(key)
    assert row is not None and row.reports_to == "coo"
