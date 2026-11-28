"""SessionLifecycle routes a session hook by the checkout's own transport setting."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.session_lifecycle import SessionLifecycle

if TYPE_CHECKING:
    from pathlib import Path


class _Recording:
    def __init__(self) -> None:
        self.ended: list[dict[str, object]] = []

    async def end(self, data: dict[str, object]) -> None:
        self.ended.append(data)


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


def test_a_resume_end_does_not_close_the_session_that_carries_on(
    closer: _Recording, tmp_path: Path
) -> None:
    root = _checkout(tmp_path / "a", "daemon")
    data: dict[str, object] = {
        "session_id": "s1",
        "cwd": str(root),
        "reason": "resume",
    }

    assert SessionLifecycle(data).end_through_biffd() is True
    assert closer.ended == []
