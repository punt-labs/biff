"""A session's unread mail, fetched and acknowledged in separate steps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biff.cli_context import CliContext
    from biff.models import Message

__all__ = ["Inbox"]


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
        return sorted((*self._tty, *self._user), key=lambda m: (m.timestamp, m.id))

    async def unfetched(self) -> int:
        """Return how much unread mail the relay still holds beyond this fetch.

        A consuming relay pulls a bounded batch, so mail may wait behind it; a
        relay that does not consume returned everything it holds.
        """
        relay = self._ctx.relay
        if not relay.consumes_on_fetch:
            return 0
        return (await relay.get_unread_summary(self._ctx.session_key)).count

    async def refile(self, message: Message) -> bool:
        """File *message* in the relay again; say whether the relay held it before.

        A relay that consumes mail when it is fetched holds none of it afterwards,
        so mail a read could not deliver goes back, where ending the session or a
        read from another process finds it.  A relay that keeps unread mail is
        left alone: filing the message again would copy it.
        """
        if not self._ctx.relay.consumes_on_fetch:
            return False
        await self._ctx.relay.deliver(message.refiled())
        return True

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
