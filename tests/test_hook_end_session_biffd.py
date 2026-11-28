"""With ``mcp.transport: daemon`` SessionEnd asks biffd to end the session."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from biff._stdlib import sentinel_dir
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook import handle_session_end
from biff.session_registry import write_active_session

if TYPE_CHECKING:
    from pathlib import Path


class _Recording:
    def __init__(self) -> None:
        self.ended: list[dict[str, object]] = []

    async def end(self, data: dict[str, object]) -> bool:
        self.ended.append(data)
        return True


class _Declines:
    async def end(self, data: dict[str, object]) -> bool:
        return False


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")


def test_the_session_in_the_payload_is_ended_through_biffd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closer = _Recording()
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: closer)

    handle_session_end({"session_id": "sess-1"})

    assert closer.ended == [{"session_id": "sess-1"}]
    assert not sentinel_dir("any").exists()


def test_when_biffd_cannot_end_it_the_marker_becomes_a_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    monkeypatch.chdir(repo)
    write_active_session("repo", "kai:abc", worktree_root=str(repo))
    monkeypatch.setattr(DaemonSessionCloser, "for_host", lambda: _Declines())

    handled = handle_session_end({"session_id": "sess-1", "cwd": str(repo)})

    assert handled == 1
    assert (sentinel_dir("repo") / "kai-abc").read_text() == "kai:abc"


def test_a_malformed_transport_setting_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "carrier-pigeon")

    with pytest.raises(SystemExit):
        handle_session_end({"session_id": "sess-1"})
