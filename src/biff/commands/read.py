"""``biff read`` — check inbox for new messages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff.commands._result import CommandResult
from biff.formatting import format_read

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biff.cli_context import CliContext
    from biff.models import Message

__all__ = ["Inbox", "read"]


@final
class Inbox:
    """A session's unread mail: its own inbox and its user's broadcast inbox.

    Reading and acknowledging are separate steps, so a caller that delivers the
    mail somewhere that can fail marks it read only once it has arrived.
    """

    __slots__ = ("_ctx", "_tty", "_user")

    _ctx: CliContext
    _tty: tuple[Message, ...]
    _user: tuple[Message, ...]

    def __new__(
        cls, ctx: CliContext, tty: Sequence[Message], user: Sequence[Message]
    ) -> Self:
        """Create the inbox holding *tty* and *user* mail for *ctx*."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._tty = tuple(tty)
        self._user = tuple(user)
        return self

    @classmethod
    async def unread(cls, ctx: CliContext) -> Self:
        """Fetch the session's unread mail without marking any of it read."""
        tty = await ctx.relay.fetch(ctx.session_key)
        user = await ctx.relay.fetch_user_inbox(ctx.user)
        return cls(ctx, tty, user)

    @property
    def messages(self) -> list[Message]:
        """Every unread message, oldest first."""
        return sorted((*self._tty, *self._user), key=lambda m: m.timestamp)

    async def mark_read(self, delivered: Sequence[Message]) -> None:
        """Mark *delivered*, and only those messages, read."""
        # A message addressed ``user:tty`` is in a session inbox, a bare ``user``
        # in the broadcast inbox; the address says so even for mail fetched earlier.
        tty_ids = [m.id for m in delivered if ":" in m.to_user]
        user_ids = [m.id for m in delivered if ":" not in m.to_user]
        if tty_ids:
            await self._ctx.relay.mark_read(self._ctx.session_key, tty_ids)
        if user_ids:
            await self._ctx.relay.mark_read_user_inbox(self._ctx.user, user_ids)


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
