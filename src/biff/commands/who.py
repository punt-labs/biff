"""``biff who`` — list active team members."""

from __future__ import annotations

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_who, log_dead_sessions
from biff.nostr_directory import SessionKeys
from biff.presence_liveness import live_sessions


async def who(ctx: CliContext) -> CommandResult:
    """List active team members and what they're working on.

    Sessions whose last heartbeat is older than the liveness window are
    dead (shut down, killed, or wedged) but may linger in the KV until the
    longer storage TTL; they are hidden from the table. A session dead long
    enough to rule out a single missed heartbeat is logged, not shown
    (DES-057).
    """
    sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    live = live_sessions(sessions)
    log_dead_sessions(sessions)
    if not live:
        text = "No sessions."
        json_data: list[dict[str, object]] = []
    else:
        # Sort by last_tool_at to match the IDLE column, not heartbeat recency.
        sorted_sessions = sorted(live, key=lambda s: s.last_tool_at, reverse=True)
        keys = await SessionKeys.for_repo(ctx.repo_root).lookup(live)
        text = format_who(sorted_sessions, keys)
        json_data = [s.model_dump(mode="json") for s in sorted_sessions]
    return CommandResult(text=text, json_data=json_data)
