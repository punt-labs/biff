"""The arrival subscription: arrival summaries written as event frames."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from biff.frames import Event
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from pydantic import JsonValue

    from biff.cli_context import CliContext
    from biff.frames import EventKind
    from biff.models import WallPost

__all__ = ["Subscription"]

logger = logging.getLogger(__name__)


@final
class Subscription:
    """Tells one connection what reaches its session.

    On start it sends the session's current unread summary and, when the
    repository has an active wall, the wall.  Then it sends one event per later
    arrival.  An event carries counts and the wall, never a message body.
    There is no replay log: a client that reconnects starts from the summary.
    """

    __slots__ = ("_ctx", "_interval", "_send", "_seq")

    _ctx: CliContext
    _send: Callable[[Event], Awaitable[None]]
    _seq: Iterator[int]
    _interval: float

    def __new__(
        cls,
        ctx: CliContext,
        send: Callable[[Event], Awaitable[None]],
        seq: Iterator[int],
        interval: float,
    ) -> Self:
        """Create a subscription for *ctx* that writes events with *send*.

        *seq* is the daemon-wide counter that orders events; *interval* is how
        often the relay is checked for arrivals.
        """
        self = super().__new__(cls)
        self._ctx = ctx
        self._send = send
        self._seq = seq
        self._interval = interval
        return self

    async def run(self) -> None:
        """Send the current summary, then each arrival, until cancelled."""
        mark = await self._ctx.relay.inbox_high_water(self._ctx.session_key)
        wall = await self._ctx.relay.get_wall()
        await self._emit("unread", {"count": await self._unread()})
        if wall is not None:
            await self._emit("wall", self._wall_data(wall))
        while True:
            await asyncio.sleep(self._interval)
            try:
                mark, wall = await self._arrivals(mark, wall)
            except RELAY_ERRORS:
                logger.info("subscription check failed (transient)", exc_info=True)

    async def _arrivals(
        self, mark: int, wall: WallPost | None
    ) -> tuple[int, WallPost | None]:
        """Emit what arrived since *mark* and *wall*; return the new baseline."""
        relay = self._ctx.relay
        current = await relay.inbox_high_water(self._ctx.session_key)
        # A read can lower the mark; only a rise is an arrival.
        if current > mark:
            await self._emit("mail", {"count": await self._unread()})
        posted = await relay.get_wall()
        if posted is not None and posted != wall:
            await self._emit("wall", self._wall_data(posted))
        return current, posted

    async def _unread(self) -> int:
        """Return the unread count over the session's and the user's inboxes."""
        summary = await self._ctx.relay.get_unread_summary(self._ctx.session_key)
        return summary.count

    async def _emit(self, kind: EventKind, data: JsonValue) -> None:
        await self._send(Event(seq=next(self._seq), kind=kind, data=data))

    @staticmethod
    def _wall_data(wall: WallPost) -> JsonValue:
        return wall.model_dump(mode="json")
