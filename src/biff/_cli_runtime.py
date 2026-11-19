"""Configure observability for a single CLI invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer
from punt_observe.logging import Role

from biff.logging_config import configure_logging
from biff.usage import captures_cli_command, open_usage, record_cli_invocation

if TYPE_CHECKING:
    from punt_observe.telemetry import Telemetry

_ROLES = {"mcp": Role.MCP, "serve": Role.MCP, "hook": Role.HOOK}


@dataclass(frozen=True, slots=True)
class McpInvocation:
    """Options and usage pipeline owned by the root CLI context."""

    user: str | None
    usage: Telemetry


def validate_repl_output(*, json_output: bool, quiet_output: bool) -> None:
    """Reject noninteractive output options when starting the REPL.

    Raises:
        typer.BadParameter: At least one unsupported output option was supplied.
    """
    unsupported = [
        option
        for option, enabled in (("--json", json_output), ("--quiet", quiet_output))
        if enabled
    ]
    if unsupported:
        flags = " and ".join(unsupported)
        verb = "is" if len(unsupported) == 1 else "are"
        raise typer.BadParameter(f"{flags} {verb} not supported in REPL mode.")


def configure_invocation(
    ctx: typer.Context, *, verbose: bool, no_telemetry: bool, user: str | None
) -> None:
    """Initialize diagnostics and, for product invocations, local usage capture."""
    command = ctx.invoked_subcommand or "repl"
    role = _ROLES.get(command, Role.CLI)
    configure_logging(role, stderr_level="DEBUG" if verbose else "WARNING")
    if role is not Role.MCP and not captures_cli_command(command):
        return
    usage = open_usage(enabled=not no_telemetry)
    ctx.call_on_close(usage.shutdown)
    if role is Role.MCP:
        ctx.obj = McpInvocation(user, usage)
    else:
        record_cli_invocation(usage, command)
