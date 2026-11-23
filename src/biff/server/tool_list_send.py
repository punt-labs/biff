"""Send the legacy ``tools/list_changed`` notification and judge the outcome."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple, Self

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

    from biff.server.channel import ChannelLink

logger = logging.getLogger(__name__)


class SendOutcome(NamedTuple):
    """Whether the notification arrived, and whether the session is still good."""

    delivered: bool
    keep_session: bool


class ListChanged:
    """One ``notifications/tools/list_changed`` send on a stored session.

    Without a *link* (DES-061) any failure leaves the session to be forgotten.
    With one (channel mode, DES-087) the connection lasts the process's life,
    so a timeout or a write the SDK dropped keeps the session and the debt,
    logged once per episode; any other exception is a bug and propagates.
    """

    _timeout: float
    _link: ChannelLink | None

    def __new__(cls, timeout: float, link: ChannelLink | None) -> Self:
        """Prepare a send bounded by *timeout* seconds."""
        self = super().__new__(cls)
        self._timeout = timeout
        self._link = link
        return self

    async def send(self, session: ServerSession) -> SendOutcome:
        """Send on *session* and report what happened."""
        link = self._link
        try:
            async with asyncio.timeout(self._timeout):
                await session.send_tool_list_changed()
            if link is not None:
                link.require_open()
        except Exception as exc:
            logger.debug("Standalone tool list change failed", exc_info=True)
            if link is None:
                logger.warning("Standalone tool list change failed; session cleared")
                return SendOutcome(delivered=False, keep_session=False)
            link.failed(exc)
            return SendOutcome(delivered=False, keep_session=True)
        if link is not None:
            link.delivered()
        return SendOutcome(delivered=True, keep_session=True)
