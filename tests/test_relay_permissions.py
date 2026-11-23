"""The local relay's directories are 0700 and its files 0600."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from biff._stdlib import biff_data_dir
from biff.models import Message, UserSession, WallPost
from biff.relay import LocalRelay


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def relay_dir(home: Path) -> Path:
    return biff_data_dir() / "relay" / "repo-key"


def _message(to_user: str) -> Message:
    return Message(from_user="kai", to_user=to_user, body="hi")


async def test_directories_are_created_0700(relay_dir: Path) -> None:
    await LocalRelay(relay_dir).deliver(_message("eric"))
    assert _mode(relay_dir) == 0o700
    assert _mode(relay_dir.parent) == 0o700


async def test_existing_directories_are_chmodded(relay_dir: Path) -> None:
    relay_dir.mkdir(parents=True)
    relay_dir.parent.chmod(0o755)
    relay_dir.chmod(0o755)
    await LocalRelay(relay_dir).deliver(_message("eric"))
    assert _mode(relay_dir) == 0o700
    assert _mode(relay_dir.parent) == 0o700


async def test_a_custom_data_dir_is_private_but_its_parent_is_left_alone(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    await LocalRelay(parent / "mine").deliver(_message("eric"))
    assert _mode(parent / "mine") == 0o700
    assert _mode(parent) == 0o755


async def test_broadcast_and_targeted_messages_are_0600(relay_dir: Path) -> None:
    relay = LocalRelay(relay_dir)
    await relay.deliver(_message("eric"))
    await relay.deliver(_message("eric:tty1"))
    assert _mode(relay_dir / "userinbox-eric.jsonl") == 0o600
    assert _mode(relay_dir / "inbox-eric-tty1.jsonl") == 0o600


async def test_rewritten_inbox_stays_0600(relay_dir: Path) -> None:
    relay = LocalRelay(relay_dir)
    message = _message("eric:tty1")
    await relay.deliver(message)
    await relay.mark_read("eric:tty1", [message.id])
    assert _mode(relay_dir / "inbox-eric-tty1.jsonl") == 0o600


async def test_existing_loose_file_is_tightened_on_next_write(
    relay_dir: Path,
) -> None:
    relay_dir.mkdir(parents=True)
    inbox = relay_dir / "userinbox-eric.jsonl"
    inbox.write_text("")
    inbox.chmod(0o644)
    await LocalRelay(relay_dir).deliver(_message("eric"))
    assert _mode(inbox) == 0o600


async def test_session_and_wall_files_are_0600(relay_dir: Path) -> None:
    relay = LocalRelay(relay_dir)
    await relay.update_session(UserSession(user="kai", tty="abc12345"))
    await relay.set_wall(
        WallPost(
            text="deploy",
            from_user="kai",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    assert _mode(relay_dir / "sessions.json") == 0o600
    assert _mode(relay_dir / "wall.json") == 0o600


async def test_tty_reservation_and_sentinel_files_are_0600(relay_dir: Path) -> None:
    relay = LocalRelay(relay_dir)
    assert await relay.reserve_tty_name("kai", "tty1", "kai:abc12345")
    relay.write_remove_sentinel("kai:abc12345")
    assert _mode(relay_dir / "ttyname-kai-tty1.lock") == 0o600
    assert _mode(relay_dir / "remove-kai-abc12345") == 0o600
