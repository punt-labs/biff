"""SessionLifecycle routes a session hook by the checkout's own transport setting."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

import pytest

from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.session_lifecycle import SessionLifecycle

if TYPE_CHECKING:
    from pathlib import Path


class _Recording:
    """A closer that records what it was asked and answers as configured."""

    def __init__(self, *, settles: bool = True) -> None:
        self.ended: list[dict[str, object]] = []
        self._settles = settles

    async def end(self, data: dict[str, object]) -> bool:
        self.ended.append(data)
        return self._settles


def _checkout(path: Path, transport: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    config = path / ".punt-labs" / "biff" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(f"mcp:\n  transport: {transport}\n")
    return path


@pytest.fixture
def closer(monkeypatch: pytest.MonkeyPatch) -> _Recording:
    recording = _Recording()
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: recording)
    monkeypatch.delenv("BIFF_MCP_TRANSPORT", raising=False)
    return recording


def test_a_daemon_checkout_ends_through_biffd(
    closer: _Recording, tmp_path: Path
) -> None:
    root = _checkout(tmp_path / "a", "daemon")
    data: dict[str, object] = {"session_id": "s1", "cwd": str(root)}

    assert SessionLifecycle(data).end_through_biffd() is True
    assert closer.ended == [data]


def test_a_checkout_that_chose_in_process_is_left_to_the_sentinel_path(
    closer: _Recording, tmp_path: Path
) -> None:
    root = _checkout(tmp_path / "a", "inprocess")

    assert (
        SessionLifecycle({"session_id": "s1", "cwd": str(root)}).end_through_biffd()
        is False
    )
    assert closer.ended == []


@pytest.mark.parametrize("reason", ["resume", "clear", "logout", "other"])
def test_every_reason_ends_the_session(
    closer: _Recording, tmp_path: Path, reason: str
) -> None:
    # /resume inside a session switches to a different session id: the old one
    # has ended whatever the reason says, and the run check protects a session
    # that is really reopened.
    root = _checkout(tmp_path / "a", "daemon")
    data: dict[str, object] = {"session_id": "s1", "cwd": str(root), "reason": reason}

    assert SessionLifecycle(data).end_through_biffd() is True
    assert closer.ended == [data]


def test_a_closer_that_could_not_settle_leaves_the_sentinel_cleanup_to_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BIFF_MCP_TRANSPORT", raising=False)
    recording = _Recording(settles=False)
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: recording)
    root = _checkout(tmp_path / "a", "daemon")

    handled = SessionLifecycle(
        {"session_id": "s1", "cwd": str(root)}
    ).end_through_biffd()

    assert handled is False


def test_a_payload_without_a_session_id_is_logged_and_runs_no_reaper(
    closer: _Recording, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _checkout(tmp_path / "a", "daemon")

    with caplog.at_level(logging.WARNING):
        handled = SessionLifecycle({"cwd": str(root)}).end_through_biffd()

    assert handled is True  # biffd may hold the session; the reaper must not run
    assert closer.ended == []
    assert "no usable session_id" in caplog.text
