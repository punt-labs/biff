"""Send the legacy ``tools/list_changed`` notification; log failures per episode."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, Self

import anyio
from mcp.shared.subscriptions import ToolsListChanged

if TYPE_CHECKING:
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.channel import ChannelLink

logger = logging.getLogger(__name__)

REMINDER_SECONDS = 600.0
# A client that stopped reading; anything else is a code bug.
TRANSPORT_ERRORS = (
    TimeoutError,
    OSError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)
_WHAT = "Tool list change (session kept)"


class EpisodeLog:
    """Log a repeating failure without burying the log or hiding a bug.

    Failures of an *expected* type (a client that stopped reading) latch per
    ``(label, type)``: one WARNING with the cause and traceback when the
    episode opens, a WARNING every ``REMINDER_SECONDS`` while it stays open,
    DEBUG in between, and one WARNING when the label next succeeds.  Anything
    else is a code bug: it is logged at ERROR with its traceback, once per
    distinct type until the label recovers, whatever episodes are open.  Biff's
    log keeps INFO and above, so none of these decisions may rest on DEBUG.
    """

    _logger: logging.Logger
    _expected: tuple[type[BaseException], ...]
    # (label, type) -> (started, last warned, attempts), by the monotonic clock
    _episodes: dict[tuple[str, type], tuple[float, float, int]]
    _bugs: set[tuple[str, type]]

    def __new__(
        cls, log: logging.Logger, expected: tuple[type[BaseException], ...]
    ) -> Self:
        """Create a log with no failure in progress."""
        self = super().__new__(cls)
        self._logger = log
        self._expected = expected
        self._episodes = {}
        self._bugs = set()
        return self

    def failed(self, what: str, exc: Exception) -> None:
        """Record that *what* failed with *exc*."""
        key = (what, type(exc))
        if not isinstance(exc, self._expected):
            if key not in self._bugs:
                self._bugs.add(key)
                self._logger.error("%s failed: %r", what, exc, exc_info=exc)
            return
        now = time.monotonic()
        if key not in self._episodes:
            self._episodes[key] = (now, now, 1)
            self._logger.warning("%s failed: %r; will retry", what, exc, exc_info=exc)
            return
        started, warned, attempts = self._episodes[key]
        attempts += 1
        if now - warned < REMINDER_SECONDS:
            self._episodes[key] = (started, warned, attempts)
            self._logger.debug("%s still failing: %r", what, exc)
            return
        self._episodes[key] = (started, now, attempts)
        self._logger.warning(
            "%s still failing for %d min, %d attempts; last error %r",
            what,
            (now - started) // 60,
            attempts,
            exc,
        )

    def recovered(self, what: str) -> None:
        """Record that *what* worked, logging recovery if it had failed."""
        keys = {k for k in (*self._episodes, *self._bugs) if k[0] == what}
        if keys:
            self._episodes = {k: e for k, e in self._episodes.items() if k not in keys}
            self._bugs -= keys
            self._logger.warning("%s recovered", what)


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
