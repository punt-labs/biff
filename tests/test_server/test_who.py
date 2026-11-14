"""The MCP ``who`` tool shows the human behind an agent on its own row."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing.caller import StampedClient, caller_for


async def _who(tmp_path: Path, launched_by: str) -> str:
    state = create_state(
        BiffConfig(
            user="claude",
            kind="agent",
            repo_name="_test-who",
            launched_by=launched_by,
            poll_interval=0.05,
        ),
        tmp_path / "data",
        hostname="h",
        pwd="/p",
        unread_dir=tmp_path / "unread",
    )
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-1")
    ) as client:
        result = await client.call_tool("who", {})
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture(autouse=True)
def _no_git_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's git user.name so only the OS-user floor can fill MGR."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


async def test_who_shows_the_os_user_floor(tmp_path: Path) -> None:
    out = await _who(tmp_path, "jim")
    assert "MGR" in out
    assert "jim" in out


async def test_who_without_any_candidate_has_no_column_and_no_error(
    tmp_path: Path,
) -> None:
    out = await _who(tmp_path, "")
    assert "claude:" in out
    assert "MGR" not in out
