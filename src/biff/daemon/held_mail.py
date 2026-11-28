"""Mail a session's outbox holds, given back to the relay it came from."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.commands.inbox import Inbox
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.daemon.sessions import Session

__all__ = ["HeldMail"]

logger = logging.getLogger(__name__)


@final
class HeldMail:
    """The mail a session's outbox holds, which a consuming relay gave up at fetch."""

    @staticmethod
    async def refile(session: Session) -> None:
        """Give the relay back every message the session's outbox holds.

        While it is held here it is out of reach of the drain that ends or
        expires the session, and of a restarted daemon.

        Every message is attempted: one the relay refuses stays held and is
        logged, and the first such failure is raised once the rest were tried.
        """
        inbox = Inbox(session.context, (), ())
        first: Exception | None = None
        for message in session.outbox.held():
            try:
                if await inbox.refile(message):
                    session.outbox.forget([message])
            except RELAY_ERRORS as exc:
                logger.warning("could not file held message %s again", message.id)
                first = first or exc
        if first is not None:
            raise first
