"""``biff read`` — check inbox for new messages."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_read


async def read(ctx: CliContext) -> CommandResult:
    """Check inbox for new messages. Marks all as read."""
    tty_unread = await ctx.relay.fetch(ctx.session_key)
    user_unread = await ctx.relay.fetch_user_inbox(ctx.user)
    all_unread = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)

    if not all_unread:
        return CommandResult(text="No new messages.", json_data=[])

    tty_ids = [m.id for m in tty_unread]
    user_ids = [m.id for m in user_unread]
    if tty_ids:
        await ctx.relay.mark_read(ctx.session_key, tty_ids)
    if user_ids:
        await ctx.relay.mark_read_user_inbox(ctx.user, user_ids)

    return CommandResult(
        text=format_read(all_unread),
        json_data=[m.model_dump(mode="json") for m in all_unread],
    )
