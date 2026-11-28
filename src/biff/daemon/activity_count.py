"""Counting a request as the activity of the session that made it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.daemon.sessions import Session

__all__ = ["ActivityCount"]

logger = logging.getLogger(__name__)


@final
class ActivityCount:
    """Moves a session's activity clock, which routes mail and shows as idle time."""

    __slots__ = ()

    @staticmethod
    async def of(session: Session) -> None:
        """Count a request as *session*'s activity; a relay that cannot is not fatal.

        A stamp that fails must not stop the request it describes: ``talk_end``
        has to hang up, and a command has its own relay errors to report.
        """
        try:
            await session.touch()
        except RELAY_ERRORS as exc:
            logger.warning("could not count a request as activity: %s", exc)
