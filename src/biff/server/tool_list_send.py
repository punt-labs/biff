"""Send the legacy ``tools/list_changed`` notification and judge the outcome."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, Self

import anyio
from mcp.shared.subscriptions import ToolsListChanged

from biff.server.channel import ChannelUnavailableError

if TYPE_CHECKING:
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.channel import ChannelLink

logger = logging.getLogger(__name__)

# A send that cannot reach the client; anything else is a bug and propagates.
_UNDELIVERABLE = (
    TimeoutError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    ChannelUnavailableError,
)


class SendOutcome(NamedTuple):
    """Whether the notification arrived, and whether the session is still good."""

    delivered: bool
    keep_session: bool


class ListChanged:
    """One ``notifications/tools/list_changed`` send and the verdict on it.

    Without a *link* (DES-061) a failed send leaves the session to be forgotten.
    With one (channel mode, DES-087) the connection lasts the process's life,
    so a timeout or a write the SDK dropped keeps the session and the debt,
    logged once per episode with its cause.
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
        except _UNDELIVERABLE as exc:
            if link is None:
                logger.warning(
                    "Tool list change failed; session cleared", exc_info=True
                )
                return SendOutcome(delivered=False, keep_session=False)
            link.failed("Tool list change, session kept", exc)
            return SendOutcome(delivered=False, keep_session=True)
        if link is not None:
            link.delivered("Tool list change, session kept")
        return SendOutcome(delivered=True, keep_session=True)
