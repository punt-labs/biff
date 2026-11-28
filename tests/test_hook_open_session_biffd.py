"""With ``mcp.transport: daemon`` SessionStart asks biffd, not a relay."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.hook import _open_session

if TYPE_CHECKING:
    import pytest


class _Recording:
    def __init__(self) -> None:
        self.asked: list[dict[str, object]] = []

    async def open(self, data: dict[str, object]) -> str:
        self.asked.append(data)
        return "k"


class _Down:
    async def open(self, data: dict[str, object]) -> str:
        msg = "biffd is down"
        raise ConnectionError(msg)


async def _in_process(data: dict[str, object], **_kw: object) -> str | None:
    msg = f"the in-process path must not run for {data}"
    raise AssertionError(msg)


def test_the_daemon_opener_runs_and_the_in_process_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    opener = _Recording()
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: opener)

    with patch("biff.session_open.open_session_from_payload", _in_process):
        _open_session({"session_id": "sid-1"})

    assert opener.asked == [{"session_id": "sid-1"}]


def test_a_daemon_failure_is_logged_and_does_not_break_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: _Down())

    _open_session({"session_id": "sid-1"})  # must not raise

    assert "biffd is down" in caplog.text
