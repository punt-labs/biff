"""The per-session unread file carries ``channel_checked_at`` (DES-011a)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from biff.models import UnreadSummary
from biff.plugins.claude_code.unread_file import write_unread_file
from biff.unread import CheckStamp, read_session_unread

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, count: int = 0) -> None:
    write_unread_file(
        path,
        UnreadSummary(count=count),
        repo_name="r",
        user="kai",
        tty_name="tty1",
        biff_enabled=True,
    )


def test_a_fresh_file_has_no_check_time(tmp_path: Path) -> None:
    path = tmp_path / "unread.json"
    _write(path)
    unread = read_session_unread(path)
    assert unread is not None
    assert unread.channel_checked_at is None


def test_recording_a_check_sets_the_field(tmp_path: Path) -> None:
    path = tmp_path / "unread.json"
    _write(path, count=3)
    CheckStamp.record(path, 1234.5)
    unread = read_session_unread(path)
    assert unread is not None
    assert unread.channel_checked_at == 1234.5
    assert unread.count == 3


def test_the_next_unread_write_keeps_the_check_time(tmp_path: Path) -> None:
    path = tmp_path / "unread.json"
    _write(path)
    CheckStamp.record(path, 99.0)
    _write(path, count=5)
    assert json.loads(path.read_text())["channel_checked_at"] == 99.0


def test_recording_with_no_file_writes_one_with_the_field(tmp_path: Path) -> None:
    path = tmp_path / "unread.json"
    CheckStamp.record(path, 7.0)
    unread = read_session_unread(path)
    assert unread is not None
    assert unread.channel_checked_at == 7.0
