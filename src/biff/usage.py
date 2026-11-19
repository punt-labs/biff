"""Privacy-safe usage schemas and MCP call instrumentation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from punt_observe.telemetry import (
    EnumAttr,
    SignalSchema,
    Telemetry,
    TelemetryConfig,
    open_telemetry,
)

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams

_CLI_SIGNAL = "biff.cli.invoke"
_MCP_SIGNAL = "biff.mcp.call"
_VERSION = 1
_CLI_COMMANDS = (
    "doctor",
    "enable",
    "disable",
    "finger",
    "install",
    "install-statusline",
    "last",
    "mesg",
    "plan",
    "read",
    "repl",
    "session",
    "status",
    "talk",
    "tty",
    "uninstall",
    "uninstall-statusline",
    "version",
    "wall",
    "who",
    "write",
)
_MCP_TOOLS = (
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
)
_SCHEMAS = (
    SignalSchema(_CLI_SIGNAL, _VERSION, "event", {"command": EnumAttr(_CLI_COMMANDS)}),
    SignalSchema(
        _MCP_SIGNAL,
        _VERSION,
        "span",
        {"tool": EnumAttr(_MCP_TOOLS), "outcome": EnumAttr(("ok", "error"))},
    ),
)


def open_usage(*, enabled: bool) -> Telemetry:
    """Open the shared capture pipeline with Biff's closed schemas."""
    return open_telemetry("biff", _SCHEMAS, TelemetryConfig(enabled=enabled))


def record_cli_invocation(usage: Telemetry, command: str) -> None:
    """Record a known top-level command without any invocation arguments."""
    usage.event(_CLI_SIGNAL, _VERSION, command=command)


def captures_cli_command(command: str) -> bool:
    """Return whether a CLI command belongs to the usage schema."""
    return command in _CLI_COMMANDS


class McpUsageMiddleware(Middleware):
    """Observe known MCP calls without inspecting arguments or results."""

    def __init__(self, usage: Telemetry) -> None:
        self._usage = usage

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Record duration and protocol outcome without collecting call content."""
        tool = context.message.name
        if tool not in _MCP_TOOLS:
            return await call_next(context)
        with self._usage.span(_MCP_SIGNAL, _VERSION, tool=tool):
            return await call_next(context)
