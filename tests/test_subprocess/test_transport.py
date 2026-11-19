"""Transport-level tests over real stdio subprocesses.

Verifies the wire protocol: CLI starts, JSON-RPC over stdin/stdout works,
and multiple subprocesses share state through the filesystem.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from biff.testing.caller import StampedClient

if TYPE_CHECKING:
    from fastmcp import Client
    from fastmcp.client.client import CallToolResult

pytestmark = pytest.mark.subprocess


def test_diagnostics_rotate_with_concurrent_process_roles(tmp_path: Path) -> None:
    roles = ("cli", "mcp", "hook")
    records_per_writer = 420
    payload = "word " * 1000 + "end"
    writer = """
import logging
import sys
from punt_observe.logging import Role
from biff.logging_config import configure_logging

configure_logging(Role(sys.argv[1]))
logger = logging.getLogger('biff.rotation')
for index in range(int(sys.argv[2])):
    logger.info('%s:%d %s', sys.argv[1], index, sys.argv[3])
"""

    def run(role: str) -> None:
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", writer, role, str(records_per_writer), payload],
            env={**os.environ, "HOME": str(tmp_path)},
            check=True,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        list(pool.map(run, roles))

    directory = tmp_path / ".punt-labs" / "biff" / "logs"
    assert directory.stat().st_mode & 0o777 == 0o700
    logs = [p for p in directory.glob("biff.log*") if not p.name.endswith(".lock")]
    assert 2 <= len(logs) <= 6
    rotation_bytes = 5 * 1024 * 1024
    max_record_bytes = len(payload.encode()) + 100
    seen: Counter[str] = Counter()
    for path in logs:
        assert path.stat().st_mode & 0o777 == 0o600
        size = path.stat().st_size
        # The shared sink checks size before taking its shared append lock.
        assert size <= rotation_bytes + (len(roles) - 1) * max_record_bytes
        if path.name != "biff.log":
            assert size > rotation_bytes - max_record_bytes
        for line in path.read_text().splitlines():
            header, record = line.split(" biff.rotation: ")
            assert header.endswith("[INFO]")
            key, body = record.split(" ", 1)
            assert body == payload
            seen[key] += 1
    assert seen == Counter(
        f"{role}:{index}" for role in roles for index in range(records_per_writer)
    )


def _text(result: CallToolResult) -> str:
    """Extract text from the first content block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestServerStartup:
    """The biff subprocess starts and responds to protocol requests."""

    async def test_tools_list(self, biff_client: Client[Any]) -> None:
        """Server lists all tools over stdio transport."""
        tools = await biff_client.list_tools()
        names = {t.name for t in tools}
        expected = {
            "biff",
            "biff_relay",
            "finger",
            "get_poll_status",
            "last",
            "mesg",
            "plan",
            "read_messages",
            "set_poll_interval",
            "talk",
            "talk_end",
            "talk_listen",
            "talk_read",
            "tty",
            "wall",
            "who",
            "write",
        }
        assert names == expected

    async def test_tools_have_descriptions(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"

    async def test_tools_have_input_schemas(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.inputSchema is not None, f"{tool.name} has no schema"


class TestToolCallOverStdio:
    """Tool calls round-trip correctly over the stdio wire protocol."""

    async def test_invalid_arguments_stay_out_of_diagnostics(
        self, tmp_path: Path
    ) -> None:
        transport = StdioTransport(
            command=sys.executable,
            args=[
                "-m",
                "biff",
                "--no-telemetry",
                "mcp",
                "--user",
                "kai",
                "--data-dir",
                str(tmp_path / "relay"),
                "--relay-url",
                "",
            ],
            env={"HOME": str(tmp_path)},
        )
        async with StampedClient(transport) as client:
            with pytest.raises(ToolError, match="rejected-input"):
                await client.call_tool(
                    "finger", {"user": {"private": "rejected-input"}}
                )

        diagnostics = (tmp_path / ".punt-labs/biff/logs/biff.log").read_text()
        assert "Starting Biff MCP server" in diagnostics
        assert "rejected-input" not in diagnostics

    async def test_plan_returns_text(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("plan", {"message": "testing stdio"})
        assert "testing stdio" in _text(result)

    async def test_who_returns_text(self, biff_client: Client[Any]) -> None:
        await biff_client.call_tool("plan", {"message": "online"})
        result = await biff_client.call_tool("who", {})
        assert "kai" in _text(result)

    async def test_finger_unknown_user(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("finger", {"user": "nobody"})
        assert "Never logged in" in _text(result)

    async def test_biff_toggle(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("mesg", {"enabled": False})
        assert "is n" in _text(result)
        result = await biff_client.call_tool("mesg", {"enabled": True})
        assert "is y" in _text(result)

    async def test_send_message_returns_text(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool(
            "write", {"to": "eric", "message": "hello over stdio"}
        )
        assert "eric" in _text(result)

    async def test_check_messages_empty(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("read_messages", {})
        assert "No new messages" in _text(result)


class TestCrossProcessState:
    """Two subprocesses share state through the same data directory."""

    async def test_plan_visible_across_processes(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
        shared_data_dir: Path,
    ) -> None:
        """kai's plan is visible to eric via separate subprocess.

        The plan message itself only appears on ``finger``, not ``who``
        (the ``who`` table shows name/repo/idle/status columns only).
        """
        await kai_client.call_tool("plan", {"message": "cross-process test"})
        who_result = await eric_client.call_tool("who", {})
        assert "kai" in _text(who_result)

        finger_result = await eric_client.call_tool("finger", {"user": "kai"})
        assert "cross-process test" in _text(finger_result)

    async def test_both_visible_in_who(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """Both subprocess users appear in /who."""
        await kai_client.call_tool("plan", {"message": "kai working"})
        await eric_client.call_tool("plan", {"message": "eric working"})

        result = await kai_client.call_tool("who", {})
        text = _text(result)
        assert "kai" in text
        assert "eric" in text

    async def test_message_across_processes(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """kai sends a message; eric receives it across processes."""
        await kai_client.call_tool(
            "write", {"to": "eric", "message": "cross-process msg"}
        )
        result = await eric_client.call_tool("read_messages", {})
        text = _text(result)
        assert "kai" in text
        assert "cross-process msg" in text


class TestDynamicDescriptionOverStdio:
    """read_messages description updates via stdio transport."""

    @staticmethod
    async def _check_desc(client: Client[Any]) -> str:
        tools = await client.list_tools()
        for t in tools:
            if t.name == "read_messages":
                assert t.description is not None
                return t.description
        raise AssertionError("read_messages not found")

    async def test_description_updates_after_message(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """kai sends to eric; eric's description shows unread."""
        await kai_client.call_tool(
            "write", {"to": "eric", "message": "subprocess test"}
        )
        # eric calls any tool to trigger refresh
        await eric_client.call_tool("plan", {"message": "working"})
        desc = await self._check_desc(eric_client)
        assert "1 unread" in desc

    async def test_description_reverts_after_check(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """After checking, description reverts to base."""
        # Drain messages from prior tests (shared NATS relay persists state)
        await eric_client.call_tool("read_messages", {})
        await kai_client.call_tool("write", {"to": "eric", "message": "hello"})
        await eric_client.call_tool("plan", {"message": "working"})
        desc = await self._check_desc(eric_client)
        assert "1 unread" in desc
        # Check clears it
        await eric_client.call_tool("read_messages", {})
        desc = await self._check_desc(eric_client)
        assert "unread" not in desc
