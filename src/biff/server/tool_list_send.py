"""Send the legacy ``tools/list_changed`` notification; log failures per episode."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, Self

from mcp.shared.subscriptions import ToolsListChanged

if TYPE_CHECKING:
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.channel import ChannelLink

logger = logging.getLogger(__name__)

_WHAT = "Tool list change (session kept)"


class EpisodeLog:
    """Log a repeating failure once when it starts and once when it ends.

    A poller or a flush retries every few seconds, so a warning per attempt
    would bury the log.  The onset line names the cause and carries its
    traceback; later attempts log at DEBUG; the first success logs recovery.
    """

    _logger: logging.Logger
    _failing: set[str]

    def __new__(cls, log: logging.Logger) -> Self:
        """Create a log with no failure in progress."""
        self = super().__new__(cls)
        self._logger = log
        self._failing = set()
        return self

    def failed(self, what: str, exc: Exception) -> None:
        """Record that *what* failed with *exc*."""
        if what in self._failing:
            self._logger.debug("%s still failing: %r", what, exc)
            return
        self._failing.add(what)
        self._logger.warning("%s failed: %r; will retry", what, exc, exc_info=exc)

    def recovered(self, what: str) -> None:
        """Record that *what* worked, logging recovery if it had failed."""
        if what in self._failing:
            self._failing.discard(what)
            self._logger.info("%s recovered", what)


class SendOutcome(NamedTuple):
    """Whether the notification arrived, and whether the session is still good."""

    delivered: bool
    keep_session: bool


class ListChanged:
    """One ``notifications/tools/list_changed`` send and the verdict on it.

    A send never raises: the refresh that triggers it has already succeeded,
    and its result must not depend on a client that stopped listening.  Without
    a *link* (DES-061) a failed send leaves the session to be forgotten.  With
    one (channel mode, DES-087) the connection lasts the process's life, so the
    session and the debt are kept, logged once per episode with the cause.
    """

    _timeout: float
    _link: ChannelLink | None

    def __new__(cls, timeout: float, link: ChannelLink | None) -> Self:
        """Prepare a send bounded by *timeout* seconds."""
        self = super().__new__(cls)
        self._timeout = timeout
        self._link = link
        return self

    async def publish(self, bus: SubscriptionBus) -> None:
        """Publish the change to 2026-07-28 subscribers; a failure is only logged."""
        try:
            async with asyncio.timeout(self._timeout):
                await bus.publish(ToolsListChanged())
        except Exception:
            logger.warning("Tool list subscription publish failed", exc_info=True)

    async def send(self, push: Callable[[], Awaitable[None]]) -> SendOutcome:
        """Run *push*, which writes the notification, and report what happened."""
        link = self._link
        try:
            async with asyncio.timeout(self._timeout):
                await push()
            if link is not None:
                link.require_open()
        except Exception as exc:
            if link is None:
                logger.warning(
                    "Tool list change failed; session cleared", exc_info=True
                )
                return SendOutcome(delivered=False, keep_session=False)
            link.failed(_WHAT, exc)
            return SendOutcome(delivered=False, keep_session=True)
        if link is not None:
            link.delivered(_WHAT)
        return SendOutcome(delivered=True, keep_session=True)
