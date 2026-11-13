"""The MCP ``who`` tool shows the manager label on an agent's own row."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing.caller import StampedClient, caller_for


async def _who(tmp_path: Path, reports_to: str) -> str:
    state = create_state(
        BiffConfig(
            user="claude",
            kind="agent",
            repo_name="_test-who",
            reports_to=reports_to,
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


async def test_who_shows_the_manager_label(tmp_path: Path) -> None:
    out = await _who(tmp_path, "ceo")
    assert "REPORTS" in out
    assert "ceo" in out


@pytest.mark.parametrize("empty", [""])
async def test_who_without_a_manager_has_no_label_and_no_error(
    tmp_path: Path, empty: str
) -> None:
    out = await _who(tmp_path, empty)
    assert "claude:" in out
    assert "REPORTS" not in out
