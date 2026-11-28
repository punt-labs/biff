"""What has arrived for a session, read without marking anything read."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff._formatting import terminal_safe
from biff.commands._result import CommandResult

if TYPE_CHECKING:
    from biff.daemon.params import Params
    from biff.daemon.session_verbs import SessionVerbs
    from biff.daemon.sessions import Session

__all__ = ["ArrivalsReading"]


@final
class ArrivalsReading:
    """The mark, the unread count, the wall and the talk traffic of one session.

    Every read here is of what is true now, and none marks mail read: only the
    user's own ``read`` does (``notification-mail-event.tex``,
    ``unreadN + readN = arrivedN``).  The unread count is the relay's plus the
    mail the daemon holds, because a consuming relay no longer lists mail that was
    fetched and not yet delivered.
    """

    __slots__ = ("_session",)

    _session: Session

    def __new__(cls, session: Session) -> Self:
        """Read the arrivals of *session*."""
        self = super().__new__(cls)
        self._session = session
        return self

    async def mark(self) -> int:
        """Return the relay's high-water mark of the session's inbox."""
        ctx = self._session.context
        return await ctx.relay.inbox_high_water(ctx.session_key)

    async def unread(self) -> int:
        """Return the unread count: the relay's, plus the mail the daemon holds."""
        ctx = self._session.context
        summary = await ctx.relay.get_unread_summary(ctx.session_key)
        return summary.count + len(self._session.outbox)

    @staticmethod
    async def handle(
        session: Session, _verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        """Answer ``arrivals``: the snapshot a client starts from, or checks against."""
        p.done()
        reading = ArrivalsReading(session)
        ctx = session.context
        mark = await reading.mark()  # the mark first: the count is read after it
        unread = await reading.unread()
        wall = await ctx.relay.get_wall()
        row = await ctx.relay.get_session(ctx.session_key)
        queued, invites = ctx.talk.peek()
        data = {
            "mark": mark,
            "unread": unread,
            "wall": None if wall is None else wall.model_dump(mode="json"),
            "invites": [
                {"from": terminal_safe(i.user), "tty": terminal_safe(i.tty)}
                for i in invites.values()
            ],
            "talk_pending": len(queued),
            # The session record holds mesg; an absent record is not off.
            "mesg": row is None or row.biff_enabled,
        }
        return CommandResult(text=f"unread: {unread}", json_data=data)
