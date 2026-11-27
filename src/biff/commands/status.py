"""``biff status`` — show connection state and session info."""

from __future__ import annotations

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.commands._status_report import build_status


async def status(ctx: CliContext) -> CommandResult:
    """Show connection state, session info, and pending messages."""
    return await build_status(ctx)
