"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.commands.plan_source import set_plan

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.commands._result import CommandResult


async def plan(ctx: CliContext, message: str) -> CommandResult:
    """Set the session's plan to *message*, as a person typing it would."""
    return await set_plan(ctx, message, "manual")
