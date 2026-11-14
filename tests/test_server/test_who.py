"""The MCP ``who`` tool shows who launched an agent on its own row."""

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


async def test_who_shows_the_launching_user(tmp_path: Path) -> None:
    out = await _who(tmp_path, "jim")
    assert "MGR" in out
    assert "jim" in out


@pytest.mark.parametrize("empty", [""])
async def test_who_without_a_launching_user_has_no_column_and_no_error(
    tmp_path: Path, empty: str
) -> None:
    # No ethos team data and no config key: the value is the row's OS user.
    out = await _who(tmp_path, empty)
    assert "claude:" in out
    assert "MGR" not in out
