"""With ``mcp.transport: daemon`` SessionEnd asks biffd to end the session."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff._stdlib import sentinel_dir
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook import handle_session_end


class _Recording:
    def __init__(self) -> None:
        self.ended: list[dict[str, object]] = []

    async def end(self, data: dict[str, object]) -> None:
        self.ended.append(data)


class _Down:
    async def end(self, data: dict[str, object]) -> None:  # noqa: ARG002
        msg = "biffd is down"
        raise ConnectionError(msg)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")


def test_the_session_in_the_payload_is_ended_through_biffd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    closer = _Recording()
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: closer)

    handle_session_end({"session_id": "sess-1"})

    assert closer.ended == [{"session_id": "sess-1"}]
    assert not sentinel_dir("any").exists()


def test_a_daemon_failure_propagates_so_the_hook_logs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: _Down())

    with pytest.raises(ConnectionError, match="biffd is down"):
        handle_session_end({"session_id": "sess-1"})
