"""``biff who`` — list active team members."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_who


async def who(ctx: CliContext) -> CommandResult:
    """List active team members and what they're working on."""
    sessions = await ctx.relay.get_sessions()
    visible = ctx.config.visible_repos
    sessions = [s for s in sessions if not s.repo or s.repo in visible]
    if not sessions:
        return CommandResult(text="No sessions.", json_data=[])
    sorted_sessions = sorted(sessions, key=lambda s: s.last_active, reverse=True)
    return CommandResult(
        text=format_who(sorted_sessions),
        json_data=[s.model_dump(mode="json") for s in sorted_sessions],
    )
