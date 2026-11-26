"""``biff who`` — list active team members."""

from __future__ import annotations

import logging

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.formatting import dead_sessions_summary, format_who
from biff.presence_liveness import dead_sessions, live_sessions


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
    if summary := dead_sessions_summary(dead_sessions(sessions)):
        logging.getLogger(__name__).info(summary)
    if not live:
        text = "No sessions."
        json_data: list[dict[str, object]] = []
    else:
        # Sort by last_tool_at, matching the IDLE column format_who renders
        # (:func:`biff.formatting.format_who`) -- last_active is heartbeat
        # recency, unrelated to the idle value shown.
        sorted_sessions = sorted(live, key=lambda s: s.last_tool_at, reverse=True)
        text = format_who(sorted_sessions)
        json_data = [s.model_dump(mode="json") for s in sorted_sessions]
    return CommandResult(text=text, json_data=json_data)
