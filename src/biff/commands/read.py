"""``biff read`` — check inbox for new messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.commands._result import CommandResult
from biff.commands.inbox import Inbox
from biff.formatting import format_read

if TYPE_CHECKING:
    from biff.cli_context import CliContext

__all__ = ["read"]


async def read(ctx: CliContext) -> CommandResult:
    """Check inbox for new messages. Marks all as read."""
    inbox = await Inbox.unread(ctx)
    unread = inbox.messages
    if not unread:
        return CommandResult(text="No new messages.", json_data=[])
    await inbox.mark_read(unread)
    return CommandResult(
        text=format_read(unread),
        json_data=[m.model_dump(mode="json") for m in unread],
    )
