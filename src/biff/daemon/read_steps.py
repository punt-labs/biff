"""The steps of a ``read`` that touch the inbox: fetch, file back, count, confirm."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.daemon.session_fetch import SessionFetch
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.commands.inbox import Inbox
    from biff.daemon.sessions import Session
    from biff.models import Message

__all__ = ["ReadSteps"]

logger = logging.getLogger(__name__)


@final
class ReadSteps:
    """Fetch the unread mail, then file back, count and confirm what a batch leaves."""

    __slots__ = ()

    @staticmethod
    async def unread(session: Session) -> Inbox:
        """Fetch *session*'s unread mail."""
        return await SessionFetch.unread(session)

    @staticmethod
    async def refile(
        session: Session, inbox: Inbox, pending: list[Message], batch: list[Message]
    ) -> int:
        """Give the relay back the mail this batch leaves, if the relay consumed it.

        Return how many messages went back.

        Held in the daemon alone, that mail would be out of reach of everything
        else that drains or reads the session (ending it, another process), and
        gone if the daemon died.  The batch itself stays held until confirmed.
        """
        sent = {m.id for m in batch}
        left_behind = [m for m in pending if m.id not in sent]
        filed = 0
        for message in left_behind:
            try:
                if await inbox.refile(message):
                    # Released as it is filed: a read cancelled before the next
                    # refile must not leave this message in the relay and here.
                    session.outbox.forget([message])
                    filed += 1
            except RELAY_ERRORS:
                logger.warning("could not file message %s again", message.id)
        return filed

    @staticmethod
    async def backlog(inbox: Inbox) -> int:
        """Return how much mail waits on the relay behind a bounded fetch.

        The count is a hint for ``more``; a relay that fails to report it must
        not sink a batch that was already fetched and is deliverable.
        """
        try:
            return await inbox.unfetched()
        except RELAY_ERRORS as exc:
            logger.warning("read backlog summary unavailable: %s", exc)
            return 0

    @staticmethod
    async def delivered(session: Session, inbox: Inbox, batch: list[Message]) -> None:
        """Forget the batch and mark it read, once the reply that carried it left."""
        session.outbox.forget(batch)
        await inbox.mark_read(batch)
