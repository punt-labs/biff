"""Fetch a session's unread mail so that none of it is in flight, unheld."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff.commands.inbox import Inbox

if TYPE_CHECKING:
    from biff.daemon.sessions import Session

__all__ = ["SessionFetch"]


@final
class SessionFetch:
    """The two fetches of a daemon read, the first held before the second starts."""

    @staticmethod
    async def unread(session: Session) -> Inbox:
        """Fetch *session*'s own mail and its user's broadcast mail, neither read.

        A consuming relay removed the session's mail when it was fetched, so it
        goes into the outbox before the broadcast fetch starts: a failure of that
        fetch, or a stop that finds it still running, leaves the mail held, where
        the next read or the hand-back finds it.
        """
        ctx = session.context
        tty = await ctx.relay.fetch(ctx.session_key)
        session.outbox.merge(tty)
        return Inbox(ctx, tty, await ctx.relay.fetch_user_inbox(ctx.user))
