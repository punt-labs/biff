"""Tests for the FastMCP application factory."""

from __future__ import annotations

from fastmcp import FastMCP

from biff.server.app import create_server
from biff.server.state import ServerState


class TestCreateServer:
    def test_returns_fastmcp_instance(self, state: ServerState) -> None:
        mcp = create_server(state)
        assert isinstance(mcp, FastMCP)

    def test_server_name(self, state: ServerState) -> None:
        mcp = create_server(state)
        assert mcp.name == "biff"

    def test_registers_all_tools(self, state: ServerState) -> None:
        mcp = create_server(state)
        tool_names = {t.name for t in mcp._tool_manager._tools.values()}
        assert "mesg" in tool_names
        assert "write" in tool_names
        assert "read_messages" in tool_names
        assert "finger" in tool_names
        assert "who" in tool_names
        assert "plan" in tool_names

    def test_no_duplicate_tools(self, state: ServerState) -> None:
        mcp = create_server(state)
        tools = list(mcp._tool_manager._tools.values())
        names = [t.name for t in tools]
        assert len(names) == len(set(names))
