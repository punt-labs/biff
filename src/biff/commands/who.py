"""``biff who`` — list active team members."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_who
from biff.relay import live_sessions


async def who(ctx: CliContext) -> CommandResult:
    """List active team members and what they're working on.

    Sessions whose last heartbeat is older than the liveness window are
    dead (shut down, killed, or wedged) but may linger in the KV until the
    longer storage TTL; they are hidden from presence (biff-mue).
    """
    sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    live = live_sessions(sessions)
    if not live:
        return CommandResult(text="No sessions.", json_data=[])
    sorted_sessions = sorted(live, key=lambda s: s.last_active, reverse=True)
    return CommandResult(
        text=format_who(sorted_sessions),
        json_data=[s.model_dump(mode="json") for s in sorted_sessions],
    )
