"""What has arrived for a session, read without marking anything read."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Self, final

from biff._formatting import terminal_safe
from biff.commands._result import CommandResult

if TYPE_CHECKING:
    from biff.daemon.params import Params
    from biff.daemon.session_verbs import SessionVerbs
    from biff.daemon.sessions import Session

__all__ = ["ArrivalsReading"]

logger = logging.getLogger(__name__)


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

    _unrowed: ClassVar[set[str]] = set()
    """Sessions already reported as having no row, so each is said once."""

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
        if row is None and ctx.session_key not in ArrivalsReading._unrowed:
            ArrivalsReading._unrowed.add(ctx.session_key)
            logger.warning(
                "session %s has no session row; mesg is reported on, so events "
                "are not held",
                ctx.session_key,
            )
        queued, invites = ctx.talk.peek()
        # An invite is live from the moment it is queued, before talk_read drains it.
        live = {(terminal_safe(i.user), terminal_safe(i.tty)) for i in invites.values()}
        live |= {
            (terminal_safe(n.nfrom), terminal_safe(n.nfrom_tty))
            for n in queued
            if n.is_invite
        }
        data = {
            "mark": mark,
            "unread": unread,
            "wall": None if wall is None else wall.model_dump(mode="json"),
            "invites": [{"from": user, "tty": tty} for user, tty in sorted(live)],
            "talk_pending": len(queued),
            # The session record holds mesg; an absent record is not off.
            "mesg": row is None or row.biff_enabled,
        }
        return CommandResult(text=f"unread: {unread}", json_data=data)
