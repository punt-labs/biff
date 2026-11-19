"""Configure observability for a single CLI invocation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import typer
from punt_observe.logging import Role

from biff.logging_config import configure_logging
from biff.usage import UsageCapture

_ROLES = {"mcp": Role.MCP, "serve": Role.MCP, "hook": Role.HOOK}


@dataclass(frozen=True, slots=True)
class McpInvocation:
    """Options and usage pipeline owned by the root CLI context."""

    user: str | None
    usage: UsageCapture


@dataclass(frozen=True, slots=True)
class CliInvocation:
    """Options governing CLI observability and interactive output."""

    verbose: bool
    no_telemetry: bool
    user: str | None

    def configure(self, ctx: typer.Context) -> None:
        """Initialize diagnostics and usage capture for this invocation."""
        command = ctx.invoked_subcommand or "repl"
        role = _ROLES.get(command, Role.CLI)
        configure_logging(role, stderr_level="DEBUG" if self.verbose else "WARNING")
        if role is Role.MCP:
            # FastMCP includes raw argument values in its validation logs.
            logging.getLogger("fastmcp.server.server").disabled = True
        self._register_usage(ctx, command, role)

    def _register_usage(self, ctx: typer.Context, command: str, role: Role) -> None:
        if role is not Role.MCP and not UsageCapture.captures_cli_command(command):
            return
        usage = UsageCapture.open(enabled=not self.no_telemetry)
        ctx.call_on_close(usage.telemetry.shutdown)
        if role is Role.MCP:
            ctx.obj = McpInvocation(self.user, usage)
        else:
            usage.record_cli_invocation(command)

    @staticmethod
    def validate_repl_output(*, json_output: bool, quiet_output: bool) -> None:
        """Reject noninteractive output options when starting the REPL.

        Raises:
            typer.BadParameter: At least one unsupported output option was supplied.
        """
        options = (("--json", json_output), ("--quiet", quiet_output))
        unsupported = [option for option, enabled in options if enabled]
        if unsupported:
            flags = " and ".join(unsupported)
            verb = "is" if len(unsupported) == 1 else "are"
            raise typer.BadParameter(f"{flags} {verb} not supported in REPL mode.")
